from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.ai.events import AISettingsUpdated, CredentialCreated, CredentialDeleted, CredentialEvent
from supportcore.audit.service import AuditService
from supportcore.billing.events import BillingAccountChanged
from supportcore.events import DomainEvent, EventBus
from supportcore.mail.events import MailboxConnected, MailboxDisconnected, MailboxUpdated
from supportcore.voice.events import (
    PhoneNumberAdded,
    PhoneNumberReleased,
    TelephonyConnected,
    TelephonyDisconnected,
    VoiceSettingsUpdated,
)
from supportcore.knowledge.events import (
    DocumentDeleted,
    DocumentEvent,
    DocumentUploaded,
    IndexingStarted,
    KnowledgeBaseCreated,
    KnowledgeBaseDeleted,
    KnowledgeBaseUpdated,
)
from supportcore.orgs.events import OrganisationCreated, OrganisationUpdated
from supportcore.risk.events import RiskRuleCreated, RiskRuleDeleted, RiskRuleUpdated
from supportcore.support.events import SupportSettingsUpdated
from supportcore.roles.events import RoleCreated, RoleDeleted, RoleEvent, RoleUpdated
from supportcore.tools.events import BusinessToolCreated, BusinessToolDeleted, BusinessToolUpdated
from supportcore.team.events import InviteAccepted, InviteRevoked, MemberInvited, MemberUpdated
from supportcore.widget.events import WidgetCreated, WidgetDeleted, WidgetIdentitySecretRotated, WidgetUpdated

SessionProvider = Callable[[], AsyncSession]


def register_audit_subscribers(bus: EventBus, session_provider: SessionProvider) -> None:
    """Audit every business-facing event. propagate=True: a failed audit write fails the request."""

    def subscribe(event_type: type[DomainEvent], action: str, describe: Callable[[Any], tuple[str, str | None, dict]]):
        async def handler(event: DomainEvent) -> None:
            target_type, target_id, metadata = describe(event)
            await AuditService(session_provider()).record(event, action, target_type, target_id, metadata)

        handler.__qualname__ = f"audit.{action}"
        bus.subscribe(event_type, handler, propagate=True)

    def role(e: RoleEvent) -> tuple[str, str | None, dict]:
        return "role", str(e.role_id), {"role_name": e.role_name, "permissions": list(e.permissions)}

    def credential(e: CredentialEvent) -> tuple[str, str | None, dict]:
        # Never the key (plaintext or ciphertext) or last4: provider and label only.
        return "credential", str(e.credential_id), {"provider": e.provider, "label": e.label}

    # Mailbox: ids and changed field names only. Never an address, host, username or password.
    subscribe(MailboxConnected, "mailbox.connected", lambda e: ("mailbox", str(e.mailbox_id), {}))
    subscribe(
        MailboxUpdated, "mailbox.updated", lambda e: ("mailbox", str(e.mailbox_id), {"changed": list(e.changed)})
    )
    subscribe(MailboxDisconnected, "mailbox.disconnected", lambda e: ("mailbox", str(e.mailbox_id), {}))

    # Voice: ids, kinds and changed field names only. Never a token, key, phone number or URL.
    subscribe(
        TelephonyConnected, "telephony.connected", lambda e: ("telephony_account", str(e.account_id), {"kind": e.kind})
    )
    subscribe(TelephonyDisconnected, "telephony.disconnected", lambda e: ("telephony_account", str(e.account_id), {}))
    subscribe(
        PhoneNumberAdded, "phone_number.added", lambda e: ("phone_number", str(e.number_id), {"ownership": e.ownership})
    )
    subscribe(
        PhoneNumberReleased,
        "phone_number.released",
        lambda e: ("phone_number", str(e.number_id), {"ownership": e.ownership}),
    )
    subscribe(
        VoiceSettingsUpdated,
        "voice_settings.updated",
        lambda e: ("organisation", str(e.organisation_id), {"changed": list(e.changed)}),
    )

    subscribe(
        OrganisationCreated,
        "organisation.created",
        lambda e: ("organisation", str(e.organisation_id), {"organisation_name": e.organisation_name}),
    )
    subscribe(
        OrganisationUpdated,
        "organisation.updated",
        lambda e: ("organisation", str(e.organisation_id), {"changes": e.changes}),
    )
    subscribe(RoleCreated, "role.created", role)
    subscribe(RoleUpdated, "role.updated", role)
    subscribe(RoleDeleted, "role.deleted", role)
    subscribe(
        MemberInvited,
        "member.invited",
        lambda e: ("invite", str(e.invite_id), {"email": e.email, "role_id": str(e.role_id)}),
    )
    subscribe(
        InviteAccepted, "invite.accepted", lambda e: ("membership", str(e.membership_id), {"user_id": str(e.user_id)})
    )
    subscribe(InviteRevoked, "invite.revoked", lambda e: ("invite", str(e.invite_id), {}))
    subscribe(MemberUpdated, "member.updated", lambda e: ("membership", str(e.membership_id), {"changes": e.changes}))
    subscribe(CredentialCreated, "credential.created", credential)
    subscribe(CredentialDeleted, "credential.deleted", credential)

    def document(e: DocumentEvent) -> tuple[str, str | None, dict]:
        # Never the storage key or file contents.
        return "document", str(e.document_id), {"knowledge_base_id": str(e.knowledge_base_id), "filename": e.filename}

    subscribe(
        KnowledgeBaseCreated,
        "kb.created",
        lambda e: (
            "knowledge_base",
            str(e.knowledge_base_id),
            {"name": e.name, "embedding_provider": e.embedding_provider, "embedding_model": e.embedding_model},
        ),
    )
    subscribe(
        KnowledgeBaseUpdated,
        "kb.updated",
        lambda e: ("knowledge_base", str(e.knowledge_base_id), {"name": e.name, "changes": e.changes, "rebuild": e.rebuild}),
    )
    subscribe(KnowledgeBaseDeleted, "kb.deleted", lambda e: ("knowledge_base", str(e.knowledge_base_id), {"name": e.name}))
    subscribe(
        DocumentUploaded,
        "document.uploaded",
        lambda e: ("document", str(e.document_id), {**document(e)[2], "file_type": e.file_type, "size_bytes": e.size_bytes}),
    )
    subscribe(DocumentDeleted, "document.deleted", document)
    subscribe(
        IndexingStarted,
        "kb.indexing_started",
        lambda e: ("knowledge_base", str(e.knowledge_base_id), {"document_ids": [str(d) for d in e.document_ids]}),
    )
    subscribe(
        AISettingsUpdated,
        "ai_settings.updated",
        lambda e: ("organisation", str(e.organisation_id), {"changes": e.changes}),
    )

    # Widgets: name and field diffs only. Never the identity secret (plaintext, ciphertext or last4).
    subscribe(WidgetCreated, "widget.created", lambda e: ("widget", str(e.widget_id), {"name": e.name}))
    subscribe(
        WidgetUpdated,
        "widget.updated",
        lambda e: ("widget", str(e.widget_id), {"name": e.name, "changes": e.changes}),
    )
    subscribe(WidgetDeleted, "widget.deleted", lambda e: ("widget", str(e.widget_id), {"name": e.name}))
    subscribe(
        WidgetIdentitySecretRotated,
        "widget.identity_secret_rotated",
        lambda e: ("widget", str(e.widget_id), {"name": e.name}),
    )

    # Risk rules: name, kind and field diffs (keywords are business content but not secret).
    subscribe(
        RiskRuleCreated,
        "risk_rule.created",
        lambda e: ("risk_rule", str(e.rule_id), {"name": e.name, "kind": e.kind, "action": e.action}),
    )
    subscribe(
        RiskRuleUpdated,
        "risk_rule.updated",
        lambda e: ("risk_rule", str(e.rule_id), {"name": e.name, "kind": e.kind, "changes": e.changes}),
    )
    subscribe(RiskRuleDeleted, "risk_rule.deleted", lambda e: ("risk_rule", str(e.rule_id), {"name": e.name, "kind": e.kind}))

    # Business tools: name and field diffs only. The auth header value never appears ("auth_header": "changed").
    subscribe(
        BusinessToolCreated,
        "business_tool.created",
        lambda e: ("business_tool", str(e.tool_id), {"name": e.name, "method": e.method}),
    )
    subscribe(
        BusinessToolUpdated,
        "business_tool.updated",
        lambda e: ("business_tool", str(e.tool_id), {"name": e.name, "changes": e.changes}),
    )
    subscribe(BusinessToolDeleted, "business_tool.deleted", lambda e: ("business_tool", str(e.tool_id), {"name": e.name}))

    subscribe(
        SupportSettingsUpdated,
        "support_settings.updated",
        lambda e: ("organisation", str(e.organisation_id), {"changes": e.changes}),
    )

    # Billing: the kind of change and field diffs (plan id, status, limit behaviour, dates). Never card data,
    # a provider secret or a webhook payload.
    subscribe(
        BillingAccountChanged,
        "billing_account.changed",
        lambda e: ("billing_account", str(e.account_id), {"change": e.change, "changes": e.changes}),
    )
