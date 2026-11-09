"""Knowledge base and document events. They never carry storage keys, file contents or API keys."""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from supportcore.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class KnowledgeBaseEvent(DomainEvent):
    knowledge_base_id: UUID
    name: str


@dataclass(frozen=True, kw_only=True)
class KnowledgeBaseCreated(KnowledgeBaseEvent):
    embedding_provider: str
    embedding_model: str


@dataclass(frozen=True, kw_only=True)
class KnowledgeBaseUpdated(KnowledgeBaseEvent):
    changes: dict[str, Any] = field(default_factory=dict)  # {field: {"from": ..., "to": ...}}, JSON-safe
    rebuild: bool = False


@dataclass(frozen=True, kw_only=True)
class KnowledgeBaseDeleted(KnowledgeBaseEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class DocumentEvent(DomainEvent):
    knowledge_base_id: UUID
    document_id: UUID
    filename: str


@dataclass(frozen=True, kw_only=True)
class DocumentUploaded(DocumentEvent):
    file_type: str
    size_bytes: int


@dataclass(frozen=True, kw_only=True)
class DocumentDeleted(DocumentEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class IndexingStarted(DomainEvent):
    knowledge_base_id: UUID
    document_ids: tuple[UUID, ...] = ()
