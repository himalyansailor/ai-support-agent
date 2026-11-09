"""Helpers for the knowledge base / document route tests (real commits, see conftest `kb_client`)."""

from sqlalchemy import select

from supportcore.db.models import Membership, Role, User

PASSWORD = "correct-horse-1"
FAKE_EMBEDDING = {"provider": "fake", "model": "fake-embedding"}
MARKDOWN = b"# Refunds\n\n" + b"Customers may request a refund within thirty days of purchase.\n\n" * 6


async def signup(client, email: str) -> dict[str, str]:
    r = await client.post("/auth/signup", json={"email": email, "password": PASSWORD, "name": email})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def create_org(client, headers, name: str = "Acme") -> str:
    r = await client.post("/orgs", json={"name": name}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def create_kb(client, org_id, headers, name: str = "Docs", **overrides) -> dict:
    body = {"name": name, "embedding": FAKE_EMBEDDING, **overrides}
    r = await client.post(f"/orgs/{org_id}/kbs", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


async def upload(client, org_id, kb_id, headers, filename="refunds.md", data=MARKDOWN):
    return await client.post(
        f"/orgs/{org_id}/kbs/{kb_id}/documents", files={"file": (filename, data)}, headers=headers
    )


async def join_org(client, session_factory, org_id, email: str, *, role_name: str | None = None, role_id=None):
    """Signs `email` up and adds it to the org with a system role (by name) or a custom role id."""
    headers = await signup(client, email)
    async with session_factory() as s:
        user = (await s.execute(select(User).where(User.email == email))).scalar_one()
        if role_id is None:
            role_id = (
                await s.execute(select(Role.id).where(Role.organisation_id == org_id, Role.name == role_name))
            ).scalar_one()
        s.add(Membership(organisation_id=org_id, user_id=user.id, role_id=role_id))
        await s.commit()
    return headers
