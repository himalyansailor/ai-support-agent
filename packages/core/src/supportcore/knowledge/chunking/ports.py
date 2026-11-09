"""Chunking port: a parsed document in, ordered chunks with exact source char spans out."""

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError, ValidationInfo, field_validator, model_validator

from supportcore.errors import DomainError
from supportcore.knowledge.parsing.ports import ParsedDocument

STRATEGIES = ("fixed", "recursive", "markdown")
MIN_CHUNK_SIZE, MAX_CHUNK_SIZE = 100, 8000


class InvalidChunkingParamsError(DomainError):
    """Unknown strategy or out-of-range chunk size/overlap."""


class ChunkingParams(BaseModel):
    """Raises InvalidChunkingParamsError (not pydantic's ValidationError) on bad input."""

    strategy: str
    chunk_size: int = 1000
    chunk_overlap: int = 150

    @model_validator(mode="wrap")
    @classmethod
    def _readable_errors(cls, data: Any, handler: Any) -> "ChunkingParams":
        try:
            return handler(data)
        except ValidationError as exc:
            problems = []
            for err in exc.errors():
                field_name = ".".join(str(part) for part in err["loc"])
                msg = str(err["msg"]).removeprefix("Value error, ")
                problems.append(f"{field_name}: {msg}" if field_name else msg)
            raise InvalidChunkingParamsError("Invalid chunking settings. " + "; ".join(problems)) from None

    @field_validator("strategy")
    @classmethod
    def _known_strategy(cls, value: str) -> str:
        if value not in STRATEGIES:
            raise ValueError(f"unknown strategy {value!r}, expected one of {', '.join(STRATEGIES)}")
        return value

    @field_validator("chunk_size")
    @classmethod
    def _size_in_range(cls, value: int) -> int:
        if not MIN_CHUNK_SIZE <= value <= MAX_CHUNK_SIZE:
            raise ValueError(f"must be between {MIN_CHUNK_SIZE} and {MAX_CHUNK_SIZE} characters")
        return value

    @field_validator("chunk_overlap")
    @classmethod
    def _overlap_in_range(cls, value: int, info: ValidationInfo) -> int:
        size = info.data.get("chunk_size")  # absent when chunk_size itself was invalid
        if value < 0:
            raise ValueError("must not be negative")
        if size is not None and value > size // 2:
            raise ValueError(f"must be at most {size // 2} (half of chunk_size)")
        return value


@dataclass
class ChunkDraft:
    """`text == document.text[char_start:char_end]` always holds."""

    ordinal: int
    text: str
    char_start: int
    char_end: int
    metadata: dict[str, Any] = field(default_factory=dict)  # page | pages, headings


class Chunker(Protocol):
    def chunk(self, doc: ParsedDocument, params: ChunkingParams) -> list[ChunkDraft]: ...
