from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CreateOrgRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v


class OrgOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    slug: str
    language: str


class UpdateOrgRequest(BaseModel):
    language: str = Field(max_length=64)  # must be in the catalog's LANGUAGES (422 unsupported_language)
