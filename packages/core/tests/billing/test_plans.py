import pytest
from sqlalchemy import select
from supportcore.billing.errors import (
    BillingNotConfiguredError,
    BillingProviderError,
    PlanKeyTakenError,
    PlanNotAvailableError,
)
from supportcore.billing.fake_billing import FakeBilling
from supportcore.billing.plans import (
    PlanInput,
    PlanPatch,
    PlanService,
    line_prices,
    resolve_plan,
    sync_plan_job,
)
from supportcore.billing.ports import PlanPrices
from supportcore.config import Settings
from supportcore.db.models import Plan, PlanProviderPrice, User
from supportcore.events import EventBus
from supportcore.jobs.ports import JobRuntime
from supportcore.knowledge.jobs import dispatch


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, dict]] = []

    def enqueue(self, name, payload, *, countdown=None) -> None:
        self.jobs.append((name, payload))


class RepricingFake(FakeBilling):
    """Like Stripe: a changed base amount gets a new base price (the fake alone keeps the old one)."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = []

    async def sync_plan(self, plan, existing, product_id):
        self.calls.append((plan, existing, product_id))
        if existing is not None:
            old = await super().sync_plan(plan, PlanPrices("", existing.meter_price_ids), product_id)
            price = self._price("base", plan.monthly_price_minor)
            if plan.monthly_price_minor == self._last:
                return old.__class__(old.product_id, existing, [])
            self._last = plan.monthly_price_minor
            return old.__class__(old.product_id, PlanPrices(price.price_id, existing.meter_price_ids), [price])
        self._last = plan.monthly_price_minor
        return await super().sync_plan(plan, existing, product_id)


class _Adapters:
    def __init__(self, billing) -> None:
        self.billing = billing
        self.job_queue = RecordingQueue()


@pytest.fixture
def fake():
    return RepricingFake()


@pytest.fixture
def adapters(fake):
    return _Adapters(fake)


@pytest.fixture
def runtime(session_factory, adapters):
    settings = Settings(_env_file=None, env="test", billing="fake", jwt_secret="test-secret-test-secret-test-secret-123")
    return JobRuntime(settings=settings, session_factory=session_factory, adapters=adapters, events=EventBus())


@pytest.fixture
async def admin(session):
    u = User(email="root@x.com", password_hash="x", name="Root", is_platform_admin=True)
    session.add(u)
    await session.flush()
    return u


def _input(**kw) -> PlanInput:
    return PlanInput(**{"key": "pro", "name": "Pro", "description": "For teams", "monthly_price_minor": 4900} | kw)


async def _make(session_factory, adapters, admin_id=None, **kw):
    """A committed plan (via the service) so jobs on other sessions see it."""
    async with session_factory() as s:
        user = User(email=f"a{kw.get('key', 'pro')}@x.com", password_hash="x", name="A")
        s.add(user)
        await s.flush()
        plan = await PlanService(s, adapters.job_queue).create(_input(**kw), user)
        await s.commit()
        return plan.id


async def test_plan_create_enqueues_sync_after_commit(session, admin, adapters):
    service = PlanService(session, adapters.job_queue)
    plan = await service.create(_input(), admin)
    assert plan.sync_status == "pending"
    assert adapters.job_queue.jobs == []  # not before the commit
    await session.commit()
    assert adapters.job_queue.jobs == [("sync_billing_plan", {"plan_id": str(plan.id)})]
    await service.update(plan.id, PlanPatch(name="Pro+"), admin)
    await service.set_active(plan.id, False, admin)
    await session.commit()
    assert len(adapters.job_queue.jobs) == 3


async def test_plan_key_taken(session, admin, adapters):
    service = PlanService(session, adapters.job_queue)
    await service.create(_input(), admin)
    with pytest.raises(PlanKeyTakenError):
        await service.create(_input(), admin)
    assert len(await service.list()) == 1  # the savepoint kept the session usable


async def test_plan_input_validation():
    for bad in ({"key": "A"}, {"key": "x" * 51}, {"monthly_price_minor": -1}, {"trial_days": 91}, {"seats": -1}, {"messages_overage_micros": -5}):
        with pytest.raises(ValueError):
            _input(**bad)
    assert _input(seats=None, trial_days=90).seats is None


async def test_update_null_clears_limit_but_unset_keeps(session, admin, adapters):
    service = PlanService(session, adapters.job_queue)
    plan = await service.create(_input(seats=5, messages=100), admin)
    await service.update(plan.id, PlanPatch.model_validate({"seats": None}), admin)
    assert plan.seats is None and plan.messages == 100


async def test_sync_job_stores_ids_and_marks_synced(runtime, session_factory, adapters, fake):
    plan_id = await _make(session_factory, adapters, messages=1000, messages_overage_micros=20_000)
    await dispatch(runtime, "sync_billing_plan", {"plan_id": str(plan_id)})
    spec = fake.calls[0][0] if fake.calls else None
    async with session_factory() as s:
        plan = await s.get(Plan, plan_id)
        prices = (await s.execute(select(PlanProviderPrice).where(PlanProviderPrice.plan_id == plan_id))).scalars().all()
        assert plan.sync_status == "synced" and plan.sync_error is None and plan.synced_at is not None
        assert plan.provider == "fake" and plan.provider_product_id == "fake_prod_pro"
        assert sorted(p.kind for p in prices) == ["base", "messages"] and all(p.active for p in prices)
        assert next(p for p in prices if p.kind == "base").amount_minor == 4900
        assert await line_prices(s, plan, fake, overage=False) == [next(p.price_id for p in prices if p.kind == "base")]
        assert len(await line_prices(s, plan, fake, overage=True)) == 2
    assert spec.currency == "usd" and spec.description == "For teams" and spec.messages_limit == 1000


@pytest.mark.parametrize(
    ("error", "code"),
    [(BillingProviderError, "billing_provider_error"), (BillingNotConfiguredError, "billing_not_configured")],
)
async def test_sync_job_failure_sets_failed_code(runtime, session_factory, adapters, fake, error, code):
    plan_id = await _make(session_factory, adapters)
    fake.fail_next = error
    await sync_plan_job(runtime, plan_id)
    async with session_factory() as s:
        plan = await s.get(Plan, plan_id)
        assert (plan.sync_status, plan.sync_error) == ("failed", code)
        with pytest.raises(PlanNotAvailableError):
            await line_prices(s, plan, fake, overage=False)
    await sync_plan_job(runtime, plan_id)  # a retry succeeds and clears the error
    async with session_factory() as s:
        plan = await s.get(Plan, plan_id)
        assert (plan.sync_status, plan.sync_error) == ("synced", None)


async def test_price_edit_keeps_old_price_resolvable(runtime, session_factory, adapters, fake):
    plan_id = await _make(session_factory, adapters)
    await sync_plan_job(runtime, plan_id)
    async with session_factory() as s:
        plan = await s.get(Plan, plan_id)
        old_base = (await line_prices(s, plan, fake, overage=False))[0]
        admin = User(email="b@x.com", password_hash="x", name="B")
        s.add(admin)
        await s.flush()
        await PlanService(s, adapters.job_queue).update(plan_id, PlanPatch(monthly_price_minor=9900), admin)
        await s.commit()
    await sync_plan_job(runtime, plan_id)
    async with session_factory() as s:
        plan = await s.get(Plan, plan_id)
        new_base = (await line_prices(s, plan, fake, overage=False))[0]
        assert new_base != old_base and plan.sync_status == "synced"
        assert (await resolve_plan(s, fake, [old_base])).id == plan_id
        assert (await resolve_plan(s, fake, ["other", new_base])).id == plan_id
        assert await resolve_plan(s, fake, ["nope"]) is None and await resolve_plan(s, fake, []) is None
        rows = {p.price_id: p for p in (await s.execute(select(PlanProviderPrice))).scalars()}
        assert rows[old_base].active is False and rows[new_base].amount_minor == 9900
    assert fake.calls[1][1].base_price_id == old_base and fake.calls[1][2] == "fake_prod_pro"


async def test_removed_overage_deactivates_meter_price(runtime, session_factory, adapters, fake):
    plan_id = await _make(session_factory, adapters, messages_overage_micros=5000)
    await sync_plan_job(runtime, plan_id)
    async with session_factory() as s:
        admin = User(email="b@x.com", password_hash="x", name="B")
        s.add(admin)
        await s.flush()
        await PlanService(s, adapters.job_queue).update(
            plan_id, PlanPatch.model_validate({"messages_overage_micros": None}), admin
        )
        await s.commit()

    class NoMeter(RepricingFake):
        async def sync_plan(self, plan, existing, product_id):
            result = await super().sync_plan(plan, existing, product_id)
            return result.__class__(result.product_id, PlanPrices(result.prices.base_price_id, {}), result.created)

    adapters.billing = NoMeter()
    adapters.billing._last = 4900
    await sync_plan_job(runtime, plan_id)
    async with session_factory() as s:
        plan = await s.get(Plan, plan_id)
        assert len(await line_prices(s, plan, adapters.billing, overage=True)) == 1


async def test_sync_job_unexpected_error_marks_failed_internal(runtime, session_factory, adapters, fake):
    """T6: a non-provider exception never leaves the plan `pending`."""
    plan_id = await _make(session_factory, adapters)
    fake.fail_next = RuntimeError
    await sync_plan_job(runtime, plan_id)
    async with session_factory() as s:
        plan = await s.get(Plan, plan_id)
        assert (plan.sync_status, plan.sync_error) == ("failed", "internal_error")


async def test_sync_job_records_metered_terms(runtime, session_factory, adapters, fake):
    """I2: each metered price records the free units and overage price it was created with."""
    plan_id = await _make(
        session_factory, adapters, messages=1000, voice_minutes=None, messages_overage_micros=20_000,
        voice_minutes_overage_micros=90_000,
    )
    await sync_plan_job(runtime, plan_id)
    async with session_factory() as s:
        prices = (await s.execute(select(PlanProviderPrice).where(PlanProviderPrice.plan_id == plan_id))).scalars().all()
    terms = {p.kind: (p.included_units, p.overage_micros) for p in prices}
    assert terms == {"base": (None, None), "messages": (1000, 20_000), "voice_minutes": (None, 90_000)}
