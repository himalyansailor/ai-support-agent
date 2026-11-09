"""supportcore is framework-free: web-framework code (FastAPI, Starlette) lives in apps/api."""

import ast
from fnmatch import fnmatch
from pathlib import Path

import supportcore

_FORBIDDEN = ("fastapi", "starlette", "opentelemetry.instrumentation.fastapi")


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_supportcore_does_not_import_web_frameworks():
    root = Path(supportcore.__file__).parent
    offenders = [
        f"{path.relative_to(root)}: {mod}"
        for path in root.rglob("*.py")
        for mod in _imported_modules(path)
        if any(mod == f or mod.startswith(f + ".") for f in _FORBIDDEN)
    ]
    assert offenders == []


# Adapter modules (path globs relative to the supportcore root). Only wiring.py (and the
# api container / worker, outside this package) may import them.
_ADAPTER_GLOBS = (
    "*/fernet_cipher.py",
    "*/s3_storage.py",
    "*/local_storage.py",
    "*_embedder.py",
    "*/langchain_factory.py",
    "*/fake_chat.py",
    "*/scripted_chat.py",
    "*_reranker.py",
    "*_store.py",
    "*/inline_queue.py",
    "*/celery_queue.py",
    "*_limiter.py",
    "*_presence.py",
    "inbox/postgres_search.py",  # the port is inbox/search_ports.py
    "assignment/round_robin.py",  # the port is assignment/ports.py
    "*_pubsub.py",  # the port is pubsub/ports.py
    "*_checkpointer.py",  # the port is bot/checkpointer.py (no underscore prefix)
    "*_tracer.py",  # the port is llmtrace/ports.py
    "knowledge/parsing/pdf.py",
    "knowledge/parsing/docx.py",
    "knowledge/parsing/text.py",
    "knowledge/parsing/html.py",
    "knowledge/chunking/langchain_chunkers.py",
    "risk/rules_llm.py",  # the port is risk/ports.py
    "widget/identity/jwt_verifier.py",  # the port is widget/identity/ports.py
    "tools/http_executor.py",  # the port is tools/ports.py
    "mail/imap_inbound.py",  # the port is mail/ports.py
    "mail/smtp_outbound.py",
    "mail/fake_*.py",
    "voice/telephony/twilio_telephony.py",  # the port is voice/telephony/ports.py
    "voice/telephony/fake_telephony.py",
    "voice/stt/deepgram_stt.py",  # the port is voice/stt/ports.py
    "voice/stt/fake_stt.py",
    "voice/tts/openai_tts.py",  # the port is voice/tts/ports.py
    "voice/tts/fake_tts.py",
    "voice/realtime/openai_realtime.py",  # the port is voice/realtime/ports.py
    "voice/realtime/fake_realtime.py",
    "ai/validation.py",
    "ai/http.py",
)
_ALLOWED_IMPORTERS = ("wiring.py", "testing.py")


def _is_adapter(rel: str) -> bool:
    return any(fnmatch(rel, g) for g in _ADAPTER_GLOBS)


def _adapter_module_names(root: Path) -> set[str]:
    return {
        "supportcore." + path.relative_to(root).with_suffix("").as_posix().replace("/", ".")
        for path in root.rglob("*.py")
        if _is_adapter(path.relative_to(root).as_posix())
    }


def test_only_wiring_imports_adapter_modules():
    root = Path(supportcore.__file__).parent
    adapters = _adapter_module_names(root)
    offenders = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel in _ALLOWED_IMPORTERS or _is_adapter(rel):
            continue
        for mod in _imported_modules(path):
            if mod in adapters:
                offenders.append(f"{rel}: {mod}")
    assert offenders == []


def test_layering_detects_adapter_import(tmp_path):
    """The detector itself flags a domain module importing an adapter."""
    pkg = tmp_path / "supportcore" / "knowledge"
    pkg.mkdir(parents=True)
    (pkg / "s3_storage.py").write_text("")
    (pkg / "service.py").write_text("from supportcore.knowledge.s3_storage import S3\n")
    adapters = _adapter_module_names(tmp_path / "supportcore")
    assert adapters == {"supportcore.knowledge.s3_storage"}
    assert adapters & _imported_modules(pkg / "service.py")


def test_supportcore_does_not_import_the_apps():
    """Core never depends on the API or the worker (CeleryJobQueue sends tasks by name)."""
    root = Path(supportcore.__file__).parent
    offenders = [
        f"{path.relative_to(root)}: {mod}"
        for path in root.rglob("*.py")
        for mod in _imported_modules(path)
        if mod.split(".")[0] in ("supportapi", "supportworker")
    ]
    assert offenders == []


def test_worker_does_not_import_the_api():
    import supportworker

    root = Path(supportworker.__file__).parent
    offenders = [
        f"{path.relative_to(root)}: {mod}"
        for path in root.rglob("*.py")
        for mod in _imported_modules(path)
        if mod.split(".")[0] in ("supportapi", "fastapi", "starlette")
    ]
    assert offenders == []
