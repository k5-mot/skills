"""環境変数とCLI指定を実行設定へ変換する。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

from dotenv import load_dotenv

Backend = Literal["openai", "libretranslate"]
Command = Literal["translate", "review", "register"]


@dataclass(frozen=True)
class Settings:
    """一回の実行に必要な確定済み設定を保持する。"""

    docling_url: str | None
    docling_api_key: str | None
    openai_base_url: str
    openai_api_key: str
    structure_model: str | None
    translation_model: str | None
    review_model: str | None
    embedding_model: str | None
    context_tokens: int
    output_tokens: int
    image_tokens: int
    libretranslate_url: str | None
    libretranslate_api_key: str | None
    langfuse_public_key: str | None
    langfuse_secret_key: str | None
    langfuse_base_url: str | None
    qdrant_url: str | None
    qdrant_api_key: str | None
    qdrant_collection: str | None
    templates_dir: Path

    @property
    def langfuse_enabled(self) -> bool:
        """Langfuse認証情報が完全に設定されているか返す。

        Returns:
            public keyとsecret keyが両方ある場合はTrue。
        """

        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def qdrant_enabled(self) -> bool:
        """Review用Qdrant設定が完全に存在するか返す。

        Returns:
            URL、collection、Embedding modelがある場合はTrue。
        """

        return bool(self.qdrant_url and self.qdrant_collection and self.embedding_model)


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    """環境変数から正の整数を読む。

    Args:
        env: 環境変数mapping。
        name: 変数名。
        default: 未設定時の値。

    Returns:
        検証済みの正整数。

    Raises:
        ValueError: 値が整数でないか1未満の場合。
    """

    try:
        value = int(env.get(name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


def load_settings(
    command: Command,
    backend: Backend = "openai",
    env: Mapping[str, str] | None = None,
) -> Settings:
    """commandに必要な環境変数を検証して設定を返す。

    Args:
        command: 実行する公開subcommand。
        backend: 翻訳に使うbackend。
        env: テスト等で差し替える環境変数。Noneならprocess環境。

    Returns:
        command向けの確定済み設定。

    Raises:
        ValueError: 必須設定不足、部分的な任意設定、予算不整合の場合。
    """

    if env is None:
        load_dotenv()
        env = os.environ
    base = Path(__file__).resolve().parents[1]
    settings = Settings(
        docling_url=env.get("DOCLING_URL"),
        docling_api_key=env.get("DOCLING_API_KEY"),
        openai_base_url=env.get("OPENAI_BASE_URL", ""),
        openai_api_key=env.get("OPENAI_API_KEY", ""),
        structure_model=env.get("OPENAI_STRUCTURE_MODEL"),
        translation_model=env.get("OPENAI_TRANSLATION_MODEL"),
        review_model=env.get("OPENAI_REVIEW_MODEL"),
        embedding_model=env.get("OPENAI_EMBEDDING_MODEL"),
        context_tokens=_positive_int(env, "LLM_CONTEXT_TOKENS", 50_000),
        output_tokens=_positive_int(env, "LLM_OUTPUT_TOKENS", 8_192),
        image_tokens=_positive_int(env, "LLM_IMAGE_TOKENS", 4_096),
        libretranslate_url=env.get("LIBRETRANSLATE_URL"),
        libretranslate_api_key=env.get("LIBRETRANSLATE_API_KEY"),
        langfuse_public_key=env.get("LANGFUSE_PUBLIC_KEY"),
        langfuse_secret_key=env.get("LANGFUSE_SECRET_KEY"),
        langfuse_base_url=env.get("LANGFUSE_BASE_URL"),
        qdrant_url=env.get("QDRANT_URL") or env.get("QDRANT_URI"),
        qdrant_api_key=env.get("QDRANT_API_KEY"),
        qdrant_collection=env.get("QDRANT_COLLECTION"),
        templates_dir=base / "templates",
    )
    _validate_settings(settings, command, backend)
    return settings


def _validate_settings(settings: Settings, command: Command, backend: Backend) -> None:
    """command別の必須設定と任意設定の一貫性を検証する。

    Args:
        settings: 検証対象設定。
        command: 実行する公開subcommand。
        backend: 翻訳に使うbackend。

    Returns:
        なし。

    Raises:
        ValueError: 設定がcommand契約を満たさない場合。
    """

    if settings.output_tokens + settings.image_tokens >= settings.context_tokens:
        raise ValueError("LLM token reservations must be smaller than context")
    # 任意serviceは部分設定を黙って無効化せず、設定ミスとして早期に知らせる。
    langfuse_pair = (settings.langfuse_public_key, settings.langfuse_secret_key)
    if any(langfuse_pair) and not all(langfuse_pair):
        raise ValueError(
            "LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are both required"
        )
    qdrant_core = (settings.qdrant_url, settings.qdrant_collection)
    if any(qdrant_core) and not all(qdrant_core):
        raise ValueError("QDRANT_URL and QDRANT_COLLECTION are both required")
    # 不使用commandの資格情報まで必須にせず、各入口が実際に使う設定だけを要求する。
    if command == "translate":
        required = {
            "DOCLING_URL": settings.docling_url,
            "OPENAI_BASE_URL": settings.openai_base_url,
            "OPENAI_API_KEY": settings.openai_api_key,
            "OPENAI_STRUCTURE_MODEL": settings.structure_model,
            "OPENAI_REVIEW_MODEL": settings.review_model,
        }
        if backend == "openai":
            required["OPENAI_TRANSLATION_MODEL"] = settings.translation_model
        if backend == "libretranslate":
            required["LIBRETRANSLATE_URL"] = settings.libretranslate_url
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"missing settings: {', '.join(missing)}")
    if command == "review":
        required = {
            "OPENAI_BASE_URL": settings.openai_base_url,
            "OPENAI_API_KEY": settings.openai_api_key,
            "OPENAI_REVIEW_MODEL": settings.review_model,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"missing settings: {', '.join(missing)}")
    if settings.qdrant_url and not settings.embedding_model:
        raise ValueError("OPENAI_EMBEDDING_MODEL is required with Qdrant")
    if command == "register":
        required = {
            "OPENAI_BASE_URL": settings.openai_base_url,
            "OPENAI_API_KEY": settings.openai_api_key,
            "OPENAI_EMBEDDING_MODEL": settings.embedding_model,
            "QDRANT_URL": settings.qdrant_url,
            "QDRANT_COLLECTION": settings.qdrant_collection,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"missing settings: {', '.join(missing)}")


def read_rules(
    settings: Settings, name: Literal["structure", "translation", "review"]
) -> str:
    """指定工程だけに対応するRules本文を読む。

    Args:
        settings: template directoryを含む設定。
        name: Rulesを利用する工程名。

    Returns:
        UTF-8のRules本文。
    """

    return (settings.templates_dir / f"{name}-rules.md").read_text(encoding="utf-8")
