"""LiteLLM経由のOpenAI互換APIを狭い契約で呼び出す。"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar, cast

import httpx
from openai import OpenAI

from src.adapters.langfuse import media, observed, update_current
from src.config import Settings

T = TypeVar("T")


class ContextLengthError(RuntimeError):
    """LLMがcontext上限超過を返したことを表す。"""


def _status_code(error: BaseException) -> int | None:
    """HTTP系例外からstatus codeを取り出す。

    Args:
        error: 調べる例外。

    Returns:
        status code。不明ならNone。
    """

    value = getattr(error, "status_code", None)
    if isinstance(value, int):
        return value
    response = getattr(error, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def retry_call(
    call: Callable[[], T], sleeper: Callable[[float], None] = time.sleep
) -> T:
    """一時的な通信障害だけを指数backoffで最大3回試す。

    Args:
        call: 一回の外部呼出しを行う関数。
        sleeper: 再試行待機に使う関数。

    Returns:
        成功した呼出しの戻り値。

    Raises:
        BaseException: 非再試行例外または3回失敗した最後の例外。
    """

    for attempt in range(3):
        try:
            return call()
        except BaseException as error:
            status = _status_code(error)
            # 内容不正や認証失敗は待っても直らないため、一時障害だけを再試行する。
            retryable = (
                isinstance(error, httpx.TransportError)
                or status in {408, 429}
                or bool(status and status >= 500)
            )
            if not retryable or attempt == 2:
                raise
            sleeper(float(2**attempt))
    raise RuntimeError("retry loop ended unexpectedly")


def _client(settings: Settings) -> OpenAI:
    """共有設定からOpenAI clientを作る。

    Args:
        settings: API URLとkeyを含む設定。

    Returns:
        OpenAI client。
    """

    return OpenAI(base_url=settings.openai_base_url, api_key=settings.openai_api_key)


def _image_message(path: Path) -> dict[str, Any]:
    """画像fileをChat Completionsのdata URL messageへ変換する。

    Args:
        path: PNGまたはJPEG画像。

    Returns:
        OpenAI image_url content part。
    """

    suffix = path.suffix.lower()
    media_type = "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{media_type};base64,{encoded}"},
    }


@observed("llm-chat", capture_input=False)
def structured_chat(
    settings: Settings,
    model: str,
    system: str,
    user: str,
    schema_name: str,
    schema: dict[str, Any],
    image_path: Path | None = None,
) -> dict[str, Any]:
    """JSON Schema出力を要求してChat Completionsを呼び出す。

    Args:
        settings: OpenAI互換API設定。
        model: routeで解決するmodel名。
        system: system prompt。
        user: user prompt。
        schema_name: JSON Schema名。
        schema: response JSON Schema。
        image_path: Structure用の任意ページ画像。

    Returns:
        schemaに従うJSON object。

    Raises:
        ContextLengthError: APIがcontext上限超過を返した場合。
        ValueError: responseがJSON objectでない場合。
    """

    trace_input: dict[str, Any] = {
        "model": model,
        "system": system,
        "user": user,
        "schema": schema,
    }
    if image_path is not None:
        trace_input["image"] = media(image_path)
    content: str | list[dict[str, Any]] = user
    if image_path is not None:
        content = [{"type": "text", "text": user}, _image_message(image_path)]
    # JSON Schemaをpromptにも明記し、response_format対応が弱いlocal modelを補助する。
    system_prompt = (
        f"{system}\n\n説明文やMarkdownを含めず、次のJSON Schemaに一致する"
        f"JSON objectだけを返してください:\n"
        f"{json.dumps(schema, ensure_ascii=False)}"
    )

    invalid_response: str | None = None

    def invoke() -> Any:
        """一回のChat Completions requestを送る。

        Returns:
            OpenAI SDK response。
        """

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ]
        if invalid_response is not None:
            # 一から再回答させず不正応答を材料に渡すことで、翻訳内容の揺れを抑える。
            messages.extend(
                [
                    {"role": "assistant", "content": invalid_response},
                    {
                        "role": "user",
                        "content": (
                            "上の不正応答を、情報を増減せず指定JSON Schemaへ"
                            "適合するJSON objectに修復してください。"
                        ),
                    },
                ]
            )
        return _client(settings).chat.completions.create(
            model=model,
            messages=cast(Any, messages),
            response_format={
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
            max_tokens=settings.output_tokens,
            temperature=0,
        )

    # 通信再試行とは分離し、形式修復でAPI呼出しが無制限に増えないよう二回に限る。
    for format_attempt in range(2):
        attempt_label = f" attempt {format_attempt + 1}/2"
        print(f"LLM: {schema_name}{attempt_label} started", flush=True)
        update_current(input={**trace_input, "system": system_prompt})
        try:
            response = retry_call(invoke)
        except BaseException as error:
            message = str(error).casefold()
            if _status_code(error) == 400 and (
                "context" in message or "token" in message
            ):
                raise ContextLengthError(str(error)) from error
            raise
        value = response.choices[0].message.content
        try:
            json_text = (value or "null").strip()
            lines = json_text.splitlines()
            if (
                lines
                and lines[0].casefold() in {"```", "```json"}
                and lines[-1] == "```"
            ):
                json_text = "\n".join(lines[1:-1])
            # local modelが末尾へ短い説明を足しても、先頭のJSON objectは回収する。
            parsed, _ = json.JSONDecoder().raw_decode(json_text)
            if not isinstance(parsed, dict):
                raise ValueError("LLM response must be a JSON object")
            missing = [key for key in schema.get("required", []) if key not in parsed]
            if missing:
                raise ValueError(f"LLM response lacks required keys: {missing}")
        except ValueError as error:
            print(f"LLM: {schema_name}{attempt_label} invalid response", flush=True)
            update_current(output={"invalid_response": value})
            if format_attempt:
                raise ValueError(
                    "LLM did not return the required JSON object"
                ) from error
            invalid_response = value or ""
            continue
        update_current(output=parsed)
        print(f"LLM: {schema_name}{attempt_label} success", flush=True)
        return parsed
    raise RuntimeError("structured response loop ended unexpectedly")


@observed("llm-embeddings", capture_input=False)
def embeddings(settings: Settings, texts: list[str]) -> list[list[float]]:
    """OpenAI互換Embedding APIで文字列列をvector化する。

    Args:
        settings: APIとEmbedding model設定。
        texts: 入力文字列列。

    Returns:
        入力順のfloat vector列。
    """

    if not settings.embedding_model:
        raise ValueError("OPENAI_EMBEDDING_MODEL is required")
    update_current(input={"model": settings.embedding_model, "texts": texts})
    response = retry_call(
        lambda: _client(settings).embeddings.create(
            model=settings.embedding_model or "", input=texts
        )
    )
    ordered = sorted(response.data, key=lambda item: item.index)
    values = [list(item.embedding) for item in ordered]
    update_current(output={"vector_count": len(values)})
    return values
