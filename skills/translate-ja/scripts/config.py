"""translate-ja の環境変数設定を扱う。"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from io_utils import sha256_text


@dataclass(frozen=True)
class DoclingSettings:
    """Docling Serve へ接続するための設定。"""

    server_url: str
    api_key: str
    timeout_seconds: int


@dataclass(frozen=True)
class OpenAISettings:
    """OpenAI 互換 Chat Completions API の設定。"""

    base_url: str
    api_key: str
    model: str
    timeout_seconds: int
    max_retries: int


def load_dotenv(path: str | Path = ".env") -> None:
    """python-dotenv に依存せず、単純な KEY=VALUE 形式の .env を読み込む。"""

    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env_first(*names: str, default: str | None = None) -> str | None:
    """複数の環境変数名から最初に設定済みの値を返す。"""

    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def require_docling_settings(*, timeout_seconds: int | None = None) -> DoclingSettings:
    """Docling Serve の必須設定を検証して返す。"""

    server_url = _env_first("DOCLING_SERVER_URL", "DOCLING_SERVE_URL")
    api_key = _env_first("DOCLING_API_KEY", "DOCLING_SERVE_API_KEY")
    if not server_url:
        raise RuntimeError("DOCLING_SERVER_URL is required")
    if not api_key:
        raise RuntimeError("DOCLING_API_KEY is required")
    return DoclingSettings(
        server_url=server_url.rstrip("/"),
        api_key=api_key,
        timeout_seconds=timeout_seconds
        or int(os.environ.get("DOCLING_TIMEOUT_SECONDS", "21600")),
    )


def require_openai_settings(*, timeout_seconds: int | None = None) -> OpenAISettings:
    """OpenAI 互換 API の必須設定を検証して返す。"""

    base_url = os.environ.get("OPENAI_BASE_URL")
    api_key = os.environ.get("OPENAI_API_KEY")
    model = os.environ.get("OPENAI_MODEL")
    if not base_url:
        raise RuntimeError("OPENAI_BASE_URL is required")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required")
    if not model:
        raise RuntimeError("OPENAI_MODEL is required")
    return OpenAISettings(
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds
        or int(os.environ.get("OPENAI_TIMEOUT_SECONDS", "1800")),
        max_retries=0,
    )


def langfuse_enabled() -> bool:
    """Langfuse 関連環境変数が設定されているかを返す。"""

    return any(
        os.environ.get(name)
        for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_OTEL_HOST")
    )


def build_langfuse_headers(
    *,
    script_name: str,
    run_id: str,
    unit_id: str,
    attempt: int,
    input_stem: str,
    input_identity: str,
) -> dict[str, str]:
    """OpenAI 互換 API に渡す Langfuse 非秘密ヘッダーを作る。"""

    if not langfuse_enabled():
        return {}
    trace_user_id = (
        os.environ.get("LANGFUSE_TRACE_USER_ID")
        or os.environ.get("USER")
        or "translate-ja"
    )
    session_id = sha256_text(input_identity)[:16]
    return {
        "langfuse_trace_id": run_id or str(uuid.uuid4()),
        "langfuse_trace_name": f"translate-ja:{input_stem}",
        "langfuse_session_id": session_id,
        "langfuse_trace_user_id": trace_user_id,
        "langfuse_tags": json.dumps(["translate-ja", script_name], ensure_ascii=False),
        "langfuse_generation_id": f"{run_id}:{unit_id}:{attempt}",
        "langfuse_generation_name": f"{script_name}:{unit_id}",
    }
