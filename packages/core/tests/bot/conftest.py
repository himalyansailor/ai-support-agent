"""Bot graph fixtures: data committed through a pooled engine (so tests can count checked-out
connections), the memory checkpointer, the fake embedder over an in-memory vector store, the
scripted chat model and a stub tool executor."""

import hashlib
import math
import uuid
from dataclasses import dataclass, field, replace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from supportcore.ai.embeddings.fake_embedder import FakeEmbedder
from supportcore.db.base import Base
from supportcore.knowledge.kb_service import collection_name
from supportcore.knowledge.vectorstore.ports import VectorHit
from supportcore.tools.ports import ToolResult

CHUNK_TEXTS = ["shipping takes three days", "contact support by email", "orders ship from our warehouse"]


class MemoryVectorStore:
    """Cosine nearest-neighbour store: {collection: [(org_id, chunk_id, document_id, vector)]}."""

    def __init__(self) -> None:
        self.collections: dict[str, list] = {}

    def put(self, name, org_id, chunk_id, document_id, vector) -> None:
        self.collections.setdefault(name, []).append((org_id, chunk_id, document_id, vector))

    async def query(self, name, embedding, *, organisation_id, k, document_ids=None):
        rows = [r for r in self.collections.get(name, []) if r[0] == organisation_id]

        def cos(v):
            dot = sum(a * b for a, b in zip(v, embedding))
            norm = math.sqrt(sum(a * a for a in v)) * math.sqrt(sum(b * b for b in embedding))
            return dot / norm if norm else 0.0

        hits = [VectorHit(chunk_id=c, document_id=d, score=cos(v), metadata={}) for _o, c, d, v in rows]
        return sorted(hits, key=lambda h: h.score, reverse=True)[:k]

    async def ensure_collection(self, *args, **kwargs) -> None:
        return None


class RecordingJobQueue:
    def __init__(self) -> None:
        self.jobs: list = []

    def enqueue(self, name, payload, *, countdown=None) -> None:
        self.jobs.append((name, payload))


OK_RESULT = ToolResult(ok=True, status=200, text='{"order": "42", "status": "shipped"}', latency_ms=3)


class StubToolExecutor:
    def __init__(self) -> None:
        self.calls: list = []
        self.result = OK_RESULT  # tests may replace it

    async def call(self, tool, args, auth_value):
        self.calls.append((tool.name, args, auth_value))
        return self.result


class RecordingSink:
    """Records every token; `text` is what was streamed since the last reset."""

    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.resets = 0
        self._since_reset: list[str] = []

    async def token(self, delta: str) -> None:
        self.tokens.append(delta)
        self._since_reset.append(delta)

    async def reset(self) -> None:
        self.resets += 1
        self._since_reset = []

    @property
    def text(self) -> str:
        return "".join(self._since_reset)


@dataclass
class World:
    org_id: uuid.UUID
    owner_id: uuid.UUID
    widget_id: uuid.UUID
    conversation_id: uuid.UUID
    kb_id: uuid.UUID | None
    chunk_ids: list[uuid.UUID] = field(default_factory=list)


@pytest.fixture
def bot_settings(tmp_path):
    from cryptography.fernet import Fernet

    from supportcore.config import Settings
    from supportcore.testing import TEST_DATABASE_URL

    return Settings(
        _env_file=None,
        env="test",
        database_url=TEST_DATABASE_URL,
        redis_url="redis://localhost:6379/0",
        jwt_secret="test-secret-test-secret-test-secret-123",
        smtp_from="noreply@test.local",
        secrets_key=Fernet.generate_key().decode(),
        file_storage="local",
        local_storage_path=str(tmp_path / "uploads"),
        vector_store="chroma",
        rate_limiter="memory",
        pubsub="memory",
        checkpointer="memory",
        llm_tracer="none",
    )


@pytest.fixture
async def bot_engine(db_engine):
    """A pooled engine (the shared fixture's NullPool has no checked-out count); tables are
    truncated at teardown."""
    from supportcore.testing import TEST_DATABASE_URL

    engine = create_async_engine(TEST_DATABASE_URL)
    yield engine
    await engine.dispose()
    names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with db_engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture
def bot_factory(bot_engine):
    return async_sessionmaker(bot_engine, expire_on_commit=False)


@pytest.fixture
def store():
    return MemoryVectorStore()


@pytest.fixture
def executor():
    return StubToolExecutor()


@pytest.fixture
def bot_adapters(bot_settings, store, executor):
    from supportcore.wiring import build_adapters

    return replace(build_adapters(bot_settings), vector_store=store, tool_executor=executor, job_queue=RecordingJobQueue())


@pytest.fixture
def bus():
    from supportcore.events import EventBus

    return EventBus()


@pytest.fixture
def runtime(bot_settings, bot_factory, bot_adapters, bus):
    from supportcore.bot.runtime import BotRuntime

    return BotRuntime(settings=bot_settings, session_factory=bot_factory, adapters=bot_adapters, events=bus)


@pytest.fixture
async def graph(runtime):
    from supportcore.bot.graph import compile_bot_graph

    return await compile_bot_graph(runtime)


@pytest.fixture(autouse=True)
def _reset_script():
    from supportcore.testing import script_chat

    script_chat([])
    yield
    script_chat([])


async def build_world(factory, adapters, settings, bus, store, *, chat=True, kb=True, tools=()) -> World:
    """An organisation with (optionally) the scripted chat model, a widget over one KB of
    CHUNK_TEXTS, the given business tools (dicts of BusinessTool columns), and a conversation."""
    from supportcore.ai.settings_service import OrganisationAISettingsService
    from supportcore.conversations.service import ConversationService
    from supportcore.db.models import BusinessTool, Chunk, Customer, Document, User
    from supportcore.db.uow import UnitOfWork
    from supportcore.knowledge.chunking.ports import ChunkingParams
    from supportcore.knowledge.kb_service import KnowledgeBaseService
    from supportcore.orgs.service import OrganisationService
    from supportcore.widget.service import WidgetService

    async with UnitOfWork(factory) as uow:
        s = uow.session
        owner = User(email=f"owner-{uuid.uuid4().hex[:8]}@x.com", password_hash="x", name="Owner")
        s.add(owner)
        await s.flush()
        org = await OrganisationService(s, bus).create(owner, "Acme")
        if chat:
            await OrganisationAISettingsService(s, org.id, bus, include_fake=True).update(
                "scripted", "scripted-chat", None, 0.2, owner
            )
        widgets = WidgetService(s, org.id, bus, adapters.cipher)
        widget = await widgets.create("Site", owner)
        kb_id, chunk_ids = None, []
        if kb:
            knowledge_base = await KnowledgeBaseService(s, org.id, bus, adapters, settings).create(
                name="Docs",
                description="",
                embedding_provider="fake",
                embedding_model="fake-embedding",
                embedding_credential_id=None,
                chunking=ChunkingParams(strategy="recursive"),
                reranker="none",
                reranker_params={},
                actor=owner,
            )
            kb_id = knowledge_base.id
            doc = Document(
                organisation_id=org.id,
                knowledge_base_id=kb_id,
                filename="faq.md",
                file_type="markdown",
                size_bytes=10,
                sha256=hashlib.sha256(str(kb_id).encode()).hexdigest(),
                storage_key=f"k/{uuid.uuid4()}",
                status="indexed",
                indexed_version=1,
            )
            s.add(doc)
            await s.flush()
            embedder = FakeEmbedder(dimensions=8)
            for i, chunk_text in enumerate(CHUNK_TEXTS):
                chunk = Chunk(
                    organisation_id=org.id,
                    knowledge_base_id=kb_id,
                    document_id=doc.id,
                    kb_version=1,
                    ordinal=i,
                    text=chunk_text,
                    char_start=0,
                    char_end=len(chunk_text),
                    metadata_={},
                )
                s.add(chunk)
                await s.flush()
                store.put(collection_name(kb_id, 1), org.id, chunk.id, doc.id, await embedder.embed_query(chunk_text))
                chunk_ids.append(chunk.id)
            await widgets.update(widget.id, knowledge_base_ids=[kb_id], actor=owner)
        for tool in tools:
            tool = dict(tool)
            auth_value = tool.pop("auth_value", None)
            row = BusinessTool(organisation_id=org.id, **tool)
            if auth_value is not None:
                row.auth_header_name = "X-Api-Key"
                row.encrypted_auth_header_value = adapters.cipher.encrypt(auth_value)
                row.auth_header_last4 = auth_value[-4:]
            s.add(row)
        customer = Customer(organisation_id=org.id, visitor_id=uuid.uuid4().hex)
        s.add(customer)
        await s.flush()
        conversation = await ConversationService(s, org.id, bus).create(customer.id, widget.id, "en", False)
        await uow.commit()
    return World(org.id, owner.id, widget.id, conversation.id, kb_id, chunk_ids)


@pytest.fixture
def make_world(bot_factory, bot_adapters, bot_settings, bus, store):
    async def make(**kwargs) -> World:
        return await build_world(bot_factory, bot_adapters, bot_settings, bus, store, **kwargs)

    return make


@pytest.fixture
def run_turn(graph, bot_factory, bus):
    """run_turn(world, text) -> (TurnResult, RecordingSink): stores the customer message and runs one turn."""
    from supportcore.bot.graph import run_turn as run_bot_turn
    from supportcore.bot.state import TurnInput
    from supportcore.conversations.service import ConversationService
    from supportcore.db.models import Conversation
    from supportcore.db.uow import UnitOfWork

    async def run(world: World, message: str):
        async with UnitOfWork(bot_factory) as uow:
            conversations = ConversationService(uow.session, world.org_id, bus)
            conversation = await uow.session.get(Conversation, world.conversation_id)
            stored, _created = await conversations.add_customer_message(conversation, message, None)
            await uow.commit()
        sink = RecordingSink()
        turn = TurnInput(
            organisation_id=world.org_id,
            conversation_id=world.conversation_id,
            widget_id=world.widget_id,
            message_id=stored.id,
            text=message,
            language="en",
        )
        return await run_bot_turn(graph, turn, sink), sink

    return run
