from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints

from supportcore.knowledge.retrieval import DEFAULT_TOP_K, MAX_TOP_K, MIN_TOP_K

MAX_QUESTION_LENGTH = 2000


class PlaygroundRequest(BaseModel):
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUESTION_LENGTH)]
    top_k: int = Field(default=DEFAULT_TOP_K, ge=MIN_TOP_K, le=MAX_TOP_K)
    with_answer: bool = True


class RetrievedChunkOut(BaseModel):
    chunk_id: UUID
    knowledge_base_id: UUID
    document_id: UUID
    document_name: str
    ordinal: int
    text: str
    score: float
    rerank_score: float | None
    metadata: dict[str, Any]


class PlaygroundAnswerOut(BaseModel):
    text: str
    citations: list[UUID]  # chunk ids, in first-cited order
    provider: str
    model: str


class PlaygroundTimingsOut(BaseModel):
    embed: int
    search: int
    rerank: int
    generate: int


class PlaygroundOut(BaseModel):
    chunks: list[RetrievedChunkOut]
    answer: PlaygroundAnswerOut | None
    warnings: list[str]
    timings_ms: PlaygroundTimingsOut
    trace_id: str | None
