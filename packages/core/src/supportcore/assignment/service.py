"""Who handles a conversation: routing an escalation to the team (round-robin over the available agents),
claiming and (re)assigning.

`route_to_team` is the single entry point for a conversation leaving the bot for the team (the bot graph's
handoff node and the widget's "talk to a person" both call it). The round-robin pointer is read and moved under
a `FOR UPDATE` lock on the org's support settings row, in the transaction that assigns, so concurrent
escalations go to different agents. Events go out after the commit (InboxPublisher). Logs carry ids only.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.analytics.events import record
from supportcore.config import Settings
from supportcore.conversations.service import ConversationService, InvalidConversationStateError
from supportcore.db.models import Conversation, Membership, Role, SupportSettings, User
from supportcore.db.session import transaction_info
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import DomainError, NotFoundError
from supportcore.events import EventBus
from supportcore.inbox.events import agent_event, handoff_event, message_payload
from supportcore.inbox.notifications import NotificationService
from supportcore.inbox.publisher import InboxPublisher
from supportcore.inbox.sla import start_cycle
from supportcore.observability.logging import get_logger
from supportcore.rbac.checker import has_permission
from supportcore.rbac.permissions import Permission
from supportcore.assignment.availability import available_agents
from supportcore.support.settings_service import SupportSettingsService
from supportcore.widget.strings import strings_for

if TYPE_CHECKING:
    from supportcore.wiring import Adapters

__all__ = ["AssigneeNotEligibleError", "AssignmentResult", "AssignmentService"]

logger = get_logger(__name__)

# Statuses a conversation can be claimed or assigned in; a `bot` one moves to `open`.
_ASSIGNABLE = ("bot", "open", "pending")


class AssigneeNotEligibleError(DomainError):
    def __init__(self) -> None:
        super().__init__("That team member cannot be assigned conversations")


@dataclass(frozen=True)
class AssignmentResult:
    assignee: Membership | None
    reason: str  # "assigned" or "no_agent_available"


class AssignmentService:
    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        events: EventBus,
        adapters: "Adapters",
        settings: Settings,
        *,
        publisher: InboxPublisher,
    ) -> None:
        self._session = session
        self._org_id = organisation_id
        self._events = events
        self._adapters = adapters
        self._settings = settings
        self._publisher = publisher
        self._conversations = TenantScopedRepository(session, Conversation, organisation_id)
        self._memberships = TenantScopedRepository(session, Membership, organisation_id)

    # --- escalation ----------------------------------------------------------------------------------

    async def route_to_team(
        self, conversation: Conversation, decision: Mapping[str, Any], *, announce: bool = True
    ) -> AssignmentResult:
        """Hands `conversation` (locked by the caller, in `bot` status) to the team: `open` (priority `high` for a
        risk escalation), the next available agent by round-robin (or nobody), SLA due dates from now, the
        `agent_joined` / `no_agent_available` system message, and after the commit the inbox upsert, the
        assignee's notification and the widget's message + `handoff` events. With `announce=False` (an email
        conversation: nobody is watching a widget) there is no system message and no widget event."""
        conversations = ConversationService(self._session, self._org_id, self._events)
        await conversations.escalate(conversation, decision)
        self._record_entered_team(conversation, decision)
        # Presence (Redis) is read before the lock, so the settings row is never held across it.
        present = await self._adapters.presence.present(self._org_id)
        support = SupportSettingsService(self._session, self._org_id, self._events)
        row = await support.get(for_update=True)  # serialises the pointer across concurrent escalations
        now = datetime.now(UTC)
        candidates = await available_agents(
            self._session, self._org_id, self._adapters.presence, now, present=present
        )
        assignee = await self._adapters.assignment.pick(candidates, await self._anchor(candidates, row))
        if assignee is not None:
            row.last_assigned_membership_id = assignee.id
            conversation.assignee_membership_id = assignee.id
            self._record_assigned(conversation, assignee.id)
        self._start_sla(conversation, row, await support.calendar(), now)
        if announce:
            name = await self._display_name(assignee) if assignee is not None else None
            strings = strings_for(conversation.language)
            text = strings["agent_joined"].format(name=name) if name is not None else strings["no_agent_available"]
            system = await conversations.add_message(conversation, "system", text)
        self._publisher.conversation_changed(self._session, self._org_id, conversation.id)
        if assignee is not None:
            await self._notify_assigned(conversation, assignee.id, None)
        if announce:
            self._publisher.widget(
                self._session,
                conversation.id,
                [
                    {"type": "message", "message": message_payload(system)},
                    agent_event(name),
                    handoff_event(conversation.status, name),
                ],
            )
        reason = "assigned" if assignee is not None else "no_agent_available"
        logger.info(
            "conversation_routed",
            conversation_id=str(conversation.id),
            assignee_membership_id=str(assignee.id) if assignee is not None else None,
            reason=reason,
            candidates=len(candidates),
        )
        return AssignmentResult(assignee, reason)

    # --- claim and assign ----------------------------------------------------------------------------

    async def claim(self, conversation_id: UUID, actor_membership: Membership) -> Conversation:
        """The actor takes the conversation. A `bot` conversation becomes `open` (the bot only answers in `bot`)."""
        await self._ensure_eligible(actor_membership.id)
        conversation = await self._locked(conversation_id)
        await self._set_assignee(conversation, actor_membership, actor_membership.id)
        return conversation

    async def assign(self, conversation_id: UUID, membership_id: UUID | None, actor: Membership) -> Conversation:
        """Assigns to `membership_id` (an active member of the org whose role has queries.answer, else
        AssigneeNotEligibleError), or unassigns with None. Assigning a `bot` conversation makes it `open`."""
        target = await self._ensure_eligible(membership_id) if membership_id is not None else None
        conversation = await self._locked(conversation_id)
        if target is None:
            if conversation.assignee_membership_id is not None:
                conversation.assignee_membership_id = None
                await self._session.flush()
                self._publisher.widget(self._session, conversation.id, [agent_event(None)])
                self._publisher.conversation_changed(self._session, self._org_id, conversation.id)
                logger.info(
                    "conversation_unassigned",
                    conversation_id=str(conversation.id),
                    actor_membership_id=str(actor.id),
                )
            return conversation
        await self._set_assignee(conversation, target, actor.id)
        return conversation

    # --- internals -----------------------------------------------------------------------------------

    async def _set_assignee(self, conversation: Conversation, assignee: Membership, actor_membership_id: UUID) -> None:
        """A different assignee gets an `agent_joined` system message (one per conversation per transaction:
        a second change in it rewrites that message). Only entering the team (bot -> open) sends the widget
        `handoff`; a reassignment within the team sends the message alone."""
        entering_team = conversation.status == "bot"
        if conversation.assignee_membership_id == assignee.id and not entering_team:
            return  # already theirs
        conversation.assignee_membership_id = assignee.id
        if entering_team:
            conversation.status = "open"
            self._record_entered_team(conversation, {"source": "claim" if assignee.id == actor_membership_id else "assign"})
            support = SupportSettingsService(self._session, self._org_id, self._events)
            self._start_sla(conversation, await support.get(), await support.calendar(), datetime.now(UTC))
        self._record_assigned(conversation, assignee.id)
        name = await self._display_name(assignee)
        text = strings_for(conversation.language)["agent_joined"].format(name=name)
        joined = transaction_info(self._session, "assignment_agent_joined")
        posted = joined.get(conversation.id)
        if posted is not None:  # changed again in this transaction: one message, naming the latest assignee
            message, event = posted
            message.text = text
            await self._session.flush()
            event["message"] = message_payload(message)  # the queued widget event is sent at commit
            self._publisher.widget(self._session, conversation.id, [agent_event(name)])
        else:
            message = await ConversationService(self._session, self._org_id, self._events).add_message(
                conversation, "system", text
            )
            event = {"type": "message", "message": message_payload(message)}
            joined[conversation.id] = (message, event)
            events = [event, agent_event(name)]
            if entering_team:
                events.append(handoff_event(conversation.status, name))
            self._publisher.widget(self._session, conversation.id, events)
        self._publisher.conversation_changed(self._session, self._org_id, conversation.id)
        await self._notify_assigned(conversation, assignee.id, actor_membership_id)
        logger.info(
            "conversation_assigned", conversation_id=str(conversation.id), assignee_membership_id=str(assignee.id)
        )

    async def _notify_assigned(
        self, conversation: Conversation, assignee_id: UUID, actor_membership_id: UUID | None
    ) -> None:
        """The assignee's `assigned` notification (none for self-assignment); the previous assignee is not told."""
        await NotificationService(self._session, self._org_id, publisher=self._publisher).create(
            kind="assigned",
            membership_id=assignee_id,
            conversation_id=conversation.id,
            actor_membership_id=actor_membership_id,
        )

    def _record_entered_team(self, conversation: Conversation, decision: Mapping[str, Any]) -> None:
        """Analytics: the conversation entered the team (a new cycle). Claims and assignments of a bot
        conversation have no rule, only a `source` of `claim` or `assign`."""
        record(
            self._session,
            self._org_id,
            conversation.id,
            "entered_team",
            channel=conversation.channel,
            data={key: decision.get(key) for key in ("rule_key", "rule_name", "action", "source")},
        )

    def _record_assigned(self, conversation: Conversation, membership_id: UUID) -> None:
        record(
            self._session,
            self._org_id,
            conversation.id,
            "assigned",
            channel=conversation.channel,
            membership_id=membership_id,
            data={"membership_id": str(membership_id)},
        )

    def _start_sla(self, conversation: Conversation, row: SupportSettings, calendar: Any, now: datetime) -> None:
        start_cycle(conversation, calendar, row.sla_targets, now, self._org_id)

    async def _locked(self, conversation_id: UUID) -> Conversation:
        conversation = await self._conversations.get(conversation_id, for_update=True)
        if conversation is None:
            raise NotFoundError("Conversation not found")
        await self._session.refresh(conversation)  # the row as of the lock
        if conversation.status not in _ASSIGNABLE:
            raise InvalidConversationStateError()
        return conversation

    async def _ensure_eligible(self, membership_id: UUID) -> Membership:
        """An active member of this org whose role has queries.answer; anything else (including another org's
        member or an unknown id) is not eligible."""
        membership = await self._memberships.get(membership_id)
        if membership is None or membership.status != "active":
            raise AssigneeNotEligibleError()
        role = await TenantScopedRepository(self._session, Role, self._org_id).get(membership.role_id)
        if role is None or not has_permission(role.permissions, Permission.QUERIES_ANSWER):
            raise AssigneeNotEligibleError()
        return membership

    async def _anchor(self, candidates: list[Membership], row: SupportSettings) -> UUID | None:
        """The candidate round-robin continues after. When the last assignee is not available now, the candidate
        just before its place in (created_at, id) order, so the next one after it is picked (None: the first)."""
        last = row.last_assigned_membership_id
        if last is None or any(m.id == last for m in candidates):
            return last
        previous = await self._memberships.get(last)
        if previous is None:
            return None
        key = (previous.created_at, previous.id)
        before = [m for m in candidates if (m.created_at, m.id) < key]
        return before[-1].id if before else None

    async def _display_name(self, membership: Membership) -> str:
        user = await self._session.get(User, membership.user_id)
        return user.name if user is not None else ""
