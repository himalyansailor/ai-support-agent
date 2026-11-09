from uuid import UUID

from pydantic import BaseModel, Field


class AgentCallTokenOut(BaseModel):
    token: str = Field(repr=False)
    identity: str
    expires_in: int


class CallbackTicketOut(BaseModel):
    ticket: str = Field(repr=False)


class CallbackCallOut(BaseModel):
    call_id: UUID
