"""LangChain model、prompt、structured outputを共通化する。"""

from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from .config import PipelineOptions

SchemaT = TypeVar("SchemaT", bound=BaseModel)


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
        max_tokens=max_tokens,
    )


def structured_model(
    options: PipelineOptions, schema: type[SchemaT], *, max_tokens: int
) -> Runnable[Any, SchemaT]:
    """Pydantic schemaを返すLangChain runnableを作る。

    Args:
        options: LLM接続に使う設定。
        schema: 応答Pydantic model。
        max_tokens: 応答token上限。

    Returns:
        検証済みschemaを返すrunnable。
    """

    method = cast(
        Literal["function_calling", "json_mode", "json_schema"],
        os.getenv("OPENAI_STRUCTURED_METHOD", "json_mode"),
    )
    return cast(
        Runnable[Any, SchemaT],
        chat_model(options, max_tokens=max_tokens).with_structured_output(
            schema, method=method
        ),
    )


def prompt_runnable(
    options: PipelineOptions,
    schema: type[SchemaT],
    system: str,
    human: str,
    *,
    max_tokens: int,
) -> Runnable[dict[str, Any], SchemaT]:
    """ChatPromptTemplateとstructured modelをLCELで結合する。

    Args:
        options: LLM接続に使う設定。
        schema: 応答Pydantic model。
        system: system prompt template。
        human: human prompt template。
        max_tokens: 応答token上限。

    Returns:
        template変数dictを受け取るLCEL runnable。
    """

    prompt = ChatPromptTemplate.from_messages([("system", system), ("human", human)])
    return prompt | structured_model(options, schema, max_tokens=max_tokens)


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
