"""Platform admin CRUD for model prices (Phase 5, Task 2)."""

import pytest
from sqlalchemy import update

from supportcore.analytics import pricing
from supportcore.db.models import User

PASSWORD = "correct-horse-1"
BODY = {"provider": "openai", "model": "gpt-x", "input_per_million_micros": 2_500_000, "output_per_million_micros": 10_000_000}


@pytest.fixture(autouse=True)
def _fresh_cache():
    pricing.clear_cache()
    yield
    pricing.clear_cache()


async def _admin(client, session) -> dict[str, str]:
    r = await client.post("/auth/signup", json={"email": "root@x.com", "password": PASSWORD, "name": "Root"})
    assert r.status_code == 201
    await session.execute(update(User).where(User.email == "root@x.com").values(is_platform_admin=True))
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_admin_prices_crud_and_403_for_owner(client, auth_headers, session):
    await client.post("/orgs", json={"name": "Acme"}, headers=auth_headers)  # the caller owns an organisation
    for call in (
        client.get("/admin/model-prices", headers=auth_headers),
        client.put("/admin/model-prices", json=BODY, headers=auth_headers),
        client.delete("/admin/model-prices/00000000-0000-0000-0000-000000000000", headers=auth_headers),
    ):
        assert (await call).status_code == 403
    assert (await client.get("/admin/model-prices")).status_code == 401

    admin = await _admin(client, session)
    assert (await client.get("/admin/model-prices", headers=admin)).json() == []  # nothing is seeded

    created = await client.put("/admin/model-prices", json=BODY, headers=admin)
    assert created.status_code == 200
    row = created.json()
    assert set(row) == {
        "id", "provider", "model", "input_per_million_micros", "output_per_million_micros", "currency",
        "updated_by", "updated_at",
    }
    assert row["currency"] == "USD" and row["input_per_million_micros"] == 2_500_000

    # upsert on (provider, model): same id, new values
    again = await client.put("/admin/model-prices", json=BODY | {"input_per_million_micros": 1}, headers=admin)
    assert again.json()["id"] == row["id"] and again.json()["input_per_million_micros"] == 1
    assert len((await client.get("/admin/model-prices", headers=admin)).json()) == 1

    assert (await client.delete(f"/admin/model-prices/{row['id']}", headers=admin)).status_code == 204
    assert (await client.delete(f"/admin/model-prices/{row['id']}", headers=admin)).status_code == 404
    assert (await client.get("/admin/model-prices", headers=admin)).json() == []


@pytest.mark.parametrize(
    "patch",
    [
        {"input_per_million_micros": -1},
        {"output_per_million_micros": 10**12 + 1},
        {"input_per_million_micros": 1.5},
        {"provider": ""},
        {"model": ""},
    ],
)
async def test_admin_price_validation(client, session, patch):
    admin = await _admin(client, session)
    assert (await client.put("/admin/model-prices", json=BODY | patch, headers=admin)).status_code == 422
    assert (await client.put("/admin/model-prices", json=BODY | {"input_per_million_micros": 10**12}, headers=admin)).status_code == 200


async def test_admin_put_and_delete_invalidate_this_process_cache(client, session):
    admin = await _admin(client, session)
    assert await pricing.price_for(session, "openai", "gpt-x") is None  # cached as unpriced
    await client.put("/admin/model-prices", json=BODY, headers=admin)
    assert (await pricing.price_for(session, "openai", "gpt-x")).input_per_million_micros == 2_500_000
    row = (await client.get("/admin/model-prices", headers=admin)).json()[0]
    await client.delete(f"/admin/model-prices/{row['id']}", headers=admin)
    assert await pricing.price_for(session, "openai", "gpt-x") is None


async def test_admin_price_change_is_logged_with_ids_only(client, session, capsys):
    from structlog.testing import capture_logs

    admin = await _admin(client, session)
    with capture_logs() as logs:
        await client.put("/admin/model-prices", json=BODY, headers=admin)
    [entry] = [e for e in logs if e["event"] == "model_price_changed"]
    assert set(entry) - {"log_level"} == {"event", "provider", "model", "admin_user_id", "change"}
    assert entry["provider"] == "openai" and entry["change"] == "upsert"


async def test_invalidation_happens_after_commit(client, session, monkeypatch):
    """The cache is dropped by an after-commit hook, not inside the handler (a read in between would re-cache
    the old price)."""
    admin = await _admin(client, session)
    seen = []
    from supportapi.routers import admin as admin_router

    real = admin_router.invalidate
    monkeypatch.setattr(admin_router, "invalidate", lambda p, m: (seen.append("invalidated"), real(p, m)))
    original_after_commit = admin_router.after_commit

    def spy(sess, cb):
        seen.append("queued")
        original_after_commit(sess, cb)

    monkeypatch.setattr(admin_router, "after_commit", spy)
    await client.put("/admin/model-prices", json=BODY, headers=admin)
    assert seen == ["queued", "invalidated"]
