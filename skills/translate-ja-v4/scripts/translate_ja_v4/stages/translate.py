"""TranslateStageをLangChainまたはLibreTranslateで実装する。"""

from __future__ import annotations

import copy
import json
import os
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any

import httpx
from pydantic import BaseModel, Field

from ..config import (
    PipelineOptions,
    PipelineState,
    TranslationBackend,
    state_options,
    state_paths,
)
from ..document import batches, resolve_target, translation_targets
from ..io import (
    LOGGER,
    glossary_matches,
    hash_file,
    hash_json,
    read_glossary,
    read_json,
    read_rules,
    record_stage,
    stage_cached,
    stage_partial,
    write_json,
)
from ..llm import llm_batches, prompt_runnable

DEFAULT_RULES = "- 日本語へ翻訳する。\n- 指定された外部翻訳ルールに従う。"
PROTECTED_PATTERN = re.compile(
    r"`[^`\n]+`"
    r"|https?://[^\s<>()]+"
    r"|www\.[^\s<>()]+"
    r"|(?<!\w)[A-Za-z]:\\[^\s]+"
    r"|(?<!\w)(?:\.\.?/|~/|/)[A-Za-z0-9_.~+@%/-]+"
    r"|(?<!\w)--?[A-Za-z][A-Za-z0-9-]*"
    r"|\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b"
    r"|\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b"
    r"|\b[a-z]+(?:[A-Z][A-Za-z0-9]*)+\b"
    r"|\b[A-Z][A-Z0-9]{1,}\b"
)


def _protect_text(value: str) -> tuple[str, list[tuple[str, str]]]:
    """機械翻訳で保持すべき断片を衝突しないplaceholderへ置換する。

    Args:
        value: LibreTranslateへ送る原文。

    Returns:
        保護済み文字列とplaceholder・原文断片の対応表。
    """

    prefix = "ZXQKEEP"
    while prefix in value:
        prefix += "X"
    protected: list[tuple[str, str]] = []

    def replace(match: re.Match[str]) -> str:
        """一致断片を一意なplaceholderへ変換する。

        Args:
            match: 保護対象の正規表現match。

        Returns:
            LibreTranslateが翻訳しにくいASCII placeholder。
        """

        placeholder = f"{prefix}{len(protected):06d}QXZ"
        protected.append((placeholder, match.group(0)))
        return placeholder

    return PROTECTED_PATTERN.sub(replace, value), protected


def _restore_text(value: str, protected: list[tuple[str, str]]) -> str:
    """LibreTranslate応答のplaceholderを原文断片へ戻す。

    Args:
        value: LibreTranslateから返された訳文。
        protected: `_protect_text` が作った対応表。

    Returns:
        URL、path、identifierを復元した訳文。

    Raises:
        ValueError: placeholderが欠落または重複している場合。
    """

    restored = value
    for placeholder, original in protected:
        if restored.count(placeholder) != 1:
            raise ValueError(
                f"LibreTranslate changed protected placeholder={placeholder}"
            )
        restored = restored.replace(placeholder, original)
    return restored


class TranslationItem(BaseModel):
    """1要素の翻訳結果を表す。"""

    id: str
    translated_text: str


class TranslationResponse(BaseModel):
    """LLM Translateのstructured output schema。"""

    translations: list[TranslationItem] = Field(default_factory=list)


def _completed(document: dict[str, Any], target: dict[str, Any]) -> bool:
    """部分成果物内で対象が翻訳済みか判定する。

    Args:
        document: 部分成果物JSON。
        target: 翻訳対象。

    Returns:
        translate_ja_v4 metadataがあればTrue。
    """

    return "translate_ja_v4" in resolve_target(document, target["path"])


def _apply(document: dict[str, Any], target: dict[str, Any], translated: str) -> None:
    """対象objectへ翻訳metadataを保存する。

    Args:
        document: 更新対象JSON。
        target: 翻訳対象。
        translated: 日本語訳。

    Returns:
        なし。

    Side Effects:
        translate_ja_v4 metadataを追加する。
    """

    item = resolve_target(document, target["path"])
    source = str(target["source"])
    render = f"{source} / {translated}" if target["kind"] == "heading" else translated
    item["translate_ja_v4"] = {
        "text_en": source,
        "text_ja": translated,
        "render_text": render,
        "kind": target["kind"],
    }


class Translator(ABC):
    """翻訳backendの最小共通契約を定義する。"""

    @abstractmethod
    def translate(self, batch: list[dict[str, Any]]) -> dict[str, str]:
        """1batchを日本語へ翻訳する。

        Args:
            batch: 翻訳対象配列。

        Returns:
            対象IDから日本語訳への対応。
        """


class LLMTranslator(Translator):
    """LangChain structured outputを使う翻訳backend。"""

    def __init__(
        self, options: PipelineOptions, rules: str, glossary: list[dict[str, str]]
    ) -> None:
        """LLM翻訳backendを初期化する。

        Args:
            options: LLMとbatch設定。
            rules: 外部翻訳ルール。
            glossary: 全用語集。

        Returns:
            なし。
        """

        self.options, self.rules, self.glossary = options, rules, glossary
        self.chain = prompt_runnable(
            options,
            TranslationResponse,
            (
                "英語を日本語へ翻訳し、translations配列を持つobjectで回答してください。"
                "各入力IDを1回ずつ含め、translated_textに日本語訳を設定してください。"
            ),
            "翻訳ルール:\n{rules}\n\n入力JSON:\n{items}",
            max_tokens=16_384,
        )

    def translate(self, batch: list[dict[str, Any]]) -> dict[str, str]:
        """LangChain chainで1batchを翻訳する。

        Args:
            batch: 翻訳対象配列。

        Returns:
            対象IDから日本語訳への対応。
        """

        request = [
            {
                "id": item["id"],
                "source_text": item["source"],
                "glossary": glossary_matches(str(item["source"]), self.glossary),
            }
            for item in batch
        ]
        items = json.dumps(request, ensure_ascii=False)
        if len(self.rules) + len(items) + 2_000 > self.options.context_chars:
            raise ValueError("TranslateStage prompt exceeds context_chars")
        response = self.chain.invoke({"rules": self.rules, "items": items})
        values = {
            item.id: item.translated_text.strip() for item in response.translations
        }
        expected = {str(item["id"]) for item in batch}
        if set(values) != expected or any(not value for value in values.values()):
            raise ValueError("LLM translation IDs must exactly match the input")
        return values


class LibreTranslator(Translator):
    """LibreTranslate batch APIを使う翻訳backend。"""

    def __init__(self, options: PipelineOptions) -> None:
        """環境変数とPipelineOptionsからLibreTranslate clientを初期化する。

        Args:
            options: timeoutとretry設定。

        Returns:
            なし。

        Raises:
            RuntimeError: URLが未設定の場合。
        """

        url = os.getenv("LIBRETRANSLATE_URL")
        if not url:
            raise RuntimeError("LIBRETRANSLATE_URL is required")
        self.url = f"{url.rstrip('/')}/translate"
        self.options = options

    def translate(self, batch: list[dict[str, Any]]) -> dict[str, str]:
        """LibreTranslateで1batchを翻訳する。

        Args:
            batch: 翻訳対象配列。

        Returns:
            対象IDから日本語訳への対応。
        """

        protected = [_protect_text(str(item["source"])) for item in batch]
        payload: dict[str, Any] = {
            "q": [value for value, _ in protected],
            "source": "en",
            "target": "ja",
            "format": "text",
        }
        if os.getenv("LIBRETRANSLATE_API_KEY"):
            payload["api_key"] = os.environ["LIBRETRANSLATE_API_KEY"]
        attempts = self.options.max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                response = httpx.post(
                    self.url,
                    json=payload,
                    timeout=self.options.request_timeout_seconds,
                )
                response.raise_for_status()
                values = response.json().get("translatedText")
                if isinstance(values, str):
                    values = [values]
                if not isinstance(values, list) or len(values) != len(batch):
                    raise ValueError("LibreTranslate response count mismatch")
                return {
                    str(item["id"]): _restore_text(str(value), replacements)
                    for item, value, (_, replacements) in zip(
                        batch, values, protected, strict=True
                    )
                }
            except httpx.HTTPError as error:
                response = getattr(error, "response", None)
                status = getattr(response, "status_code", None)
                retryable = isinstance(error, httpx.TransportError) or status in {
                    408,
                    409,
                    429,
                    500,
                    502,
                    503,
                    504,
                }
                if attempt == attempts or not retryable:
                    raise
                delay = min(
                    self.options.retry_max_seconds,
                    self.options.retry_initial_seconds * (2 ** (attempt - 1)),
                )
                LOGGER.warning(
                    "Retrying LibreTranslate request attempt=%s max_attempts=%s delay=%.1f",
                    attempt,
                    attempts,
                    delay,
                )
                time.sleep(delay)
        raise RuntimeError("LibreTranslate request attempts exhausted")


def _translate_with_fallback(
    backend: Translator,
    batch: list[dict[str, Any]],
    *,
    split_on_error: bool,
) -> Iterator[tuple[list[dict[str, Any]], dict[str, str]]]:
    """LLM失敗時に要素数を半減し、成功したsub-batchを順次返す。

    Args:
        backend: 利用する翻訳backend。
        batch: 翻訳対象batch。
        split_on_error: 失敗時に再分割するか。

    Yields:
        成功したsub-batchと翻訳結果。

    Raises:
        Exception: 1要素でもbackendが失敗した場合。
    """

    try:
        yield batch, backend.translate(batch)
    except Exception:
        if not split_on_error or len(batch) == 1:
            raise
        middle = len(batch) // 2
        LOGGER.warning(
            "TranslateStage batch failed; retrying with smaller batches elements=%s->%s",
            len(batch),
            middle,
        )
        yield from _translate_with_fallback(
            backend, batch[:middle], split_on_error=True
        )
        yield from _translate_with_fallback(
            backend, batch[middle:], split_on_error=True
        )


def translate_stage(state: PipelineState) -> PipelineState:
    """本文を選択backendで日本語化するLangGraph node。

    Args:
        state: Clean成果物を含むgraph state。

    Returns:
        Translate成果物パスを設定した部分state。
    """

    options, paths = state_options(state), state_paths(state)
    glossary = (
        read_glossary(options.glossary)
        if options.translator == TranslationBackend.LLM
        else []
    )
    rules = (
        read_rules(options.translation_rules, DEFAULT_RULES)
        if options.translator == TranslationBackend.LLM
        else ""
    )
    input_hash = hash_file(paths.cleaned_json)
    config_hash = hash_json(
        {
            "version": 2,
            "backend": options.translator,
            "batch_chars": options.batch_chars,
            "context_chars": options.context_chars,
            "max_elements": options.max_batch_elements,
            "max_output_tokens": options.max_output_tokens,
            "rules": rules,
            "glossary": glossary,
            "model": os.getenv("OPENAI_MODEL")
            if options.translator == TranslationBackend.LLM
            else None,
            "libre_url": os.getenv("LIBRETRANSLATE_URL")
            if options.translator == TranslationBackend.DEFAULT
            else None,
        }
    )
    if stage_cached(
        paths.manifest, "translate", input_hash, config_hash, paths.translated_json
    ):
        LOGGER.info("Resumed TranslateStage output=%s", paths.translated_json)
        return {
            "current_path": str(paths.translated_json),
            "completed_stage": "translate",
        }
    source = read_json(paths.cleaned_json)
    document = (
        read_json(paths.translated_json)
        if stage_partial(
            paths.manifest, "translate", input_hash, config_hash, paths.translated_json
        )
        else copy.deepcopy(source)
    )
    targets = translation_targets(source)
    pending = [target for target in targets if not _completed(document, target)]
    record_stage(
        paths.manifest,
        "translate",
        "running",
        input_hash,
        config_hash,
        paths.translated_json,
        {"total": len(targets), "completed": len(targets) - len(pending)},
    )
    backend: Translator = (
        LLMTranslator(options, rules, glossary)
        if options.translator == TranslationBackend.LLM
        else LibreTranslator(options)
    )
    request_limit = options.batch_chars
    if options.translator == TranslationBackend.LLM:
        request_limit = min(
            request_limit, max(1, options.context_chars - len(rules) - 2_000)
        )
    pending_batches = (
        llm_batches(
            pending,
            request_limit,
            options.max_batch_elements,
            options.max_output_tokens,
        )
        if options.translator == TranslationBackend.LLM
        else batches(pending, request_limit, options.max_batch_elements)
    )
    for batch in pending_batches:
        results = _translate_with_fallback(
            backend,
            batch,
            split_on_error=options.translator == TranslationBackend.LLM,
        )
        for completed_batch, translations in results:
            for target in completed_batch:
                _apply(document, target, translations[str(target["id"])])
            write_json(paths.translated_json, document)
            done = sum(_completed(document, target) for target in targets)
            record_stage(
                paths.manifest,
                "translate",
                "running",
                input_hash,
                config_hash,
                paths.translated_json,
                {"total": len(targets), "completed": done},
            )
    write_json(paths.translated_json, document)
    record_stage(
        paths.manifest,
        "translate",
        "completed",
        input_hash,
        config_hash,
        paths.translated_json,
        {"total": len(targets), "completed": len(targets)},
    )
    return {"current_path": str(paths.translated_json), "completed_stage": "translate"}
