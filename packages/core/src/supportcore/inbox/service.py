"""Inbox reads: filtered, paged conversation lists, per-view counts, one conversation's detail and older messages.

Everything is scoped to the organisation. Search text, message text, names and emails are never logged.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import and_, exists, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.conversations.service import ConversationService
from supportcore.db.models import BotTurn, Conversation, ConversationTag, Message, WidgetKnowledgeBase
from supportcore.db.models.conversation import CONVERSATION_STATUSES, PRIORITIES
from supportcore.db.models import Customer
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import DomainError, NotFoundError
from supportcore.events import EventBus
from supportcore.inbox.customers import linked_conversations
from supportcore.inbox.mentions import mentioned_clause
from supportcore.inbox.notes import NoteService, NoteView
from supportcore.inbox.search_ports import Cursor, Page
from supportcore.inbox.summary import InboxSummary, load_call_views, load_summaries
from supportcore.mail.views import EmailMeta, email_meta_for

if TYPE_CHECKING:
    from supportcore.wiring import Adapters

__all__ = [
    "INBOX_VIEWS",
    "InboxDetail",
    "InboxService",
    "InvalidInboxFilterError",
    "InvalidSearchQueryError",
    "MAX_SEARCH_LENGTH",
    "MIN_SEARCH_LENGTH",
    "MessageWindow",
]

INBOX_VIEWS = ("mine", "unassigned", "all", "bot", "mentions")
MIN_SEARCH_LENGTH = 2
MAX_SEARCH_LENGTH = 200
DETAIL_MESSAGES = 200
# A search is answered from its newest matches: the filters and paging then apply within this window.
SEARCH_CANDIDATES = 500
_TEAM_STATUSES = ("open", "pending")


class InvalidSearchQueryError(DomainError):
    def __init__(self) -> None:
        super().__init__(f"A search must be {MIN_SEARCH_LENGTH}-{MAX_SEARCH_LENGTH} characters")


class InvalidInboxFilterError(DomainError):
    def __init__(self, message: str = "That inbox filter is not valid") -> None:
        super().__init__(message)


@dataclass(frozen=True)
class MessageWindow:
    """Messages oldest first; `has_more` when older ones exist."""

    items: list[Message]
    has_more: bool
    emails: dict[UUID, EmailMeta]  # by message id; only messages that are emails


@dataclass(frozen=True)
class InboxDetail:
    summary: InboxSummary
    messages: list[Message]
    has_more: bool
    notes: list[NoteView]  # oldest first; the team's only, never sent to the widget
    turns: list[BotTurn]
    chunks: dict[UUID, list[dict[str, Any]]]
    history: list[InboxSummary]  # the customer's other conversations (see inbox.customers)
    emails: dict[UUID, EmailMeta]  # email metadata by message id
    calls: list[dict[str, Any]] = field(default_factory=list)  # every call, oldest first (voice conversations)


def _clean_query(q: str) -> str:
    q = q.strip()
    if not MIN_SEARCH_LENGTH <= len(q) <= MAX_SEARCH_LENGTH:
        raise InvalidSearchQueryError()
    return q


class InboxService:
    def __init__(self, session: AsyncSession, organisation_id: UUID, adapters: "Adapters") -> None:
        self._session = session
        self._org_id = organisation_id
        self._adapters = adapters
        self._conversations = TenantScopedRepository(session, Conversation, organisation_id)
        self._messages = TenantScopedRepository(session, Message, organisation_id)

    # --- lists -------------------------------------------------------------------------------------

    @staticmethod
    def _view_clauses(view: str, viewer_membership_id: UUID | None, *, with_status: bool = True) -> list[Any]:
        """The conditions of a view; `with_status=False` leaves out its status condition."""
        if view == "mentions":  # a note mentions the viewer; any status
            if viewer_membership_id is None:
                return [Conversation.id.is_(None)]
            return [mentioned_clause(viewer_membership_id)]
        if view == "bot":
            return [Conversation.status == "bot"] if with_status else []
        if view == "mine":
            clauses: list[Any] = [
                Conversation.assignee_membership_id == viewer_membership_id
                if viewer_membership_id is not None
                else Conversation.id.is_(None)  # not a member: nothing is theirs
            ]
        elif view == "unassigned":
            clauses = [Conversation.assignee_membership_id.is_(None)]
        else:
            return []
        return clauses + [Conversation.status.in_(_TEAM_STATUSES)] if with_status else clauses

    async def list(
        self,
        view: str,
        *,
        status: str | None = None,
        channel: str | None = None,
        assignee: str | None = None,
        priority: str | None = None,
        tag_id: UUID | None = None,
        kb_id: UUID | None = None,
        include_test: bool = False,
        q: str | None = None,
        callback: str | None = None,
        cursor: Cursor | None = None,
        limit: int = 25,
        viewer_membership_id: UUID | None = None,
    ) -> Page[InboxSummary]:
        """Newest activity first, keyset-paged on (last_message_at, id).

        `view` sets a base filter (mine / unassigned: open and pending; bot: bot; mentions: a note mentions the
        viewer, any status). An explicit `status` replaces the
        view's status. `assignee` is `me`, `none` or a membership id. With `q`, only the newest SEARCH_CANDIDATES
        matches are listed.
        `callback=requested` lists the voice conversations still waiting for a call back (the partial index).
        """
        if view not in INBOX_VIEWS:
            raise InvalidInboxFilterError("Unknown inbox view")
        if status is not None and status not in CONVERSATION_STATUSES:
            raise InvalidInboxFilterError("Unknown status")
        if priority is not None and priority not in PRIORITIES:
            raise InvalidInboxFilterError("Unknown priority")
        if callback is not None and callback != "requested":
            raise InvalidInboxFilterError("The callback filter can only be requested")
        query = _clean_query(q) if q is not None else None
        stmt = self._conversations.select(Conversation.id, Conversation.last_message_at).where(
            *self._view_clauses(view, viewer_membership_id, with_status=status is None)
        )
        if status is not None:
            stmt = stmt.where(Conversation.status == status)
        if channel is not None:
            stmt = stmt.where(Conversation.channel == channel)
        if callback is not None:
            stmt = stmt.where(Conversation.callback_status == "requested")
        if priority is not None:
            stmt = stmt.where(Conversation.priority == priority)
        if assignee is not None:
            stmt = stmt.where(self._assignee_clause(assignee, viewer_membership_id))
        if tag_id is not None:
            stmt = stmt.where(
                exists().where(ConversationTag.conversation_id == Conversation.id, ConversationTag.tag_id == tag_id)
            )
        if kb_id is not None:
            stmt = stmt.where(
                Conversation.widget_id.in_(
                    select(WidgetKnowledgeBase.widget_id).where(WidgetKnowledgeBase.knowledge_base_id == kb_id)
                )
            )
        if not include_test:
            stmt = stmt.where(Conversation.is_test.is_(False))
        if query is not None:
            matches = await self._adapters.conversation_search.search(
                self._session, self._org_id, query, include_test=include_test, limit=SEARCH_CANDIDATES, cursor=None
            )
            stmt = stmt.where(Conversation.id.in_(matches.items))
        if cursor is not None:
            stmt = stmt.where(tuple_(Conversation.last_message_at, Conversation.id) < tuple_(*cursor))
        stmt = stmt.order_by(Conversation.last_message_at.desc(), Conversation.id.desc()).limit(limit + 1)
        rows = (await self._session.execute(stmt)).all()
        page = rows[:limit]
        next_cursor = (page[-1][1], page[-1][0]) if len(rows) > limit else None
        return Page(await load_summaries(self._session, self._org_id, [row[0] for row in page]), next_cursor)

    @staticmethod
    def _assignee_clause(assignee: str, viewer_membership_id: UUID | None) -> Any:
        if assignee == "none":
            return Conversation.assignee_membership_id.is_(None)
        if assignee == "me":
            if viewer_membership_id is None:
                return Conversation.id.is_(None)
            return Conversation.assignee_membership_id == viewer_membership_id
        try:
            return Conversation.assignee_membership_id == UUID(assignee)
        except ValueError:
            raise InvalidInboxFilterError("The assignee must be me, none or a membership id") from None

    async def counts(self, viewer_membership_id: UUID | None) -> dict[str, int]:
        """Open + pending conversations that are the viewer's (`mine`) or nobody's (`unassigned`), bot ones, and the
        callbacks still to make. Like the `callback=requested` filter, the callbacks count ignores the conversation's
        status: a resolved conversation with a callback pending still counts."""
        mine = and_(*self._view_clauses("mine", viewer_membership_id))
        unassigned = and_(*self._view_clauses("unassigned", None))
        bot = and_(*self._view_clauses("bot", None))
        stmt = self._conversations.select(
            func.count().filter(mine),
            func.count().filter(unassigned),
            func.count().filter(bot),
            func.count().filter(Conversation.callback_status == "requested"),
        ).where(Conversation.is_test.is_(False))
        n_mine, n_unassigned, n_bot, n_callbacks = (await self._session.execute(stmt)).one()
        return {"mine": n_mine, "unassigned": n_unassigned, "bot": n_bot, "callbacks": n_callbacks}

    # --- one conversation --------------------------------------------------------------------------

    async def summary(self, conversation_id: UUID) -> InboxSummary:
        found = await load_summaries(self._session, self._org_id, [conversation_id])
        if not found:
            raise NotFoundError("Conversation not found")
        return found[0]

    async def detail(self, conversation_id: UUID) -> InboxDetail:
        summary = await self.summary(conversation_id)
        window = await self._message_window(conversation_id, before=None, limit=DETAIL_MESSAGES)
        turns, chunks = await ConversationService(self._session, self._org_id, EventBus()).turn_traces(conversation_id)
        customer = (
            await self._session.execute(
                TenantScopedRepository(self._session, Customer, self._org_id)
                .select()
                .where(Customer.id == summary.customer["id"])
            )
        ).scalar_one()
        history = [h for h in await linked_conversations(self._session, self._org_id, customer) if h.id != summary.id]
        notes = await NoteService(self._session, self._org_id).list(conversation_id)
        calls = await load_call_views(self._session, self._org_id, conversation_id) if summary.call is not None else []
        return InboxDetail(summary, window.items, window.has_more, notes, turns, chunks, history, window.emails, calls)

    async def messages_before(self, conversation_id: UUID, before: datetime, limit: int = 100) -> MessageWindow:
        """Messages created before `before`: the newest `limit` of them, oldest first."""
        if await self._conversations.get(conversation_id) is None:
            raise NotFoundError("Conversation not found")
        return await self._message_window(conversation_id, before=before, limit=limit)

    async def _message_window(self, conversation_id: UUID, *, before: datetime | None, limit: int) -> MessageWindow:
        stmt = self._messages.select().where(Message.conversation_id == conversation_id)
        if before is not None:
            stmt = stmt.where(Message.created_at < before)
        stmt = stmt.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit + 1)
        rows = list((await self._session.execute(stmt)).scalars())
        items = list(reversed(rows[:limit]))
        emails = await email_meta_for(self._session, self._org_id, [m.id for m in items])
        return MessageWindow(items, len(rows) > limit, emails)
