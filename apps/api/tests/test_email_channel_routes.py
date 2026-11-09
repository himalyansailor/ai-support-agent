from uuid import UUID

import pytest
from sqlalchemy import select

from kb_helpers import create_org, join_org, signup
from supportcore.db.models import AuditLog, EmailMailbox
from supportcore.mail.ports import MailAuthError
from supportcore.secrets.ports import SecretCipher

BODY = {
    "address": "support@acme.example",
    "display_name": "Acme Support",
    "imap_host": "imap.acme.example",
    "imap_username": "support-login",
    "imap_password": "imap-secret-pw",
    "smtp_host": "smtp.acme.example",
    "smtp_username": "support-login",
    "smtp_password": "smtp-secret-pw",
}
OUT_KEYS = {
    "id", "address", "display_name", "imap_host", "imap_port", "imap_security", "imap_username",
    "imap_password_set", "imap_folder", "smtp_host", "smtp_port", "smtp_security", "smtp_username",
    "smtp_password_set", "enabled", "status", "last_error_type", "last_error_at", "last_sync_at", "connected_at",
    "smtp_status", "smtp_error_type", "smtp_last_error_at",
}


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs = []

    def enqueue(self, name, payload, *, countdown=None) -> None:
        self.jobs.append((name, payload))


@pytest.fixture
def test_settings(test_settings):
    return test_settings.model_copy(update={"mail_inbound": "fake", "mail_outbound": "fake"})


@pytest.fixture
async def setup(raw_client):
    headers = await signup(raw_client, "owner@x.com")
    org_id = await create_org(raw_client, headers)
    return raw_client, headers, f"/orgs/{org_id}/channels/email", org_id


@pytest.fixture(autouse=True)
def _truncate(session_factory):
    """Real-commit tests: session_factory truncates every table at teardown."""


async def test_password_encrypted_and_never_returned(setup, session_factory):
    client, headers, url, _ = setup
    assert (await client.get(url, headers=headers)).json() is None
    r = await client.put(url, json=BODY, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == OUT_KEYS and body["imap_password_set"] is True and body["smtp_password_set"] is True
    assert (body["imap_port"], body["smtp_port"], body["status"]) == (993, 587, "active")
    async with session_factory() as s:
        box = (await s.execute(select(EmailMailbox))).scalar_one()
    for text in (r.text, (await client.get(url, headers=headers)).text):
        assert "imap-secret-pw" not in text and "smtp-secret-pw" not in text
        assert box.imap_password_encrypted not in text and box.smtp_password_encrypted not in text
    assert box.imap_password_encrypted != "imap-secret-pw"


async def test_settings_show_each_side_health(setup, session_factory):
    """The receiving (IMAP) and sending (SMTP) sides report their health separately."""
    from datetime import UTC, datetime

    from sqlalchemy import update

    client, headers, url, _ = setup
    body = (await client.put(url, json=BODY, headers=headers)).json()
    assert (body["smtp_status"], body["smtp_error_type"], body["smtp_last_error_at"]) == ("active", None, None)
    async with session_factory() as s:
        await s.execute(
            update(EmailMailbox).values(smtp_status="auth_failed", smtp_error_type="auth_failed", smtp_last_error_at=datetime.now(UTC))
        )
        await s.commit()

    got = (await client.get(url, headers=headers)).json()

    assert (got["status"], got["last_error_type"]) == ("active", None)
    assert (got["smtp_status"], got["smtp_error_type"]) == ("auth_failed", "auth_failed") and got["smtp_last_error_at"]


async def test_update_without_password_keeps_it(setup, session_factory):
    client, headers, url, _ = setup
    await client.put(url, json=BODY, headers=headers)
    async with session_factory() as s:
        before = (await s.execute(select(EmailMailbox.imap_password_encrypted))).scalar_one()
    update = {k: v for k, v in BODY.items() if not k.endswith("password")}
    r = await client.put(url, json={**update, "display_name": "Renamed"}, headers=headers)
    assert r.status_code == 200 and r.json()["display_name"] == "Renamed" and r.json()["imap_password_set"] is True
    async with session_factory() as s:
        assert (await s.execute(select(EmailMailbox.imap_password_encrypted))).scalar_one() == before


async def test_create_requires_passwords_422(setup):
    client, headers, url, _ = setup
    r = await client.put(url, json={k: v for k, v in BODY.items() if k != "smtp_password"}, headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "invalid_mailbox_settings"
    r = await client.put(url, json={**BODY, "imap_port": 0}, headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "invalid_mailbox_settings"


async def test_test_connection_reports_each_side(setup, app):
    client, headers, url, _ = setup
    await client.put(url, json=BODY, headers=headers)
    app.state.container.adapters.inbound_mailbox.fail_with(MailAuthError())
    r = await client.post(f"{url}/test", json={}, headers=headers)
    assert r.status_code == 200
    assert r.json() == {"imap": {"ok": False, "error_type": "auth_failed"}, "smtp": {"ok": True, "error_type": None}}
    assert "secret" not in r.text


async def test_routes_require_channels_manage(setup, session_factory):
    client, headers, url, org_id = setup
    agent = await join_org(client, session_factory, UUID(org_id), "agent@x.com", role_name="Agent")
    assert (await client.get(url, headers=agent)).status_code == 403
    assert (await client.put(url, json=BODY, headers=agent)).status_code == 403
    assert (await client.delete(url, headers=agent)).status_code == 403
    assert (await client.post(f"{url}/test", json={}, headers=agent)).status_code == 403
    assert (await client.post(f"{url}/sync", headers=agent)).status_code == 403
    role = await client.post(
        f"/orgs/{org_id}/roles", json={"name": "Chan", "permissions": ["channels.manage"]}, headers=headers
    )
    member = await join_org(client, session_factory, UUID(org_id), "chan@x.com", role_id=UUID(role.json()["id"]))
    assert (await client.get(url, headers=member)).status_code == 200


async def test_cross_tenant_404(setup):
    client, headers, url, _ = setup
    await client.put(url, json=BODY, headers=headers)
    other = await signup(client, "other@x.com")
    other_org = await create_org(client, other, "Other")
    assert (await client.get(url, headers=other)).status_code == 404
    assert (await client.put(url, json=BODY, headers=other)).status_code == 404
    assert (await client.delete(url, headers=other)).status_code == 404
    assert (await client.post(f"{url}/sync", headers=other)).status_code == 404
    # The other org's own mailbox is unaffected.
    assert (await client.get(f"/orgs/{other_org}/channels/email", headers=other)).json() is None


async def test_disconnect(setup):
    client, headers, url, _ = setup
    await client.put(url, json=BODY, headers=headers)
    assert (await client.delete(url, headers=headers)).status_code == 204
    assert (await client.get(url, headers=headers)).json() is None
    assert (await client.delete(url, headers=headers)).status_code == 404


async def test_audit_rows_have_no_secrets_or_addresses(setup, session_factory):
    client, headers, url, _ = setup
    await client.put(url, json=BODY, headers=headers)
    await client.put(url, json={**BODY, "imap_host": "imap2.acme.example"}, headers=headers)
    await client.delete(url, headers=headers)
    async with session_factory() as s:
        rows = (await s.execute(select(AuditLog).where(AuditLog.action.like("mailbox.%")))).scalars().all()
    assert sorted(r.action for r in rows) == ["mailbox.connected", "mailbox.disconnected", "mailbox.updated"]
    dump = repr([r.__dict__ for r in rows]).lower()
    for forbidden in ("secret", "acme", "support-login", "imap2", "@"):
        assert forbidden not in dump


async def test_sync_now_rate_limited(setup, app):
    client, headers, url, _ = setup
    await client.put(url, json=BODY, headers=headers)
    queue = RecordingQueue()
    app.state.container.adapters.job_queue = queue
    r = await client.post(f"{url}/sync", headers=headers)
    assert r.status_code == 202
    r = await client.post(f"{url}/sync", headers=headers)
    assert r.status_code == 429 and r.json()["code"] == "rate_limited" and "Retry-After" in r.headers
    assert len(queue.jobs) == 1 and queue.jobs[0][0] == "sync_mailbox"
    assert set(queue.jobs[0][1]) == {"organisation_id", "mailbox_id"}


async def test_host_change_requires_password(setup):
    client, headers, url, _ = setup
    await client.put(url, json=BODY, headers=headers)
    moved = {k: v for k, v in BODY.items() if not k.endswith("password")} | {"imap_host": "evil.example.com"}
    r = await client.put(url, json=moved, headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "invalid_mailbox_settings"
    assert "imap_password" in r.json()["detail"] and "evil" not in r.json()["detail"]
    r = await client.post(f"{url}/test", json={"imap_host": "evil.example.com"}, headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "invalid_mailbox_settings"
    r = await client.put(url, json={**moved, "imap_password": "typed-again"}, headers=headers)
    assert r.status_code == 200 and r.json()["imap_host"] == "evil.example.com"


async def test_test_connection_rate_limited(setup):
    client, headers, url, _ = setup
    await client.put(url, json=BODY, headers=headers)
    for _ in range(5):
        assert (await client.post(f"{url}/test", json={}, headers=headers)).status_code == 200
    r = await client.post(f"{url}/test", json={}, headers=headers)
    assert r.status_code == 429 and r.json()["code"] == "rate_limited"


async def test_prod_refusals_over_http(setup, app, monkeypatch):
    client, headers, url, _ = setup
    container = app.state.container
    monkeypatch.setattr(container, "settings", container.settings.model_copy(update={"env": "prod"}))
    for body in ({**BODY, "smtp_security": "none"}, {**BODY, "imap_host": "10.0.0.5"}):
        r = await client.put(url, json=body, headers=headers)
        assert r.status_code == 422 and r.json()["code"] == "invalid_mailbox_settings", r.text
    r = await client.post(f"{url}/test", json={"imap_host": "169.254.169.254"}, headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "invalid_mailbox_settings"
