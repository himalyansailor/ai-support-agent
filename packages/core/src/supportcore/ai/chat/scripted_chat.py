"""Scripted chat model for tests (provider `scripted`, registered only when settings.env == "test").

Replies come from a module-level script set with `supportcore.testing.script_chat([...])`. Each
model call pops the next entry: a `str` (a text reply), an `AIMessage` (text and/or tool calls) or
an exception instance (raised, like a provider error), a `StreamThenFail` (streams some text,
then raises) or a `NoUsage` (a text reply that reports no token usage, like a provider that does not).

Calls whose first message carries the risk classifier marker (`SUPPORT_RISK_CLASSIFIER v1`) take
their reply from a separate classifier script instead, so a test's reply order does not depend on
whether the classifier ran; when that script is empty the classifier gets a "no topic" verdict.

`bind_tools` returns a copy that remembers the bound tool names; every call is recorded (with the
tool names bound) on the script. Streaming splits the text into word chunks and sends tool calls
and usage in a final chunk. A `hook(messages)` (sync or async) runs at the start of every call.
"""

from __future__ import annotations

import inspect
import json
import re
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from supportcore.ai.catalog import get_chat_spec

CLASSIFIER_MARKER = "SUPPORT_RISK_CLASSIFIER v1"
NO_TOPIC_VERDICT = '{"topic_ids": [], "asks_for_human": false, "frustrated": false, "reason": "none"}'

@dataclass(frozen=True)
class StreamThenFail:
    """A reply that streams `text` and then raises `error` (a provider failing mid-stream)."""

    text: str
    error: BaseException


@dataclass(frozen=True)
class NoUsage:
    """A text reply that reports no token usage (no `usage_metadata`)."""

    text: str


Entry = str | AIMessage | BaseException | StreamThenFail | NoUsage
Hook = Callable[[list[BaseMessage]], Any]


@dataclass
class ScriptedCall:
    messages: list[BaseMessage]
    tools: list[str]  # names of the bound tools; empty when none were bound
    classifier: bool


@dataclass
class ChatScript:
    replies: deque[Entry] = field(default_factory=deque)
    classifier: deque[Entry] = field(default_factory=deque)
    hook: Hook | None = None
    calls: list[ScriptedCall] = field(default_factory=list)

    @property
    def reply_calls(self) -> list[ScriptedCall]:
        """The calls that were not classifier calls."""
        return [c for c in self.calls if not c.classifier]


_script = ChatScript()


def set_script(
    replies: Sequence[Entry], *, classifier: Sequence[Entry] = (), hook: Hook | None = None
) -> ChatScript:
    global _script
    _script = ChatScript(replies=deque(replies), classifier=deque(classifier), hook=hook)
    return _script


def _is_classifier(messages: list[BaseMessage]) -> bool:
    return bool(messages) and isinstance(messages[0].content, str) and CLASSIFIER_MARKER in messages[0].content


def _usage(messages: list[BaseMessage], text: str) -> dict[str, int]:
    words_in = sum(len(str(m.content).split()) for m in messages)
    words_out = len(text.split())
    return {"input_tokens": words_in, "output_tokens": words_out, "total_tokens": words_in + words_out}


class ScriptedChatModel(BaseChatModel):
    bound_tools: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ScriptedChatModel:  # type: ignore[override]
        names = [t["name"] if isinstance(t, dict) else getattr(t, "name", str(t)) for t in tools]
        return self.model_copy(update={"bound_tools": names})

    def _next(self, messages: list[BaseMessage]) -> AIMessage | StreamThenFail:
        classifier = _is_classifier(messages)
        _script.calls.append(ScriptedCall(list(messages), list(self.bound_tools), classifier))
        queue = _script.classifier if classifier else _script.replies
        if not queue:
            if classifier:
                return AIMessage(content=NO_TOPIC_VERDICT)
            raise RuntimeError("The chat script is exhausted")
        entry = queue.popleft()
        if isinstance(entry, BaseException):
            raise entry
        if isinstance(entry, StreamThenFail):
            return entry
        if isinstance(entry, NoUsage):
            return AIMessage(content=entry.text)
        if isinstance(entry, str):
            entry = AIMessage(content=entry)
        return entry.model_copy(update={"usage_metadata": _usage(messages, str(entry.content))})

    async def _run_hook(self, messages: list[BaseMessage]) -> None:
        if _script.hook is not None:
            result = _script.hook(messages)
            if inspect.isawaitable(result):
                await result

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if _script.hook is not None:
            _script.hook(messages)
        return ChatResult(generations=[ChatGeneration(message=_whole(self._next(messages)))])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        await self._run_hook(messages)
        return ChatResult(generations=[ChatGeneration(message=_whole(self._next(messages)))])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        if _script.hook is not None:
            _script.hook(messages)
        yield from _chunks(self._next(messages))

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        await self._run_hook(messages)
        for chunk in _chunks(self._next(messages)):
            yield chunk


def _whole(entry: AIMessage | StreamThenFail) -> AIMessage:
    if isinstance(entry, StreamThenFail):
        raise entry.error
    return entry


def _chunks(entry: AIMessage | StreamThenFail) -> Iterator[ChatGenerationChunk]:
    if isinstance(entry, StreamThenFail):
        for word in re.findall(r"\s*\S+", entry.text):
            yield ChatGenerationChunk(message=AIMessageChunk(content=word))
        raise entry.error
    message = entry
    text = message.content if isinstance(message.content, str) else str(message.content)
    for word in re.findall(r"\s*\S+", text):
        yield ChatGenerationChunk(message=AIMessageChunk(content=word))
    tool_call_chunks = [
        {"name": call["name"], "args": json.dumps(call["args"]), "id": call.get("id"), "index": i}
        for i, call in enumerate(message.tool_calls)
    ]
    yield ChatGenerationChunk(
        message=AIMessageChunk(content="", tool_call_chunks=tool_call_chunks, usage_metadata=message.usage_metadata)
    )


def scripted_chat_model(
    *,
    model: str,
    api_key: str | None,
    base_url: str | None,
    temperature: float,
    max_tokens: int | None = None,
    timeout: float | None = None,
) -> BaseChatModel:
    get_chat_spec("scripted", model, include_fake=True)
    return ScriptedChatModel()
