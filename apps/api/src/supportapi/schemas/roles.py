from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


def _strip_name(v: str | None) -> str | None:
    if v is None:
        return v
    v = v.strip()
    if not 1 <= len(v) <= 50:
        raise ValueError("name must be 1-50 characters")
    return v


class CreateRoleRequest(BaseModel):
    name: str
    permissions: list[str] = Field(default_factory=list)

    _name = field_validator("name")(_strip_name)


class UpdateRoleRequest(BaseModel):
    name: str | None = None
    permissions: list[str] | None = None

    _name = field_validator("name")(_strip_name)

    @model_validator(mode="after")
    def _at_least_one(self) -> "UpdateRoleRequest":
        if self.name is None and self.permissions is None:
            raise ValueError("provide name and/or permissions")
        return self


class RoleOut(BaseModel):
    id: UUID
    name: str
    permissions: list[str]
    is_system: bool
    member_count: int


class PermissionOut(BaseModel):
    key: str
    label: str
