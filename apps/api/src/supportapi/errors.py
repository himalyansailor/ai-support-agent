"""Maps domain errors to HTTP responses. Later tasks add their errors to DOMAIN_ERRORS."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from supportcore.ai.catalog import UnknownModelError, UnsupportedLanguageError
from supportcore.ai.credentials import (
    CredentialInUseError,
    CredentialInvalidError,
    CredentialLabelTakenError,
    CredentialUnreadableError,
)
from supportcore.ai.errors import ProviderResponseError
from supportcore.ai.ports import EmbeddingError
from supportcore.ai.settings_service import (
    CredentialProviderMismatchError,
    InvalidModelNameError,
    InvalidTemperatureError,
)
from supportcore.analytics.definitions import InvalidDateRangeError
from supportcore.assignment.service import AssigneeNotEligibleError
from supportcore.billing.errors import (
    AlreadySubscribedError,
    BillingDisabledError,
    BillingNotConfiguredError,
    BillingProviderError,
    InvalidWebhookError,
    NotSubscribedError,
    OverageUnsupportedError,
    PlanKeyTakenError,
    PlanLimitError,
    PlanNotAvailableError,
    TrialNotAvailableError,
)
from supportcore.conversations.service import (
    InvalidContactEmailError,
    InvalidConversationStateError,
    InvalidCsatError,
    InvalidMessageError,
    InvalidPriorityError,
)
from supportcore.errors import NotFoundError
from supportcore.identity.service import (
    EmailTakenError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    WeakPasswordError,
)
from supportcore.inbox.notes import InvalidNoteError
from supportcore.inbox.saved_replies import (
    InvalidSavedReplyError,
    SavedReplyShortcutTakenError,
    TooManySavedRepliesError,
)
from supportcore.inbox.service import InvalidInboxFilterError, InvalidSearchQueryError
from supportcore.inbox.tags import InvalidTagError, TagNameTakenError, TooManyTagsError
from supportcore.knowledge.chunking.ports import InvalidChunkingParamsError
from supportcore.knowledge.document_service import (
    DuplicateDocumentError,
    InvalidFilenameError,
    UploadTooLargeError,
)
from supportcore.knowledge.kb_service import (
    InvalidKnowledgeBaseError,
    KnowledgeBaseNameTakenError,
    RebuildInProgressError,
    UnsupportedLanguageForModelError,
)
from supportcore.knowledge.parsing.ports import ParseError, UnsupportedFileTypeError
from supportcore.knowledge.vectorstore.ports import (
    VectorCollectionNotFoundError,
    VectorDimensionMismatchError,
)
from supportcore.mail.mailbox_service import InvalidMailboxSettingsError
from supportcore.mail.outbound import EmailNotAvailableError, EmailNotRetryableError
from supportcore.notifications.ports import EmailDeliveryError
from supportcore.ratelimit.ports import RateLimitedError
from supportcore.rbac.checker import PermissionEscalationError
from supportcore.rbac.permissions import InvalidPermissionError
from supportcore.risk.service import (
    BuiltinRuleError,
    InvalidRiskRuleError,
    RuleLockedError,
)
from supportcore.roles.service import (
    RoleInUseError,
    RoleNameTakenError,
    SystemRoleImmutableError,
)
from supportcore.support.settings_service import (
    InvalidBusinessHoursError,
    InvalidSlaTargetsError,
)
from supportcore.team.invites import (
    AlreadyMemberError,
    InviteAccountExistsError,
    InviteEmailMismatchError,
    InviteInvalidError,
    InviteSignupDetailsRequiredError,
)
from supportcore.team.service import (
    InvalidAgentStatusError,
    InvalidCallPreferenceError,
    InvalidMemberStatusError,
    InvalidPhoneNumberError,
    LastOwnerError,
    OwnerRoleRequiredError,
)
from supportcore.tools.schema import InvalidToolArgumentsError, InvalidToolSchemaError
from supportcore.tools.service import InvalidToolError, ToolNameTakenError
from supportcore.voice.errors import (
    AgentUnreachableError,
    InvalidVoiceSettingsError,
    NoCallbackNumberError,
    PhoneNumberUnavailableError,
    TelephonyAccountInUseError,
    TelephonyCredentialsError,
    TelephonyNotConfiguredError,
    TelephonyUnavailableError,
    VoiceNotAvailableError,
)
from supportcore.widget.domains import InvalidDomainError
from supportcore.widget.identity.ports import InvalidCustomerTokenError
from supportcore.widget.service import (
    IdentitySecretRequiredError,
    InvalidAppearanceError,
    TooManyDomainsError,
)
from supportcore.widget.session import IdentityRequiredError, OriginNotAllowedError
from supportcore.widget.tokens import InvalidWidgetTokenError

# Domain error -> (HTTP status, stable machine-readable code). Clients branch on `code`, not on `detail`.
DOMAIN_ERRORS: dict[type[Exception], tuple[int, str]] = {
    EmailTakenError: (409, "email_taken"),
    InvalidCredentialsError: (401, "invalid_credentials"),
    InvalidRefreshTokenError: (401, "invalid_refresh_token"),
    WeakPasswordError: (422, "weak_password"),
    NotFoundError: (404, "not_found"),
    SystemRoleImmutableError: (409, "system_role_immutable"),
    RoleInUseError: (409, "role_in_use"),
    RoleNameTakenError: (409, "role_name_taken"),
    InvalidPermissionError: (422, "invalid_permission"),
    LastOwnerError: (409, "last_owner"),
    OwnerRoleRequiredError: (403, "owner_role_required"),
    PermissionEscalationError: (403, "permission_escalation"),
    InvalidPhoneNumberError: (422, "invalid_phone_number"),
    InvalidCallPreferenceError: (422, "invalid_call_preference"),
    InvalidMemberStatusError: (422, "invalid_member_status"),
    InvalidAgentStatusError: (422, "invalid_agent_status"),
    AlreadyMemberError: (409, "already_member"),
    InviteInvalidError: (410, "invite_invalid"),
    InviteEmailMismatchError: (403, "invite_email_mismatch"),
    InviteAccountExistsError: (409, "invite_account_exists"),
    InviteSignupDetailsRequiredError: (422, "invite_signup_details_required"),
    EmailDeliveryError: (502, "email_delivery_failed"),
    CredentialInvalidError: (422, "credential_invalid"),
    CredentialInUseError: (409, "credential_in_use"),
    CredentialLabelTakenError: (409, "credential_label_taken"),
    CredentialProviderMismatchError: (422, "credential_provider_mismatch"),
    UnknownModelError: (422, "unknown_model"),
    InvalidTemperatureError: (422, "invalid_temperature"),
    InvalidModelNameError: (422, "invalid_model_name"),
    CredentialUnreadableError: (422, "credential_unreadable"),
    UnsupportedLanguageError: (422, "unsupported_language"),
    UnsupportedFileTypeError: (415, "unsupported_file_type"),
    ParseError: (422, "parse_error"),
    InvalidChunkingParamsError: (422, "invalid_chunking_params"),
    VectorDimensionMismatchError: (409, "vector_dimension_mismatch"),
    # Also covers VectorCollectionOwnershipError (a subclass): foreign collections look absent.
    VectorCollectionNotFoundError: (404, "vector_collection_not_found"),
    UploadTooLargeError: (413, "upload_too_large"),
    DuplicateDocumentError: (409, "duplicate_document"),
    InvalidFilenameError: (422, "invalid_filename"),
    RebuildInProgressError: (409, "rebuild_in_progress"),
    UnsupportedLanguageForModelError: (422, "unsupported_language_for_model"),
    KnowledgeBaseNameTakenError: (409, "knowledge_base_name_taken"),
    InvalidKnowledgeBaseError: (422, "invalid_knowledge_base"),
    # E.g. the dimension probe of an openai_compatible model failed; the message is sanitised.
    EmbeddingError: (502, "embedding_failed"),
    # An openai_compatible response refused unread (too large, compressed); normally wrapped first.
    ProviderResponseError: (502, "provider_response_refused"),
    RateLimitedError: (429, "rate_limited"),
    InvalidDomainError: (422, "invalid_domain"),
    InvalidAppearanceError: (422, "invalid_appearance"),
    TooManyDomainsError: (422, "too_many_domains"),
    IdentitySecretRequiredError: (422, "identity_secret_required"),
    OriginNotAllowedError: (403, "origin_not_allowed"),
    IdentityRequiredError: (401, "identity_required"),
    InvalidCustomerTokenError: (401, "invalid_customer_token"),
    InvalidWidgetTokenError: (401, "invalid_widget_token"),
    InvalidConversationStateError: (409, "invalid_conversation_state"),
    AssigneeNotEligibleError: (422, "assignee_not_eligible"),
    InvalidMessageError: (422, "invalid_message"),
    InvalidCsatError: (422, "invalid_csat"),
    InvalidDateRangeError: (422, "invalid_date_range"),
    InvalidContactEmailError: (422, "invalid_contact_email"),
    BuiltinRuleError: (409, "builtin_rule"),
    RuleLockedError: (409, "rule_locked"),
    InvalidRiskRuleError: (422, "invalid_risk_rule"),
    InvalidToolSchemaError: (422, "invalid_tool_schema"),
    InvalidToolArgumentsError: (422, "invalid_tool_arguments"),
    InvalidToolError: (422, "invalid_tool"),
    ToolNameTakenError: (409, "tool_name_taken"),
    InvalidBusinessHoursError: (422, "invalid_business_hours"),
    InvalidSlaTargetsError: (422, "invalid_sla_targets"),
    InvalidSearchQueryError: (422, "invalid_search_query"),
    InvalidInboxFilterError: (422, "invalid_inbox_filter"),
    InvalidPriorityError: (422, "invalid_priority"),
    InvalidNoteError: (422, "invalid_note"),
    InvalidTagError: (422, "invalid_tag"),
    TooManyTagsError: (422, "too_many_tags"),
    TagNameTakenError: (409, "tag_name_taken"),
    InvalidSavedReplyError: (422, "invalid_saved_reply"),
    TooManySavedRepliesError: (422, "too_many_saved_replies"),
    SavedReplyShortcutTakenError: (409, "saved_reply_shortcut_taken"),
    InvalidMailboxSettingsError: (422, "invalid_mailbox_settings"),
    EmailNotAvailableError: (409, "email_not_available"),
    EmailNotRetryableError: (409, "email_not_retryable"),
    InvalidVoiceSettingsError: (422, "invalid_voice_settings"),
    TelephonyCredentialsError: (422, "telephony_credentials_invalid"),
    TelephonyNotConfiguredError: (409, "telephony_not_configured"),
    PhoneNumberUnavailableError: (409, "phone_number_unavailable"),
    VoiceNotAvailableError: (409, "voice_not_available"),
    TelephonyUnavailableError: (502, "telephony_unavailable"),
    TelephonyAccountInUseError: (409, "telephony_account_in_use"),
    NoCallbackNumberError: (409, "callback_number_missing"),
    AgentUnreachableError: (409, "agent_unreachable"),
    PlanLimitError: (402, "plan_limit"),
    BillingProviderError: (502, "billing_provider_error"),
    BillingNotConfiguredError: (409, "billing_not_configured"),
    BillingDisabledError: (409, "billing_disabled"),
    OverageUnsupportedError: (409, "overage_unsupported"),
    InvalidWebhookError: (400, "invalid_webhook"),
    PlanNotAvailableError: (409, "plan_not_available"),
    PlanKeyTakenError: (409, "plan_key_taken"),
    AlreadySubscribedError: (409, "already_subscribed"),
    NotSubscribedError: (409, "not_subscribed"),
    TrialNotAvailableError: (409, "trial_not_available"),
}


async def _domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    status, code = next(DOMAIN_ERRORS[cls] for cls in type(exc).__mro__ if cls in DOMAIN_ERRORS)
    return JSONResponse({"detail": str(exc), "code": code}, status_code=status)


async def _rate_limited_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RateLimitedError)
    return JSONResponse(
        {"detail": str(exc), "code": "rate_limited"},
        status_code=429,
        headers={"Retry-After": str(exc.retry_after_seconds)},
    )


async def _plan_limit_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, PlanLimitError)
    return JSONResponse(
        {"detail": str(exc), "code": "plan_limit", "resource": exc.resource, "limit": exc.limit}, status_code=402
    )


def register_error_handlers(app: FastAPI) -> None:
    for exc_class in DOMAIN_ERRORS:
        app.add_exception_handler(exc_class, _domain_error_handler)
    app.add_exception_handler(RateLimitedError, _rate_limited_handler)
    app.add_exception_handler(PlanLimitError, _plan_limit_handler)
