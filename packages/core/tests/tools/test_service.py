import httpx
import pytest
from sqlalchemy import select

from supportcore.config import Settings
from supportcore.db.models import AuditLog, BusinessTool, Organisation
from supportcore.errors import NotFoundError
from supportcore.tools.http_executor import HttpToolExecutor
from supportcore.tools.schema import InvalidToolArgumentsError, InvalidToolSchemaError
from supportcore.tools.service import BusinessToolService, InvalidToolError, ToolNameTakenError

SCHEMA = {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}


def _args(**over):
    base = dict(
        name="get_order",
        description="Look up an order",
        method="GET",
        url_template="https://api.shop.com/orders/{order_id}",
        input_schema=SCHEMA,
    )
    return {**base, **over}


async def test_create_list_get_and_audit(tools, owner, session):
    tool = await tools.create(actor=owner, **_args())
    assert (tool.enabled, tool.read_only, tool.auth_header_last4) == (True, True, None)
    assert [t.name for t in await tools.list()] == ["get_order"]
    assert (await tools.get(tool.id)).id == tool.id
    rows = (await session.execute(select(AuditLog).where(AuditLog.action == "business_tool.created"))).scalars().all()
    assert len(rows) == 1


@pytest.mark.parametrize(
    "over",
    [
        {"name": "Bad Name"},
        {"name": "x"},
        {"description": ""},
        {"method": "DELETE"},
        {"url_template": "ftp://x.com/a"},
        {"url_template": "https://user:pw@x.com/a"},
        {"url_template": "https://{order_id}.x.com/a"},
        {"auth_header_name": "Authorization"},
        {"auth_header_name": "Host", "auth_header_value": "v"},
        {"auth_header_name": "X-Key", "auth_header_value": "bad\nvalue"},
    ],
)
async def test_create_rejects_bad_input(tools, owner, over):
    with pytest.raises(InvalidToolError):
        await tools.create(actor=owner, **_args(**over))


async def test_placeholder_must_be_required(tools, owner):
    optional = {"type": "object", "properties": {"order_id": {"type": "string"}}}
    with pytest.raises(InvalidToolSchemaError, match="required"):
        await tools.create(actor=owner, **_args(input_schema=optional))
    tool = await tools.create(actor=owner, **_args())
    with pytest.raises(InvalidToolSchemaError, match="required"):
        await tools.update(tool.id, actor=owner, input_schema=optional)


async def test_create_schema_errors(tools, owner):
    with pytest.raises(InvalidToolSchemaError):
        await tools.create(actor=owner, **_args(input_schema={"type": "array"}))
    with pytest.raises(InvalidToolSchemaError):
        await tools.create(actor=owner, **_args(url_template="https://x.com/{nope}"))


async def test_create_requires_https_in_prod(session, org, bus, cipher, owner):
    from cryptography.fernet import Fernet

    prod = Settings(
        _env_file=None, env="prod", jwt_secret="x" * 40, secrets_key=Fernet.generate_key().decode(),
        billing="stripe", stripe_secret_key="sk_x", stripe_webhook_secret="whsec_x",
    )
    service = BusinessToolService(session, org.id, bus, cipher, prod)
    with pytest.raises(InvalidToolError, match="https"):
        await service.create(actor=owner, **_args(url_template="http://api.shop.com/orders/{order_id}"))
    with pytest.raises(InvalidToolError, match="not allowed"):  # private literal: refused without DNS
        await service.create(actor=owner, **_args(url_template="https://10.0.0.5/orders/{order_id}"))
    with pytest.raises(InvalidToolError, match="not allowed"):
        await service.create(actor=owner, **_args(url_template="https://localhost/orders/{order_id}"))


async def test_http_allowed_in_dev_test_only_with_private_allowed(tools, owner, session, org, bus, cipher):
    tool = await tools.create(actor=owner, **_args(url_template="http://localhost:9000/orders/{order_id}"))
    assert tool.url_template.startswith("http://localhost")
    strict = Settings(_env_file=None, env="test", jwt_secret="x" * 40, allow_private_base_urls=False)
    with pytest.raises(InvalidToolError):
        await BusinessToolService(session, org.id, bus, cipher, strict).create(
            actor=owner, **_args(name="other", url_template="http://api.shop.com/x/{order_id}")
        )


async def test_auth_value_encrypted_and_last4_only(tools, owner, session, cipher):
    tool = await tools.create(
        actor=owner, **_args(auth_header_name="X-Api-Key", auth_header_value="secret-token-1234")
    )
    assert tool.auth_header_last4 == "1234" and tool.auth_header_name == "X-Api-Key"
    assert tool.encrypted_auth_header_value != "secret-token-1234"
    assert cipher.decrypt(tool.encrypted_auth_header_value) == "secret-token-1234"
    (snap,) = await tools.enabled_snapshots()
    assert snap.auth_value == "secret-token-1234" and "secret-token" not in repr(snap)
    # Rotating the value: audit says only "changed".
    await tools.update(tool.id, actor=owner, auth_header_value="other-secret-9999")
    assert tool.auth_header_last4 == "9999"
    rows = (await session.execute(select(AuditLog))).scalars().all()
    dump = repr([(r.action, r.__dict__) for r in rows])
    assert "secret-token" not in dump and "other-secret" not in dump and "9999" not in dump
    updated = next(r for r in rows if r.action == "business_tool.updated")
    assert "changed" in repr(updated.__dict__)


async def test_update_fields_clear_auth_and_name_taken(tools, owner):
    tool = await tools.create(actor=owner, **_args(auth_header_name="X-Key", auth_header_value="abcd1234"))
    other = await tools.create(actor=owner, **_args(name="other_tool"))
    updated = await tools.update(tool.id, actor=owner, description="New", enabled=False, read_only=False)
    assert (updated.description, updated.enabled, updated.read_only) == ("New", False, False)
    assert await tools.enabled_snapshots() == [await _snap(tools, other)]
    await tools.update(tool.id, actor=owner, auth_header_name=None)
    assert (tool.auth_header_name, tool.encrypted_auth_header_value, tool.auth_header_last4) == (None, None, None)
    with pytest.raises(ToolNameTakenError):
        await tools.update(other.id, actor=owner, name="get_order")
    with pytest.raises(ToolNameTakenError):
        await tools.create(actor=owner, **_args())
    with pytest.raises(InvalidToolError):  # value without a header name
        await tools.update(other.id, actor=owner, auth_header_value="v")
    with pytest.raises(InvalidToolSchemaError):  # new schema drops the URL placeholder
        await tools.update(tool.id, actor=owner, input_schema={"type": "object"})
    with pytest.raises(InvalidToolError):
        await tools.update(tool.id, actor=owner, organisation_id="x")


async def _snap(tools, tool):
    (snap,) = [s for s in await tools.enabled_snapshots() if s.id == tool.id]
    return snap


async def test_delete_and_cross_tenant_404(tools, owner, session, org_b, bus, cipher, settings):
    tool = await tools.create(actor=owner, **_args())
    foreign = BusinessToolService(session, org_b.id, bus, cipher, settings)
    with pytest.raises(NotFoundError):
        await foreign.get(tool.id)
    with pytest.raises(NotFoundError):
        await foreign.delete(tool.id, owner)
    with pytest.raises(NotFoundError):
        await foreign.update(tool.id, actor=owner, description="x")
    assert await foreign.list() == []
    await tools.delete(tool.id, owner)
    assert await tools.list() == []
    with pytest.raises(NotFoundError):
        await tools.get(tool.id)


async def test_test_commits_before_the_http_call(session_factory, settings, cipher):
    from supportcore.events import EventBus
    from supportcore.db.models import User

    async with session_factory() as s:
        user = User(email="o@x.com", password_hash="x", name="O")
        org = Organisation(name="Acme", slug="acme")
        s.add_all([user, org])
        await s.commit()
        user_id, org_id = user.id, org.id

    async with session_factory() as s:
        in_transaction_during_call = []
        seen = []

        def handler(request):
            in_transaction_during_call.append(s.in_transaction())
            seen.append(request)
            return httpx.Response(200, text="ok")

        service = BusinessToolService(
            s, org_id, EventBus(), cipher, settings, HttpToolExecutor(settings, transport=httpx.MockTransport(handler))
        )
        actor = await s.get(User, user_id)
        tool = await service.create(
            actor=actor, **_args(auth_header_name="X-Key", auth_header_value="abcd1234")
        )
        result = await service.test(tool.id, {"order_id": "A/1"})
        assert result.ok and result.text == "ok"
        assert in_transaction_during_call == [False]  # nothing held open across the call
        assert seen[0].headers["X-Key"] == "abcd1234" and seen[0].url.raw_path == b"/orders/A%2F1"
        with pytest.raises(InvalidToolArgumentsError):
            await service.test(tool.id, {})
        with pytest.raises(InvalidToolArgumentsError):
            await service.test(tool.id, {"order_id": ".."})
    async with session_factory() as s:  # the create was committed by test()
        assert (await s.execute(select(BusinessTool.id))).scalar_one() == tool.id


async def test_url_origin_change_clears_the_stored_auth_value(tools, owner, session):
    tool = await tools.create(actor=owner, **_args(auth_header_name="X-Key", auth_header_value="secret-abcd1234"))
    # Same origin, new path: the header stays.
    await tools.update(tool.id, actor=owner, url_template="https://api.shop.com/v2/orders/{order_id}")
    assert (tool.auth_header_name, tool.auth_header_last4) == ("X-Key", "1234")
    for url in (
        "https://evil.example.net/orders/{order_id}",  # host
        "https://api.shop.com:8443/orders/{order_id}",  # port
        "http://api.shop.com/orders/{order_id}",  # scheme
    ):
        await tools.update(tool.id, actor=owner, auth_header_name="X-Key", auth_header_value="secret-abcd1234")
        await tools.update(tool.id, actor=owner, url_template=url)
        assert (tool.auth_header_name, tool.encrypted_auth_header_value, tool.auth_header_last4) == (None, None, None)
        assert (await _snap(tools, tool)).auth_value is None
        await tools.update(tool.id, actor=owner, url_template="https://api.shop.com/orders/{order_id}")
    # A new value in the same update is kept.
    await tools.update(
        tool.id,
        actor=owner,
        url_template="https://evil.example.net/orders/{order_id}",
        auth_header_name="X-Key",
        auth_header_value="fresh-value-5678",
    )
    assert (tool.auth_header_name, tool.auth_header_last4) == ("X-Key", "5678")
    # A header name alone with a new origin is refused: the value must be re-entered.
    with pytest.raises(InvalidToolError, match="set together"):
        await tools.update(
            tool.id, actor=owner, url_template="https://api.shop.com/orders/{order_id}", auth_header_name="X-Key"
        )
    updated = (
        (await session.execute(select(AuditLog).where(AuditLog.action == "business_tool.updated"))).scalars().all()
    )
    assert "fresh-value" not in repr([r.__dict__ for r in updated])
