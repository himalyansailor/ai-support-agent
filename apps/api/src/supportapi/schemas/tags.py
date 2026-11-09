from uuid import UUID

from pydantic import BaseModel, Field


class TagOut(BaseModel):
    id: UUID
    name: str


class TagIn(BaseModel):
    name: str = Field(max_length=200)  # the service trims and checks 1-40 (422 invalid_tag)


class ConversationTagsIn(BaseModel):
    tag_ids: list[UUID] = Field(max_length=200)  # more than 10 distinct ids is 422 too_many_tags
