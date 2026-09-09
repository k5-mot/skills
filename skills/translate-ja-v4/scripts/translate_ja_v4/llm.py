"""LangChain model、prompt、structured outputを共通化する。"""

from __future__ import annotations

import base64
import mimetypes
import os
from functools import lru_cache
from math import ceil
from pathlib import Path
from typing import Any, Literal, TypeVar, cast
from urllib.parse import urlparse

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_openai import ChatOpenAI
from langfuse import Langfuse, get_client
from langfuse.langchain import CallbackHandler
from pydantic import BaseModel

from .config import PipelineOptions
from .io import LOGGER

SchemaT = TypeVar("SchemaT", bound=BaseModel)


def _mask_trace_media(*, data: Any, **_kwargs: Any) -> Any:
    """Langfuse traceからbase64 mediaだけを再帰的に除外する。

    Args:
        data: Langfuseが記録しようとする入力、出力、metadata。
        **_kwargs: Langfuse mask callbackの将来互換引数。

    Returns:
        media data URIを固定markerへ置換したJSON互換値。
    """

    if isinstance(data, str) and data.startswith("data:") and ";base64," in data:
        return "<media omitted from trace>"
    if isinstance(data, list):
        return [_mask_trace_media(data=item) for item in data]
    if isinstance(data, tuple):
        return tuple(_mask_trace_media(data=item) for item in data)
    if isinstance(data, dict):
        return {key: _mask_trace_media(data=value) for key, value in data.items()}
    return data


@lru_cache(maxsize=4)
def _langfuse_client(
    public_key: str, secret_key: str, base_url: str | None
) -> Langfuse:
    """同じ設定のLangfuse clientをprocess内で再利用する。

    Args:
        public_key: Langfuse project public key。
        secret_key: Langfuse project secret key。
        base_url: Cloudまたはself-hosted base URL。

    Returns:
        LangChain callbackが参照するLangfuse client。

    Side Effects:
        OpenTelemetry exporterとbackground workerを初期化する。
    """

    return Langfuse(
        public_key=public_key,
        secret_key=secret_key,
        base_url=base_url,
        mask=_mask_trace_media,
    )


def _prepare_langfuse_base_url() -> str | None:
    """Langfuse接続先を検証し、旧環境変数を標準名へ正規化する。

    Returns:
        正規化したbase URL。接続先が未設定ならNone。

    Raises:
        RuntimeError: URLがHTTP(S)の絶対URLでない場合。

    Side Effects:
        `LANGFUSE_BASE_URL` 未設定時にprocess環境へ正規化値を設定する。
    """

    candidates = (
        ("LANGFUSE_BASE_URL", os.getenv("LANGFUSE_BASE_URL")),
        ("LANGFUSE_HOST", os.getenv("LANGFUSE_HOST")),
        ("LANGFUSE_OTEL_HOST", os.getenv("LANGFUSE_OTEL_HOST")),
    )
    source, value = next(((key, item) for key, item in candidates if item), ("", None))
    if value is None:
        return None
    normalized = value.strip().rstrip("/")
    suffix = "/api/public/otel"
    if normalized.endswith(suffix):
        normalized = normalized[: -len(suffix)]
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(f"{source} must be an absolute HTTP(S) URL")
    if not os.getenv("LANGFUSE_BASE_URL"):
        os.environ["LANGFUSE_BASE_URL"] = normalized
        LOGGER.info("Using %s as LANGFUSE_BASE_URL", source)
    return normalized


def _langfuse_config(trace_name: str) -> RunnableConfig:
    """環境変数が揃う場合だけLangfuse callback設定を作る。

    Args:
        trace_name: Langfuseで識別するStage・agent名。

    Returns:
        LangChain runnableへ渡すrun名、tag、任意のcallback。

    Raises:
        RuntimeError: Langfuse credentialが片方だけ設定されている場合。
    """

    public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY")
    if bool(public_key) != bool(secret_key):
        raise RuntimeError(
            "LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY must be set together"
        )
    config: RunnableConfig = {
        "run_name": f"translate-ja-v4.{trace_name}",
        "tags": ["translate-ja-v4", trace_name],
    }
    if public_key and secret_key:
        base_url = _prepare_langfuse_base_url()
        os.environ["LANGFUSE_MEDIA_UPLOAD_ENABLED"] = "false"
        _langfuse_client(public_key, secret_key, base_url)
        config["callbacks"] = [CallbackHandler(public_key=public_key)]
    return config


def flush_langfuse() -> None:
    """有効なLangfuse clientの送信待ちtraceをflushする。

    Returns:
        なし。

    Side Effects:
        credential設定時だけLangfuse endpointへ未送信eventを送る。
    """

    if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
        return
    try:
        public_key = os.environ["LANGFUSE_PUBLIC_KEY"]
        _prepare_langfuse_base_url()
        get_client(public_key=public_key).flush()
    except Exception as error:
        LOGGER.warning("Failed to flush Langfuse traces error=%s", error)


def estimate_output_tokens(
    characters: int,
    elements: int,
    *,
    expansion: float = 1.25,
    per_element_tokens: int = 48,
) -> int:
    """日本語structured outputの保守的なtoken数を文字数から見積もる。

    Args:
        characters: 応答本文に相当する入力文字数。
        elements: 応答objectの要素数。
        expansion: 翻訳や校正による文字数増加係数。
        per_element_tokens: IDやJSON fieldに確保するtoken数。

    Returns:
        切り上げた推定出力token数。
    """

    return ceil(max(0, characters) * expansion) + max(0, elements) * max(
        0, per_element_tokens
    )


def llm_batches(
    items: list[dict[str, Any]],
    max_chars: int,
    max_elements: int,
    max_output_tokens: int,
    *,
    text_key: str = "source",
    expansion: float = 1.25,
    per_element_tokens: int = 48,
) -> list[list[dict[str, Any]]]:
    """入力文字数、件数、推定出力token数でLLM batchを分割する。

    Args:
        items: 文書順の対象。
        max_chars: 入力本文の合計文字数上限。
        max_elements: 件数上限。0なら無制限。
        max_output_tokens: 推定応答token数上限。
        text_key: 出力量見積りに使うfield名。
        expansion: 出力文字数の増加係数。
        per_element_tokens: structured outputの要素別余白。

    Returns:
        順序を維持したbatch配列。単一要素が上限を超える場合も単独で返す。
    """

    result: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    input_size = 0
    output_size = 0
    for item in items:
        source_size = len(str(item.get("source", "")))
        predicted_size = len(str(item.get(text_key, item.get("source", ""))))
        candidate_tokens = estimate_output_tokens(
            output_size + predicted_size,
            len(current) + 1,
            expansion=expansion,
            per_element_tokens=per_element_tokens,
        )
        element_limit = max_elements > 0 and len(current) >= max_elements
        if current and (
            input_size + source_size > max_chars
            or element_limit
            or candidate_tokens > max_output_tokens
        ):
            result.append(current)
            current, input_size, output_size = [], 0, 0
        current.append(item)
        input_size += source_size
        output_size += predicted_size
    if current:
        result.append(current)
    return result


def chat_model(options: PipelineOptions, *, max_tokens: int) -> ChatOpenAI:
    """OpenAI互換設定からLangChain chat modelを作る。

    Args:
        options: context上限を含むパイプライン設定。
        max_tokens: 応答token上限。

    Returns:
        設定済みChatOpenAI。

    Raises:
        RuntimeError: 必須環境変数が不足する場合。
    """

    values = {
        key: os.getenv(key)
        for key in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL")
    }
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"missing OpenAI settings: {', '.join(missing)}")
    return ChatOpenAI(
        model=cast(str, values["OPENAI_MODEL"]),
        base_url=cast(str, values["OPENAI_BASE_URL"]),
        api_key=cast(str, values["OPENAI_API_KEY"]),
        temperature=0,
        timeout=options.request_timeout_seconds,
        max_retries=options.max_retries,
        max_tokens=min(max_tokens, options.max_output_tokens),
    )


def structured_model(
    options: PipelineOptions,
    schema: type[SchemaT],
    *,
    max_tokens: int,
    trace_name: str = "llm",
) -> Runnable[Any, SchemaT]:
    """Pydantic schemaを返すLangChain runnableを作る。

    Args:
        options: LLM接続に使う設定。
        schema: 応答Pydantic model。
        max_tokens: 応答token上限。
        trace_name: Langfuseのrun名とtagに使う識別子。

    Returns:
        検証済みschemaを返すrunnable。
    """

    method = cast(
        Literal["function_calling", "json_mode", "json_schema"],
        os.getenv("OPENAI_STRUCTURED_METHOD", "json_mode"),
    )
    runnable = cast(
        Runnable[Any, SchemaT],
        chat_model(options, max_tokens=max_tokens).with_structured_output(
            schema, method=method
        ),
    )
    return runnable.with_config(_langfuse_config(trace_name))


def prompt_runnable(
    options: PipelineOptions,
    schema: type[SchemaT],
    system: str,
    human: str,
    *,
    max_tokens: int,
    trace_name: str = "llm",
) -> Runnable[dict[str, Any], SchemaT]:
    """ChatPromptTemplateとstructured modelをLCELで結合する。

    Args:
        options: LLM接続に使う設定。
        schema: 応答Pydantic model。
        system: system prompt template。
        human: human prompt template。
        max_tokens: 応答token上限。
        trace_name: Langfuseのrun名とtagに使う識別子。

    Returns:
        template変数dictを受け取るLCEL runnable。
    """

    prompt = ChatPromptTemplate.from_messages([("system", system), ("human", human)])
    return prompt | structured_model(
        options, schema, max_tokens=max_tokens, trace_name=trace_name
    )


def image_messages(
    system: str, prompt: str, image_path: Path | None
) -> list[BaseMessage]:
    """StructureStage用のtextまたは画像付きmessageを作る。

    Args:
        system: system prompt。
        prompt: user prompt。
        image_path: 添付するページ画像。

    Returns:
        LangChain message配列。
    """

    if image_path is None or not image_path.is_file():
        return [SystemMessage(system), HumanMessage(prompt)]
    mimetype = mimetypes.guess_type(image_path.name)[0] or "image/png"
    encoded = base64.b64encode(image_path.read_bytes()).decode()
    return [
        SystemMessage(system),
        HumanMessage(
            content=[
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mimetype};base64,{encoded}"},
                },
            ]
        ),
    ]
