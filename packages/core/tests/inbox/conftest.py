"""Builders for the inbox tests. Everything is flushed in the rollback-isolated `session`."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from supportcore.db.models import (
    Conversation,
    Customer,
    KnowledgeBase,
    Membership,
    Message,
    Role,
    User,
    Widget,
    WidgetKnowledgeBase,
)
from supportcore.inbox.postgres_search import PostgresConversationSearch
from supportcore.inbox.service import InboxService

BASE_TIME = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class Builder:
    def __init__(self, session, org) -> None:
        self.session = session
        self.org = org
        self.clock = 0

    async def member(self, name="Ann", permissions=("queries.answer",)):
        user = User(email=f"{uuid.uuid4().hex[:8]}@staff.test", password_hash="x", name=name)
        role = Role(organisation_id=self.org.id, name=f"r-{uuid.uuid4().hex[:6]}", permissions=list(permissions))
        self.session.add_all([user, role])
        await self.session.flush()
        membership = Membership(organisation_id=self.org.id, user_id=user.id, role_id=role.id)
        self.session.add(membership)
        await self.session.flush()
        return membership

    async def customer(self, name=None, email=None, external_id=None):
        c = Customer(
            organisation_id=self.org.id, visitor_id=uuid.uuid4().hex, name=name, email=email, external_id=external_id
        )
        self.session.add(c)
        await self.session.flush()
        return c

    async def conversation(self, customer=None, texts=(), **fields):
        """`texts`: (role, text) pairs, one minute apart; last_message_at follows the last one."""
        customer = customer or await self.customer()
        self.clock += 1
        fields.setdefault("last_message_at", BASE_TIME + timedelta(hours=self.clock))
        conversation = Conversation(organisation_id=self.org.id, customer_id=customer.id, **fields)
        self.session.add(conversation)
        await self.session.flush()
        for i, (role, text) in enumerate(texts):
            self.session.add(
                Message(
                    organisation_id=self.org.id,
                    conversation_id=conversation.id,
                    role=role,
                    text=text,
                    created_at=BASE_TIME + timedelta(hours=self.clock, minutes=i),
                )
            )
        await self.session.flush()
        return conversation

    async def widget_with_kb(self):
        kb = KnowledgeBase(
            organisation_id=self.org.id,
            name=f"kb-{uuid.uuid4().hex[:6]}",
            embedding_provider="fake",
            embedding_model="fake-embedding",
            embedding_dimensions=8,
            chunking={},
        )
        widget = Widget(organisation_id=self.org.id, name="W", public_key=uuid.uuid4().hex)
        self.session.add_all([kb, widget])
        await self.session.flush()
        self.session.add(WidgetKnowledgeBase(widget_id=widget.id, knowledge_base_id=kb.id))
        await self.session.flush()
        return widget, kb


@pytest.fixture
async def org_a_committed(session_factory):
    """(organisation id, conversation id) really committed (session_factory truncates at teardown)."""
    from supportcore.db.models import Organisation

    async with session_factory() as s:
        org = Organisation(name="Org C", slug="org-c")
        s.add(org)
        await s.flush()
        b = Builder(s, org)
        conversation = await b.conversation(status="open")
        await s.commit()
        return org.id, conversation.id


@pytest.fixture
def adapters():
    return SimpleNamespace(conversation_search=PostgresConversationSearch())


@pytest.fixture
def build(session, org_a):
    return Builder(session, org_a)


@pytest.fixture
def service(session, org_a, adapters):
    return InboxService(session, org_a.id, adapters)


@pytest.fixture
def make_service(session, adapters):
    return lambda org: InboxService(session, org.id, adapters)


__all__ = ["BASE_TIME", "Builder", "select"]
