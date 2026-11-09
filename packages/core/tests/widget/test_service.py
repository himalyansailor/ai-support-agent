import json
import re
from uuid import uuid4

import pytest
from sqlalchemy import select

from supportcore.db.models import AuditLog, KnowledgeBase, Organisation
from supportcore.errors import NotFoundError
from supportcore.widget.domains import InvalidDomainError
from supportcore.widget.service import (
    IdentitySecretRequiredError,
    InvalidAppearanceError,
    TooManyDomainsError,
    WidgetService,
)


async def _audit(session, org_id, action=None) -> list[AuditLog]:
    stmt = select(AuditLog).where(AuditLog.organisation_id == org_id).order_by(AuditLog.created_at)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    return list((await session.execute(stmt)).scalars())


async def _kb(session, org_id, name="Docs") -> KnowledgeBase:
    kb = KnowledgeBase(
        organisation_id=org_id, name=name, embedding_provider="fake", embedding_model="fake-embedding",
        embedding_dimensions=8, chunking={"strategy": "recursive", "chunk_size": 1000, "chunk_overlap": 150},
    )
    session.add(kb)
    await session.flush()
    return kb


async def test_create_generates_unique_public_key_prefix_wk(widgets, owner, session, org):
    a = await widgets.create("Site", owner)
    b = await widgets.create("Site 2", owner)
    assert re.fullmatch(r"wk_[A-Za-z0-9_-]{24}", a.public_key)
    assert a.public_key != b.public_key
    assert (a.enabled, a.csat_enabled, a.identity_required, a.allowed_domains) == (True, True, False, [])
    assert a.appearance == {
        "primary_color": "#4f46e5", "position": "bottom-right", "launcher": "bubble", "logo_url": None, "greeting": None,
    }
    assert a.encrypted_identity_secret is None and a.created_by_user_id == owner.id
    rows = await _audit(session, org.id, "widget.created")
    assert [r.target_id for r in rows] == [str(a.id), str(b.id)] and rows[0].metadata_["name"] == "Site"


async def test_create_validates_name(widgets, owner):
    with pytest.raises(InvalidAppearanceError):
        await widgets.create("   ", owner)
    with pytest.raises(InvalidAppearanceError):
        await widgets.create("x" * 101, owner)


async def test_get_list_and_cross_tenant(widgets, owner, session, org, bus, cipher):
    w = await widgets.create("Site", owner)
    assert [x.id for x in await widgets.list()] == [w.id]
    other = Organisation(name="Other", slug="other")
    session.add(other)
    await session.flush()
    foreign = WidgetService(session, other.id, bus, cipher)
    with pytest.raises(NotFoundError):
        await foreign.get(w.id)
    assert await foreign.list() == []
    with pytest.raises(NotFoundError):
        await widgets.get(uuid4())


async def test_update_fields_and_audit_diff(widgets, owner, session, org):
    w = await widgets.create("Site", owner)
    await widgets.update(
        w.id,
        name="Shop",
        enabled=False,
        allowed_domains=["HTTPS://Shop.Example.com/", "shop.example.com", "*.Example.com"],
        appearance={"primary_color": "#AABBCC", "greeting": "Hi!"},
        csat_enabled=False,
        actor=owner,
    )
    w = await widgets.get(w.id)
    assert (w.name, w.enabled, w.csat_enabled) == ("Shop", False, False)
    assert w.allowed_domains == ["shop.example.com", "*.example.com"]  # normalised, de-duplicated
    assert w.appearance["primary_color"] == "#aabbcc" and w.appearance["greeting"] == "Hi!"
    assert w.appearance["position"] == "bottom-right"  # merged over the current appearance
    [row] = await _audit(session, org.id, "widget.updated")
    assert set(row.metadata_["changes"]) == {"name", "enabled", "allowed_domains", "appearance", "csat_enabled"}
    json.dumps(row.metadata_)
    # No-op update emits nothing.
    await widgets.update(w.id, name="Shop", actor=owner)
    assert len(await _audit(session, org.id, "widget.updated")) == 1


@pytest.mark.parametrize(
    "appearance",
    [
        {"nope": 1},
        {"primary_color": "red"},
        {"primary_color": "#12345"},
        {"position": "top-left"},
        {"launcher": "hidden"},
        {"logo_url": "http://x.com/a.png"},
        {"logo_url": "javascript:alert(1)"},
        {"greeting": "g" * 501},
        {"greeting": 5},
    ],
)
async def test_update_rejects_bad_appearance(widgets, owner, appearance):
    w = await widgets.create("Site", owner)
    with pytest.raises(InvalidAppearanceError):
        await widgets.update(w.id, appearance=appearance, actor=owner)


async def test_update_domain_errors(widgets, owner):
    w = await widgets.create("Site", owner)
    with pytest.raises(InvalidDomainError):
        await widgets.update(w.id, allowed_domains=["*"], actor=owner)
    with pytest.raises(TooManyDomainsError):
        await widgets.update(w.id, allowed_domains=[f"s{i}.example.com" for i in range(21)], actor=owner)
    await widgets.update(w.id, allowed_domains=[f"s{i}.example.com" for i in range(20)], actor=owner)


async def test_update_links_knowledge_bases(widgets, owner, session, org):
    w = await widgets.create("Site", owner)
    kb1, kb2 = await _kb(session, org.id, "A"), await _kb(session, org.id, "B")
    await widgets.update(w.id, knowledge_base_ids=[kb1.id, kb2.id, kb1.id], actor=owner)
    assert set(await widgets.knowledge_base_ids(w.id)) == {kb1.id, kb2.id}
    await widgets.update(w.id, knowledge_base_ids=[kb2.id], actor=owner)
    assert await widgets.knowledge_base_ids(w.id) == [kb2.id]
    await widgets.update(w.id, knowledge_base_ids=[], actor=owner)
    assert await widgets.knowledge_base_ids(w.id) == []


async def test_update_rejects_kb_from_other_org_404(widgets, owner, session):
    w = await widgets.create("Site", owner)
    other = Organisation(name="Other", slug="other")
    session.add(other)
    await session.flush()
    foreign_kb = await _kb(session, other.id)
    with pytest.raises(NotFoundError):
        await widgets.update(w.id, knowledge_base_ids=[foreign_kb.id], actor=owner)
    assert await widgets.knowledge_base_ids(w.id) == []


async def test_rotate_secret_returns_plaintext_once_and_stores_ciphertext(widgets, owner, session, org, cipher):
    w = await widgets.create("Site", owner)
    w, secret = await widgets.rotate_identity_secret(w.id, owner)
    assert re.fullmatch(r"wsec_[A-Za-z0-9_-]{43}", secret)
    assert w.encrypted_identity_secret and w.encrypted_identity_secret != secret
    assert secret not in w.encrypted_identity_secret
    assert cipher.decrypt(w.encrypted_identity_secret) == secret
    assert w.identity_secret_last4 == secret[-4:]
    [row] = await _audit(session, org.id, "widget.identity_secret_rotated")
    assert secret not in json.dumps(row.metadata_) and w.encrypted_identity_secret not in json.dumps(row.metadata_)
    assert secret[-4:] not in json.dumps(row.metadata_)
    # Rotating again replaces it.
    w2, secret2 = await widgets.rotate_identity_secret(w.id, owner)
    assert secret2 != secret and cipher.decrypt(w2.encrypted_identity_secret) == secret2
    # No update audit row ever carries the secret.
    for r in await _audit(session, org.id):
        assert secret not in json.dumps(r.metadata_) and secret2 not in json.dumps(r.metadata_)


async def test_identity_required_without_secret_422(widgets, owner):
    w = await widgets.create("Site", owner)
    with pytest.raises(IdentitySecretRequiredError):
        await widgets.update(w.id, identity_required=True, actor=owner)
    await widgets.rotate_identity_secret(w.id, owner)
    await widgets.update(w.id, identity_required=True, actor=owner)
    assert (await widgets.get(w.id)).identity_required is True


async def test_get_by_public_key_only_enabled(widgets, owner, session):
    w = await widgets.create("Site", owner)
    found = await WidgetService.get_by_public_key(session, w.public_key)
    assert found is not None and found.id == w.id
    assert await WidgetService.get_by_public_key(session, "wk_unknown") is None
    await widgets.update(w.id, enabled=False, actor=owner)
    assert await WidgetService.get_by_public_key(session, w.public_key) is None


async def test_delete_removes_widget_links_and_audits(widgets, owner, session, org):
    w = await widgets.create("Site", owner)
    kb = await _kb(session, org.id)
    await widgets.update(w.id, knowledge_base_ids=[kb.id], actor=owner)
    await widgets.delete(w.id, owner)
    with pytest.raises(NotFoundError):
        await widgets.get(w.id)
    await session.refresh(kb)  # the KB itself survives
    [row] = await _audit(session, org.id, "widget.deleted")
    assert row.target_id == str(w.id)
