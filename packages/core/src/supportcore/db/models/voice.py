"""The voice channel: the organisation's Twilio account, its phone numbers, voice settings, calls and recordings."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from supportcore.db.base import Base, TenantMixin, TimestampMixin, uuid_pk

TELEPHONY_ACCOUNT_KINDS = ("platform", "byo")
TELEPHONY_ACCOUNT_STATUSES = ("active", "auth_failed", "error")
NUMBER_OWNERSHIPS = ("platform", "byo")
VOICE_MODES = ("chained", "speech_to_speech")
CALL_SOURCES = ("phone", "browser", "callback")
CALL_DIRECTIONS = ("inbound", "outbound")
CALL_MODES = ("chained", "speech_to_speech")
TRANSFER_STATUSES = ("ringing", "connected", "no_answer", "abandoned", "failed")
TRANSFER_TARGETS = ("browser", "phone")
CALL_STATUSES = ("ringing", "in_progress", "completed", "failed", "busy", "no_answer", "canceled")
CALL_END_REASONS = (
    "caller_hangup",
    "handoff_callback",
    "max_duration",
    "silence",
    "voice_unavailable",
    "capacity",
    "error",
    "transferred",
    "transfer_no_answer",
    "callback",
    "plan_limit",
)


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _now() -> datetime:
    return datetime.now(UTC)


class TelephonyAccount(TenantMixin, TimestampMixin, Base):
    """The organisation's Twilio account (a platform subaccount or its own). Secrets are Fernet ciphertext."""

    __tablename__ = "telephony_accounts"
    __table_args__ = (
        UniqueConstraint("organisation_id", name="uq_telephony_accounts_organisation_id"),
        CheckConstraint(_one_of("kind", TELEPHONY_ACCOUNT_KINDS), name="ck_telephony_accounts_kind"),
        CheckConstraint(_one_of("status", TELEPHONY_ACCOUNT_STATUSES), name="ck_telephony_accounts_status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    account_sid: Mapped[str] = mapped_column(String(34), unique=True, nullable=False)
    auth_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    auth_token_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    api_key_sid: Mapped[str | None] = mapped_column(String(34), nullable=True)
    api_key_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    twiml_app_sid: Mapped[str | None] = mapped_column(String(34), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active", nullable=False)
    last_error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # never the ciphertext
        return (
            f"TelephonyAccount(id={self.id!s}, organisation_id={self.organisation_id!s}, "
            f"kind={self.kind!r}, status={self.status!r})"
        )


class PhoneNumber(TenantMixin, TimestampMixin, Base):
    """A phone number calls arrive on. `e164` is unique across organisations: it is how a call finds its org."""

    __tablename__ = "phone_numbers"
    __table_args__ = (
        UniqueConstraint("e164", name="uq_phone_numbers_e164"),
        CheckConstraint(_one_of("ownership", NUMBER_OWNERSHIPS), name="ck_phone_numbers_ownership"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    telephony_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("telephony_accounts.id", ondelete="CASCADE"), nullable=False
    )
    e164: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_sid: Mapped[str] = mapped_column(String(34), nullable=False)
    ownership: Mapped[str] = mapped_column(String(16), nullable=False)
    label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # The widget whose knowledge bases answer calls to this number.
    widget_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("widgets.id", ondelete="SET NULL"), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sql_text("true"), nullable=False)
    # The voice URL we last configured on Twilio; differs from the current public URL when the tunnel moved.
    webhook_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)


class VoiceSettings(TenantMixin, TimestampMixin, Base):
    """Per-organisation voice configuration; one row per organisation, created lazily."""

    __tablename__ = "voice_settings"
    __table_args__ = (
        CheckConstraint(_one_of("mode", VOICE_MODES), name="ck_voice_settings_mode"),
        CheckConstraint(
            "recording_retention_days BETWEEN 1 AND 3650", name="ck_voice_settings_recording_retention_days"
        ),
        CheckConstraint("max_call_minutes BETWEEN 1 AND 120", name="ck_voice_settings_max_call_minutes"),
    )

    # Overrides TenantMixin's column: the organisation id is the primary key here.
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organisations.id", ondelete="CASCADE"), primary_key=True
    )
    phone_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sql_text("false"), nullable=False
    )
    browser_calls_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sql_text("false"), nullable=False
    )
    mode: Mapped[str] = mapped_column(String(24), default="chained", server_default="chained", nullable=False)
    stt_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stt_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stt_credential_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provider_credentials.id", ondelete="SET NULL"), nullable=True
    )
    tts_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tts_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tts_voice: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tts_credential_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provider_credentials.id", ondelete="SET NULL"), nullable=True
    )
    record_calls: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sql_text("false"), nullable=False)
    recording_retention_days: Mapped[int] = mapped_column(
        Integer, default=90, server_default=sql_text("90"), nullable=False
    )
    max_call_minutes: Mapped[int] = mapped_column(Integer, default=30, server_default=sql_text("30"), nullable=False)
    # The widget whose call button is enabled; NULL = all of the organisation's widgets.
    browser_widget_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("widgets.id", ondelete="SET NULL"), nullable=True
    )
    # Speech-to-speech (mode = "speech_to_speech"): the Realtime model, its voice and the OpenAI key.
    realtime_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    realtime_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    realtime_voice: Mapped[str | None] = mapped_column(String(32), nullable=True)
    realtime_credential_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provider_credentials.id", ondelete="SET NULL"), nullable=True
    )
    # Live transfer to a person: to an agent's browser (on by default) and to an agent's phone (off: it costs money).
    transfer_browser_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sql_text("true"), nullable=False
    )
    transfer_phone_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sql_text("false"), nullable=False
    )


class Call(TenantMixin, Base):
    """One voice call (phone or browser); its transcript is the conversation's messages."""

    __tablename__ = "calls"
    __table_args__ = (
        CheckConstraint(_one_of("source", CALL_SOURCES), name="ck_calls_source"),
        CheckConstraint(_one_of("direction", CALL_DIRECTIONS), name="ck_calls_direction"),
        CheckConstraint(_one_of("status", CALL_STATUSES), name="ck_calls_status"),
        CheckConstraint(
            "end_reason IS NULL OR " + _one_of("end_reason", CALL_END_REASONS), name="ck_calls_end_reason"
        ),
        CheckConstraint("mode IS NULL OR " + _one_of("mode", CALL_MODES), name="ck_calls_mode"),
        # A conversation has one inbound call; callbacks add outbound calls to it.
        Index(
            "uq_calls_inbound_conversation_id",
            "conversation_id",
            unique=True,
            postgresql_where=sql_text("direction = 'inbound'"),
        ),
        Index("ix_calls_conversation_id", "conversation_id"),
        UniqueConstraint("provider_call_sid", name="uq_calls_provider_call_sid"),
        UniqueConstraint("ticket_jti", name="uq_calls_ticket_jti"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    phone_number_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("phone_numbers.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), default="inbound", server_default="inbound", nullable=False)
    mode: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # The agent a callback call was placed by.
    agent_membership_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memberships.id", ondelete="SET NULL"), nullable=True
    )
    # NULL until Twilio returns the sid (an outbound callback creates the row first).
    provider_call_sid: Mapped[str | None] = mapped_column(String(34), nullable=True)
    caller_e164: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # The single-use browser call ticket's id.
    ticket_jti: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="ringing", server_default="ringing", nullable=False)
    end_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)
    stream_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the stream's session finished (its last turn persisted, usage stored). A call that connected a stream
    # is finalised by the later of this and Twilio's terminal status callback.
    stream_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When Twilio's terminal status callback arrived (its status and duration are stored on the call). The stale-call
    # sweep finalises a call whose stream end never got recorded some time after this.
    provider_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now(), nullable=False
    )
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stt_seconds: Mapped[float] = mapped_column(Float, default=0, server_default=sql_text("0"), nullable=False)
    tts_characters: Mapped[int] = mapped_column(Integer, default=0, server_default=sql_text("0"), nullable=False)
    recording_sid: Mapped[str | None] = mapped_column(String(34), nullable=True)
    # Set once the call's end bookkeeping (duration, analytics event) ran, so it runs exactly once.
    finalised: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sql_text("false"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now(), nullable=False
    )


class CallRecording(TenantMixin, Base):
    """A stored recording of a call, deleted at `delete_after`."""

    __tablename__ = "call_recordings"
    __table_args__ = (
        UniqueConstraint("call_id", name="uq_call_recordings_call_id"),
        Index("ix_call_recordings_delete_after", "delete_after"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(64), default="audio/mpeg", server_default="audio/mpeg", nullable=False
    )
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    delete_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now(), nullable=False
    )


class CallTransfer(TenantMixin, TimestampMixin, Base):
    """A live hand-off of a call to a person: the agents rung one after another until one joins or none can."""

    __tablename__ = "call_transfers"
    __table_args__ = (
        UniqueConstraint("call_id", name="uq_call_transfers_call_id"),
        CheckConstraint(_one_of("status", TRANSFER_STATUSES), name="ck_call_transfers_status"),
        CheckConstraint(
            "current_target IS NULL OR " + _one_of("current_target", TRANSFER_TARGETS),
            name="ck_call_transfers_current_target",
        ),
        Index("ix_call_transfers_organisation_id_status", "organisation_id", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), default="ringing", server_default="ringing", nullable=False)
    # The current attempt number (1-based once ringing).
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default=sql_text("0"), nullable=False)
    current_membership_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memberships.id", ondelete="SET NULL"), nullable=True
    )
    current_target: Mapped[str | None] = mapped_column(String(16), nullable=True)
    current_leg_sid: Mapped[str | None] = mapped_column(String(34), nullable=True)
    tried_membership_ids: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb"), nullable=False
    )
    connected_membership_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memberships.id", ondelete="SET NULL"), nullable=True
    )
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The connected agent's leg entered the conference (its join event); null while it may still drop before.
    agent_joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
