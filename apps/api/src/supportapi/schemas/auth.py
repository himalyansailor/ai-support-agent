from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from supportapi.schemas.common import DisplayName, EmailAddress


class SignupRequest(BaseModel):
    email: EmailAddress
    password: str = Field(max_length=1024)
    name: DisplayName


class LoginRequest(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=1024)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    name: str
    is_platform_admin: bool


class AuthResponse(BaseModel):
    access_token: str
    user: UserOut


class MembershipOut(BaseModel):
    organisation_id: UUID
    organisation_name: str
    role_name: str
    permissions: list[str]


class MeResponse(BaseModel):
    user: UserOut
    memberships: list[MembershipOut]
