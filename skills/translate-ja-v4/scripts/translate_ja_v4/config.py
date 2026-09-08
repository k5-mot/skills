"""パイプライン設定と成果物パスを定義する。"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import TypedDict


class FrozenModel(BaseModel):
    """変更不能なPydantic modelの共通設定を提供する。"""

    model_config = ConfigDict(frozen=True)


class TranslationBackend(StrEnum):
    """TranslateStageで利用できるbackendを表す。"""

    DEFAULT = "default"
    LLM = "llm"


class PipelineOptions(FrozenModel):
    """CLIから受け取るパイプライン設定を保持する。"""

    input: Path
    output_dir: Path | None = None
    output: Path | None = None
    template: Path | None = None
    env: Path = Path(".env")
    glossary: Path | None = None
    structure_rules: Path | None = None
    translation_rules: Path | None = None
    review_rules: Path | None = None
    translator: TranslationBackend = TranslationBackend.DEFAULT
    context_chars: int = Field(default=50_000, ge=1)
    batch_chars: int = Field(default=20_000, ge=1)
    max_batch_elements: int = Field(default=0, ge=0)
    max_output_tokens: int = Field(default=16_384, ge=256)
    request_timeout_seconds: float = Field(default=1_800, ge=1)
    max_retries: int = Field(default=5, ge=0)
    retry_initial_seconds: float = Field(default=1, ge=0)
    retry_max_seconds: float = Field(default=30, ge=0)
    stage_max_attempts: int = Field(default=2, ge=1)
    stage_retry_initial_seconds: float = Field(default=1, gt=0)
    stage_retry_max_seconds: float = Field(default=8, gt=0)
    pdf_chunk_pages: int = Field(default=10, ge=1)
    skip_vlm: bool = False
    skip_review: bool = False
    skip_docx: bool = False
    review_rag: bool = False
    force: bool = False


class StagePaths(FrozenModel):
    """各Stageが読み書きするパスを保持する。"""

    output_dir: Path
    document_json: Path
    normalized_json: Path
    structured_json: Path
    cleaned_json: Path
    translated_json: Path
    reviewed_json: Path
    markdown: Path
    docx: Path
    artifacts: Path
    manifest: Path
    checkpoints: Path


class PipelineState(TypedDict, total=False):
    """LangGraph node間で受け渡す軽量な直列化可能stateを表す。"""

    options: dict[str, Any]
    paths: dict[str, Any]
    current_path: str
    completed_stage: str


def build_paths(options: PipelineOptions) -> StagePaths:
    """CLI設定から固定の成果物パスを作る。

    Args:
        options: 入力と出力先を含むCLI設定。

    Returns:
        全Stageの成果物パス。
    """

    source = options.input.resolve()
    output_dir = (
        options.output_dir.resolve()
        if options.output_dir
        else Path("outputs").resolve() / source.stem
    )
    return StagePaths(
        output_dir=output_dir,
        document_json=output_dir / f"{source.stem}.json",
        normalized_json=output_dir / "document.normalized.json",
        structured_json=output_dir / "document.structured.json",
        cleaned_json=output_dir / "document.cleaned.json",
        translated_json=output_dir / "document.translated.json",
        reviewed_json=output_dir / "document.reviewed.json",
        markdown=output_dir / "document.ja.md",
        docx=options.output.resolve()
        if options.output
        else output_dir / "document.ja.docx",
        artifacts=output_dir / "artifacts",
        manifest=output_dir / "manifest.json",
        checkpoints=output_dir / ".langgraph.sqlite3",
    )


def state_options(state: PipelineState) -> PipelineOptions:
    """graph stateから型付きCLI設定を復元する。

    Args:
        state: LangGraphの共有state。

    Returns:
        検証済みPipelineOptions。
    """

    return PipelineOptions.model_validate(state["options"])


def state_paths(state: PipelineState) -> StagePaths:
    """graph stateから型付き成果物パスを復元する。

    Args:
        state: LangGraphの共有state。

    Returns:
        検証済みStagePaths。
    """

    return StagePaths.model_validate(state["paths"])


def initial_state(options: PipelineOptions, paths: StagePaths) -> PipelineState:
    """LangGraphへ渡す初期stateを作る。

    Args:
        options: CLI設定。
        paths: 成果物パス。

    Returns:
        JSON互換値だけを含む初期state。
    """

    return {
        "options": options.model_dump(mode="json"),
        "paths": paths.model_dump(mode="json"),
        "current_path": str(options.input.resolve()),
    }


def thread_id(options: PipelineOptions, paths: StagePaths) -> str:
    """入力と出力先に固有のLangGraph thread IDを返す。

    Args:
        options: CLI設定。
        paths: 成果物パス。

    Returns:
        安定した短いSHA-256文字列。
    """

    payload = json.dumps(
        [str(options.input.resolve()), str(paths.output_dir)], ensure_ascii=False
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:24]
