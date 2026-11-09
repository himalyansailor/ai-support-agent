"""Risk rules: the topics and built-in signals that decide whether the bot answers, offers a person or escalates."""

import uuid
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, Integer, String, UniqueConstraint
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from supportcore.db.base import Base, TenantMixin, TimestampMixin, uuid_pk

RISK_KINDS = ("topic", "builtin")
RISK_ACTIONS = ("escalate", "offer_human", "bot_handles")


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class RiskRule(TenantMixin, TimestampMixin, Base):
    __tablename__ = "risk_rules"
    __table_args__ = (
        # NULL builtin_keys (topics) never collide; this is also the seeding conflict target.
        UniqueConstraint("organisation_id", "builtin_key", name="uq_risk_rules_organisation_id_builtin_key"),
        CheckConstraint(_one_of("kind", RISK_KINDS), name="ck_risk_rules_kind"),
        CheckConstraint(_one_of("action", RISK_ACTIONS), name="ck_risk_rules_action"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    builtin_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # The classifier sees the description, so it should say what the topic covers.
    description: Mapped[str] = mapped_column(String(1000), default="", server_default="", nullable=False)
    keywords: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sql_text("true"), nullable=False)
    locked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sql_text("false"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sql_text("'{}'::jsonb"), nullable=False
    )
