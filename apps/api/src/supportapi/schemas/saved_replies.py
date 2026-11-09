from uuid import UUID

from pydantic import BaseModel, Field


class SavedReplyOut(BaseModel):
    id: UUID
    title: str
    body: str
    shortcut: str | None
    created_by_membership_id: UUID | None


# The service trims and checks the real limits (422 invalid_saved_reply); these only bound the payload.
class SavedReplyIn(BaseModel):
    title: str = Field(max_length=1000)
    body: str = Field(max_length=20000)
    shortcut: str | None = Field(default=None, max_length=200)


class SavedReplyPatch(BaseModel):
    title: str | None = Field(default=None, max_length=1000)
    body: str | None = Field(default=None, max_length=20000)
    shortcut: str | None = Field(default=None, max_length=200)  # explicit null removes the shortcut
