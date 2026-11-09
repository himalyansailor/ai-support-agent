from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from supportapi.schemas.auth import UserOut
from supportapi.schemas.common import EmailAddress


class MemberOut(BaseModel):
    membership_id: UUID
    user_id: UUID
    name: str
    email: str
    role_id: UUID
    role_name: str
    status: str
    phone_number: str | None
    agent_status: str
    call_preference: str


class UpdateMemberRequest(BaseModel):
    """Omitted fields are unchanged; phone_number may be null to clear it."""

    role_id: UUID | None = None
    status: Literal["active", "deactivated"] | None = None
    phone_number: str | None = Field(default=None, max_length=32)
    call_preference: str | None = Field(default=None, max_length=16)  # validated by the domain: invalid_call_preference

    @model_validator(mode="after")
    def _at_least_one(self) -> "UpdateMemberRequest":
        if not self.model_fields_set:
            raise ValueError("provide role_id, status, phone_number and/or call_preference")
        return self


class CreateInviteRequest(BaseModel):
    email: EmailAddress
    role_id: UUID


class InviteOut(BaseModel):
    id: UUID
    email: str
    role_id: UUID
    role_name: str
    invited_by_user_id: UUID | None
    expires_at: datetime
    created_at: datetime


class InvitePreviewRequest(BaseModel):
    token: str = Field(min_length=1, max_length=256)


class InvitePreviewOut(BaseModel):
    organisation_name: str
    email: str
    role_name: str


class AcceptInviteRequest(BaseModel):
    token: str = Field(min_length=1, max_length=256)
    name: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=1024)


class AcceptInviteResponse(BaseModel):
    organisation_id: UUID
    membership_id: UUID
    user: UserOut
    access_token: str | None  # set only when the invite created a new account
