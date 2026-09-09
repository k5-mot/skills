"""英日PDFの対応付けとmulti-agent翻訳レビューを実装する。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from collections.abc import Iterator
from enum import StrEnum
from math import ceil
from pathlib import Path
from typing import Any, Literal, cast

from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from langchain_qdrant import QdrantVectorStore
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from qdrant_client import QdrantClient
from typing_extensions import TypedDict

SKILLS_DIR = Path(__file__).resolve().parents[3]
V4_SCRIPTS = SKILLS_DIR / "translate-ja-v4" / "scripts"
if not V4_SCRIPTS.is_dir():
    raise RuntimeError("review-enja requires sibling skill translate-ja-v4")
if str(V4_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(V4_SCRIPTS))

from translate_ja_v4.config import (  # noqa: E402
    PipelineOptions as V4Options,
    build_paths as build_v4_paths,
    initial_state as initial_v4_state,
)
from translate_ja_v4.document import iter_table_cells  # noqa: E402
from translate_ja_v4.io import (  # noqa: E402
    configure_logging,
    glossary_matches,
    hash_file,
    hash_json,
    load_environment,
    read_glossary,
    read_json,
    read_rules,
    text_of,
    write_bytes,
    write_json,
)
from translate_ja_v4.llm import (  # noqa: E402
    flush_langfuse,
    prompt_runnable,
)
from translate_ja_v4.stages.normalize import normalize_stage as v4_normalize  # noqa: E402
from translate_ja_v4.stages.parse import parse_stage as v4_parse  # noqa: E402

LOGGER = logging.getLogger("review-enja")
DEFAULT_RULES = "- 英日翻訳をレビューする。\n- 指定された外部Reviewルールに従う。"
SKIP_LABELS = {"code", "program_listing", "page_header", "page_footer"}


class FrozenModel(BaseModel):
    """変更不能なPydantic modelの共通設定を提供する。"""

    model_config = ConfigDict(frozen=True)


class ReviewOptions(FrozenModel):
    """CLIから受け取るレビュー設定を保持する。"""

    source: Path
    translation: Path
    output_dir: Path
    env: Path = Path(".env")
    glossary: Path | None = None
    review_rules: Path | None = None
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
    review_rag: bool = False
    force: bool = False


class ReviewPaths(FrozenModel):
    """レビューPipelineの固定成果物パスを保持する。"""

    output_dir: Path
    source_dir: Path
    translation_dir: Path
    aligned_json: Path
    reviewed_json: Path
    report: Path
    manifest: Path
    checkpoints: Path


class ReviewState(TypedDict, total=False):
    """LangGraph node間で共有する直列化可能stateを表す。"""

    options: dict[str, Any]
    paths: dict[str, Any]
    source_json: str
    translation_json: str
    aligned_json: str
    reviewed_json: str
    report: str
    completed_stage: str


class AlignmentItem(BaseModel):
    """英日要素の一つの多対多対応を表す。"""

    source_ids: list[str] = Field(default_factory=list)
    translation_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class AlignmentResponse(BaseModel):
    """AlignmentStageのstructured output schemaを表す。"""

    alignments: list[AlignmentItem] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_root_array(cls, value: Any) -> Any:
        """modelが返したroot配列を正規objectへ変換する。

        Args:
            value: Pydantic検証前のAlignment応答。

        Returns:
            root配列をalignments fieldへ包んだ値。それ以外は元の値。
        """

        return {"alignments": value} if isinstance(value, list) else value


class ReviewStatus(StrEnum):
    """最終Reviewの合否を表す。"""

    PASS = "pass"
    NEEDS_CHANGE = "needs_change"


class ReviewSeverity(StrEnum):
    """指摘の重大度を表す。"""

    INFO = "info"
    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"


class ReviewFinding(BaseModel):
    """ReviewerまたはAdjudicatorの1対応分の回答を表す。"""

    id: str
    status: ReviewStatus
    severity: ReviewSeverity
    categories: list[str] = Field(default_factory=list)
    suggested_translation: str
    reason: str


class ReviewResponse(BaseModel):
    """Review agentのstructured output schemaを表す。"""

    reviews: list[ReviewFinding] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_root_array(cls, value: Any) -> Any:
        """modelが返したroot配列を正規objectへ変換する。

        Args:
            value: Pydantic検証前のReview応答。

        Returns:
            root配列をreviews fieldへ包んだ値。それ以外は元の値。
        """

        return {"reviews": value} if isinstance(value, list) else value


class ReviewBatchState(TypedDict, total=False):
    """Review subgraph内で共有するbatch stateを表す。"""

    options: dict[str, Any]
    items: list[dict[str, Any]]
    rules: str
    fidelity: dict[str, dict[str, Any]]
    terminology: dict[str, dict[str, Any]]
    final: dict[str, dict[str, Any]]


def build_paths(options: ReviewOptions) -> ReviewPaths:
    """CLI設定から固定成果物パスを作る。

    Args:
        options: 出力先を含むレビュー設定。

    Returns:
        全Stageの成果物パス。
    """

    root = options.output_dir.resolve()
    return ReviewPaths(
        output_dir=root,
        source_dir=root / "source",
        translation_dir=root / "translation",
        aligned_json=root / "document.aligned.json",
        reviewed_json=root / "document.reviewed.json",
        report=root / "review.md",
        manifest=root / "manifest.json",
        checkpoints=root / ".langgraph.sqlite3",
    )


def _options(state: ReviewState) -> ReviewOptions:
    """graph stateから型付き設定を復元する。

    Args:
        state: LangGraph共有state。

    Returns:
        検証済みReviewOptions。
    """

    return ReviewOptions.model_validate(state["options"])


def _paths(state: ReviewState) -> ReviewPaths:
    """graph stateから型付き成果物パスを復元する。

    Args:
        state: LangGraph共有state。

    Returns:
        検証済みReviewPaths。
    """

    return ReviewPaths.model_validate(state["paths"])


def _combined_hash(paths: list[Path]) -> str:
    """複数fileの内容を順序付きでhash化する。

    Args:
        paths: hash対象file配列。

    Returns:
        16進SHA-256文字列。
    """

    return hash_json([hash_file(path) for path in paths])


def _stage_cached(
    manifest_path: Path,
    stage: str,
    input_hash: str,
    config_hash: str,
    output_path: Path,
) -> bool:
    """manifestと成果物hashからStageをResumeできるか判定する。

    Args:
        manifest_path: manifest.json。
        stage: Stage識別子。
        input_hash: 現在の入力hash。
        config_hash: 現在の設定hash。
        output_path: 期待する成果物。

    Returns:
        完了成果物を安全に再利用できる場合はTrue。
    """

    if not manifest_path.is_file() or not output_path.is_file():
        return False
    record = read_json(manifest_path).get("stages", {}).get(stage, {})
    return bool(
        record.get("status") == "completed"
        and record.get("input_sha256") == input_hash
        and record.get("config_sha256") == config_hash
        and record.get("output_sha256") == hash_file(output_path)
    )


def _stage_partial(
    manifest_path: Path,
    stage: str,
    input_hash: str,
    config_hash: str,
    output_path: Path,
) -> bool:
    """要素単位Resumeが可能な部分成果物か判定する。

    Args:
        manifest_path: manifest.json。
        stage: Stage識別子。
        input_hash: 現在の入力hash。
        config_hash: 現在の設定hash。
        output_path: 部分成果物。

    Returns:
        入力と設定が一致するrunning成果物ならTrue。
    """

    if not manifest_path.is_file() or not output_path.is_file():
        return False
    record = read_json(manifest_path).get("stages", {}).get(stage, {})
    return bool(
        record.get("status") == "running"
        and record.get("input_sha256") == input_hash
        and record.get("config_sha256") == config_hash
    )


def _record_stage(
    manifest_path: Path,
    stage: str,
    status: str,
    input_hash: str,
    config_hash: str,
    output_path: Path,
    details: dict[str, Any] | None = None,
) -> None:
    """Stage状態と成果物hashをatomicに記録する。

    Args:
        manifest_path: manifest.json。
        stage: Stage識別子。
        status: runningまたはcompleted。
        input_hash: 入力hash。
        config_hash: 設定hash。
        output_path: Stage成果物。
        details: 追加の進捗・監査情報。

    Returns:
        なし。

    Side Effects:
        manifest.jsonを更新する。
    """

    manifest = read_json(manifest_path) if manifest_path.is_file() else {}
    manifest.setdefault("schema_version", 1)
    record: dict[str, Any] = {
        "status": status,
        "input_sha256": input_hash,
        "config_sha256": config_hash,
        "output": str(output_path),
    }
    if output_path.is_file():
        record["output_sha256"] = hash_file(output_path)
    if details:
        record.update(details)
    manifest.setdefault("stages", {})[stage] = record
    manifest["stage"] = stage
    write_json(manifest_path, manifest)
    log = LOGGER.info if status == "completed" else LOGGER.debug
    log("Stage %s stage=%s output=%s", status, stage, output_path)


def _v4_options(options: ReviewOptions, source: Path, output_dir: Path) -> V4Options:
    """review設定をParse・Normalize用v4設定へ変換する。

    Args:
        options: 共通retry設定を持つレビュー設定。
        source: 英語または日本語PDF。
        output_dir: 当該PDFの中間成果物directory。

    Returns:
        ParseStageとNormalizeStageに必要なv4設定。
    """

    return V4Options(
        input=source,
        output_dir=output_dir,
        env=options.env,
        request_timeout_seconds=options.request_timeout_seconds,
        max_retries=options.max_retries,
        retry_initial_seconds=options.retry_initial_seconds,
        retry_max_seconds=options.retry_max_seconds,
        pdf_chunk_pages=options.pdf_chunk_pages,
        context_chars=options.context_chars,
        batch_chars=options.batch_chars,
        max_batch_elements=options.max_batch_elements,
        max_output_tokens=options.max_output_tokens,
        force=options.force,
    )


def _parse_one(options: ReviewOptions, source: Path, output_dir: Path) -> Path:
    """一つのPDFをv4 ParseStageとNormalizeStageで処理する。

    Args:
        options: Parse設定。
        source: 処理対象PDF。
        output_dir: 中間成果物directory。

    Returns:
        normalized Docling JSONのパス。

    Side Effects:
        Docling Serveを呼び、v4形式のJSON、artifact、manifestを生成する。
    """

    v4_options = _v4_options(options, source, output_dir)
    v4_paths = build_v4_paths(v4_options)
    state = initial_v4_state(v4_options, v4_paths)
    state.update(v4_parse(state))
    state.update(v4_normalize(state))
    return v4_paths.normalized_json


def parse_stage(state: ReviewState) -> ReviewState:
    """英日PDFを解析・正規化するLangGraph nodeを実行する。

    Args:
        state: Review設定と成果物パスを含むgraph state。

    Returns:
        英日normalized JSONパスを設定した部分state。
    """

    options, paths = _options(state), _paths(state)
    for path, label in (
        (options.source, "source"),
        (options.translation, "translation"),
    ):
        if path.suffix.lower() != ".pdf" or not path.is_file():
            raise ValueError(f"{label} must be an existing PDF: {path}")
    source_json = _parse_one(options, options.source.resolve(), paths.source_dir)
    translation_json = _parse_one(
        options, options.translation.resolve(), paths.translation_dir
    )
    input_hash = _combined_hash([options.source, options.translation])
    config_hash = hash_json(
        {
            "version": 1,
            "pdf_chunk_pages": options.pdf_chunk_pages,
            "docling_url": os.getenv("DOCLING_SERVER_URL")
            or os.getenv("DOCLING_SERVE_URL"),
        }
    )
    _record_stage(
        paths.manifest,
        "parse",
        "completed",
        input_hash,
        config_hash,
        source_json,
        {
            "source_output": str(source_json),
            "source_sha256": hash_file(source_json),
            "translation_output": str(translation_json),
            "translation_sha256": hash_file(translation_json),
        },
    )
    return {
        "source_json": str(source_json),
        "translation_json": str(translation_json),
        "completed_stage": "parse",
    }


def _page_no(item: dict[str, Any]) -> int | None:
    """Docling要素の先頭ページ番号を返す。

    Args:
        item: provenanceを持つDocling要素。

    Returns:
        ページ番号。存在しなければNone。
    """

    provenance = item.get("prov")
    if isinstance(provenance, list) and provenance and isinstance(provenance[0], dict):
        page = provenance[0].get("page_no")
        return page if isinstance(page, int) else None
    return None


def _segments(document: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
    """Docling本文、表題、表セルを順序付きReview要素へ変換する。

    Args:
        document: normalized Docling JSON。
        prefix: 英日を区別するID prefix。

    Returns:
        ID、text、kind、pageを持つReview要素配列。
    """

    result: list[dict[str, Any]] = []
    headings = {"title", "section_header", "heading", "header"}
    for index, item in enumerate(document.get("texts", [])):
        if not isinstance(item, dict):
            continue
        text, label = text_of(item).strip(), str(item.get("label", "paragraph"))
        if text and label not in SKIP_LABELS:
            result.append(
                {
                    "id": f"{prefix}:#/texts/{index}",
                    "text": text,
                    "kind": "heading" if label in headings else "body",
                    "page": _page_no(item),
                }
            )
    for table_index, table in enumerate(document.get("tables", [])):
        if not isinstance(table, dict):
            continue
        caption = table.get("caption") or table.get("title")
        if isinstance(caption, str) and caption.strip():
            result.append(
                {
                    "id": f"{prefix}:#/tables/{table_index}/caption",
                    "text": caption.strip(),
                    "kind": "heading",
                    "page": _page_no(table),
                }
            )
        for ref, _path, cell, _row, _column in iter_table_cells(table, table_index):
            text = text_of(cell).strip()
            if text:
                result.append(
                    {
                        "id": f"{prefix}:{ref}",
                        "text": text,
                        "kind": "table_cell",
                        "page": _page_no(table),
                    }
                )
    return result


def _partition(items: list[dict[str, Any]], groups: int) -> list[list[dict[str, Any]]]:
    """文字量の累積比で順序付き要素を指定数へ分割する。

    Args:
        items: 文書順の要素。
        groups: 作成するgroup数。

    Returns:
        元の順序と全要素を維持したgroup配列。
    """

    result: list[list[dict[str, Any]]] = [[] for _ in range(groups)]
    weights = [max(1, len(str(item["text"]))) for item in items]
    total, consumed = sum(weights), 0
    for item, weight in zip(items, weights, strict=True):
        position = consumed + weight / 2
        index = min(groups - 1, int(position * groups / max(1, total)))
        result[index].append(item)
        consumed += weight
    return result


def _paired_batches(
    sources: list[dict[str, Any]],
    translations: list[dict[str, Any]],
    max_chars: int,
    max_elements: int,
) -> list[tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
    """英日文書を相対文字位置が近いAlignment batchへ分割する。

    Args:
        sources: 英語要素配列。
        translations: 日本語要素配列。
        max_chars: 両言語を合計したbatch文字数の目安。
        max_elements: 両言語を合計した要素数上限。0は無制限。

    Returns:
        対応候補となる英日要素batch配列。
    """

    total_chars = sum(len(str(item["text"])) for item in sources + translations)
    groups = max(1, ceil(total_chars / max_chars))
    if max_elements:
        groups = max(groups, ceil((len(sources) + len(translations)) / max_elements))
    source_groups = _partition(sources, groups)
    translation_groups = _partition(translations, groups)
    return [
        (source_group, translation_group)
        for source_group, translation_group in zip(
            source_groups, translation_groups, strict=True
        )
        if source_group or translation_group
    ]


def _validate_alignments(
    alignments: list[AlignmentItem],
    sources: list[dict[str, Any]],
    translations: list[dict[str, Any]],
) -> None:
    """Alignment応答が両入力IDを順序通り一度ずつ含むか検証する。

    Args:
        alignments: LLMが返した対応配列。
        sources: 入力英語要素。
        translations: 入力日本語要素。

    Returns:
        なし。

    Raises:
        ValueError: 空対応、ID欠落、重複、順序変更がある場合。
    """

    if any(not item.source_ids and not item.translation_ids for item in alignments):
        raise ValueError("Alignment must contain at least one source or translation ID")
    source_ids = [item["id"] for item in sources]
    translation_ids = [item["id"] for item in translations]
    if [value for item in alignments for value in item.source_ids] != source_ids:
        raise ValueError("Alignment source IDs must exactly match input order")
    if [
        value for item in alignments for value in item.translation_ids
    ] != translation_ids:
        raise ValueError("Alignment translation IDs must exactly match input order")


def _align_batch(
    options: ReviewOptions,
    sources: list[dict[str, Any]],
    translations: list[dict[str, Any]],
) -> Iterator[AlignmentItem]:
    """一つの英日batchをLLMで対応付け、失敗時は半分へ分割する。

    Args:
        options: LLMとcontext設定。
        sources: 英語要素batch。
        translations: 日本語要素batch。

    Yields:
        検証済みAlignment要素。

    Raises:
        Exception: 最小batchでもAlignmentに失敗した場合。
    """

    if not sources or not translations:
        for item in sources:
            yield AlignmentItem(source_ids=[str(item["id"])])
        for item in translations:
            yield AlignmentItem(translation_ids=[str(item["id"])])
        return
    payload = json.dumps(
        {"source": sources, "translation": translations}, ensure_ascii=False
    )
    if len(payload) + 2_000 > options.context_chars:
        error: Exception = ValueError("AlignmentStage prompt exceeds context_chars")
    else:
        try:
            chain = prompt_runnable(
                _v4_options(options, options.source, options.output_dir),
                AlignmentResponse,
                (
                    "英語原文と日本語訳のAlignment Agentとして、外部textを生成せず"
                    "alignments配列を持つobjectで回答してください。各IDを入力順のまま"
                    "正確に1回だけ使い、1対多・多対1・欠落・追加を表現してください。"
                ),
                "入力JSON:\n{items}",
                max_tokens=options.max_output_tokens,
                trace_name="review-enja-alignment",
            )
            response = chain.invoke({"items": payload})
            _validate_alignments(response.alignments, sources, translations)
            yield from response.alignments
            return
        except Exception as caught:
            error = caught
    if len(sources) + len(translations) <= 2:
        raise error
    source_middle = (len(sources) + 1) // 2
    translation_middle = (len(translations) + 1) // 2
    LOGGER.warning(
        "AlignmentStage batch failed; retrying with smaller batches elements=%s->%s",
        len(sources) + len(translations),
        max(source_middle + translation_middle, 1),
    )
    yield from _align_batch(
        options, sources[:source_middle], translations[:translation_middle]
    )
    yield from _align_batch(
        options, sources[source_middle:], translations[translation_middle:]
    )


def _materialize_alignments(
    raw: list[AlignmentItem],
    sources: list[dict[str, Any]],
    translations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """IDだけのAlignment応答へ信頼済み原文、訳文、ページを結合する。

    Args:
        raw: 検証済みAlignment応答。
        sources: 全英語要素。
        translations: 全日本語要素。

    Returns:
        安定IDと原文・訳文を持つAlignment配列。
    """

    source_by_id = {str(item["id"]): item for item in sources}
    translation_by_id = {str(item["id"]): item for item in translations}
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw, 1):
        source_items = [source_by_id[item_id] for item_id in item.source_ids]
        translation_items = [
            translation_by_id[item_id] for item_id in item.translation_ids
        ]
        status = (
            "aligned"
            if source_items and translation_items
            else "source_only"
            if source_items
            else "translation_only"
        )
        result.append(
            {
                "id": f"A{index:06d}",
                "status": status,
                "source_ids": item.source_ids,
                "translation_ids": item.translation_ids,
                "source_pages": sorted(
                    {
                        value["page"]
                        for value in source_items
                        if value["page"] is not None
                    }
                ),
                "translation_pages": sorted(
                    {
                        value["page"]
                        for value in translation_items
                        if value["page"] is not None
                    }
                ),
                "source_text": "\n".join(str(value["text"]) for value in source_items),
                "translated_text": "\n".join(
                    str(value["text"]) for value in translation_items
                ),
                "alignment_reason": item.reason,
            }
        )
    return result


def alignment_stage(state: ReviewState) -> ReviewState:
    """英日要素をLLMで対応付けるLangGraph nodeを実行する。

    Args:
        state: normalized英日JSONを含むgraph state。

    Returns:
        Alignment成果物パスを設定した部分state。
    """

    options, paths = _options(state), _paths(state)
    source_path, translation_path = (
        Path(state["source_json"]),
        Path(state["translation_json"]),
    )
    input_hash = _combined_hash([source_path, translation_path])
    config_hash = hash_json(
        {
            "version": 1,
            "context_chars": options.context_chars,
            "batch_chars": options.batch_chars,
            "max_batch_elements": options.max_batch_elements,
            "max_output_tokens": options.max_output_tokens,
            "model": os.getenv("OPENAI_MODEL"),
        }
    )
    if _stage_cached(
        paths.manifest, "alignment", input_hash, config_hash, paths.aligned_json
    ):
        LOGGER.info("Resumed AlignmentStage output=%s", paths.aligned_json)
        return {
            "aligned_json": str(paths.aligned_json),
            "completed_stage": "alignment",
        }
    sources = _segments(read_json(source_path), "en")
    translations = _segments(read_json(translation_path), "ja")
    if not sources or not translations:
        raise ValueError("Both PDFs must contain reviewable text")
    raw: list[AlignmentItem] = []
    for source_batch, translation_batch in _paired_batches(
        sources,
        translations,
        options.batch_chars,
        options.max_batch_elements,
    ):
        raw.extend(_align_batch(options, source_batch, translation_batch))
    _validate_alignments(raw, sources, translations)
    alignments = _materialize_alignments(raw, sources, translations)
    write_json(
        paths.aligned_json,
        {
            "schema_version": 1,
            "source": str(options.source.resolve()),
            "translation": str(options.translation.resolve()),
            "alignments": alignments,
        },
    )
    _record_stage(
        paths.manifest,
        "alignment",
        "completed",
        input_hash,
        config_hash,
        paths.aligned_json,
        {
            "total": len(alignments),
            "aligned": sum(item["status"] == "aligned" for item in alignments),
            "source_only": sum(item["status"] == "source_only" for item in alignments),
            "translation_only": sum(
                item["status"] == "translation_only" for item in alignments
            ),
        },
    )
    return {
        "aligned_json": str(paths.aligned_json),
        "completed_stage": "alignment",
    }


def _retriever() -> Any:
    """環境変数から任意のQdrant retrieverを作る。

    Returns:
        設定済みVectorStoreRetriever。

    Raises:
        RuntimeError: Qdrantまたはembedding設定が不足する場合。
    """

    url = os.getenv("QDRANT_URI") or os.getenv("QDRANT_URL")
    key = os.getenv("QDRANT_API_KEY")
    if not url or not key:
        raise RuntimeError("QDRANT_URI and QDRANT_API_KEY are required")
    client = QdrantClient(url=url, api_key=key)
    collection = os.getenv("QDRANT_COLLECTION")
    if not collection:
        names = [item.name for item in client.get_collections().collections]
        if len(names) != 1:
            raise RuntimeError(
                "QDRANT_COLLECTION is required unless exactly one collection exists"
            )
        collection = names[0]
    embeddings = OpenAIEmbeddings(
        model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
        base_url=os.getenv("OPENAI_BASE_URL"),
        api_key=SecretStr(os.getenv("OPENAI_API_KEY", "")),
    )
    store = QdrantVectorStore(
        client,
        collection,
        embeddings,
        vector_name=os.getenv("QDRANT_VECTOR_NAME", ""),
    )
    return store.as_retriever(search_kwargs={"k": int(os.getenv("QDRANT_TOP_K", "3"))})


def _evidence(
    retriever: Any, items: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """原文をQdrantでbatch検索してID別根拠へ整形する。

    Args:
        retriever: Qdrant VectorStoreRetriever。
        items: Review対象Alignment配列。

    Returns:
        Alignment IDごとの出典付き根拠。
    """

    documents: list[list[Document]] = retriever.batch(
        [str(item["source_text"]) for item in items]
    )
    return {
        str(item["id"]): [
            {"text": doc.page_content[:800], "metadata": doc.metadata} for doc in docs
        ]
        for item, docs in zip(items, documents, strict=True)
    }


def _reviewer(
    state: ReviewBatchState, role: Literal["fidelity", "terminology"]
) -> dict[str, dict[str, dict[str, Any]]]:
    """指定観点のReviewerをLangChainで実行する。

    Args:
        state: Review対象と設定を含むsubgraph state。
        role: fidelityまたはterminology。

    Returns:
        role名をkeyにしたReview案。
    """

    options = ReviewOptions.model_validate(state["options"])
    chain = prompt_runnable(
        _v4_options(options, options.source, options.output_dir),
        ReviewResponse,
        (
            f"{role} Reviewerとして、外部Reviewルールに従いreviews配列を持つ"
            "objectで回答してください。各入力IDを正確に1回含めてください。"
        ),
        "Reviewルール:\n{rules}\n\n入力JSON:\n{items}",
        max_tokens=options.max_output_tokens,
        trace_name=f"review-enja-{role}",
    )
    response = chain.invoke(
        {
            "rules": state["rules"],
            "items": json.dumps(state["items"], ensure_ascii=False),
        }
    )
    values = {item.id: item.model_dump(mode="json") for item in response.reviews}
    expected = {str(item["id"]) for item in state["items"]}
    if set(values) != expected:
        raise ValueError(f"{role} Review IDs must exactly match the input")
    return {role: values}


def _fidelity(state: ReviewBatchState) -> dict[str, Any]:
    """忠実性Reviewer nodeを実行する。

    Args:
        state: Review batch state。

    Returns:
        fidelity Review案。
    """

    return _reviewer(state, "fidelity")


def _terminology(state: ReviewBatchState) -> dict[str, Any]:
    """用語Reviewer nodeを実行する。

    Args:
        state: Review batch state。

    Returns:
        terminology Review案。
    """

    return _reviewer(state, "terminology")


def _decision_key(value: dict[str, Any]) -> tuple[Any, ...]:
    """Reviewer案から裁定要否だけに関係する比較keyを作る。

    Args:
        value: ReviewFindingのJSON互換dict。

    Returns:
        status、severity、category、修正案の比較tuple。
    """

    return (
        value["status"],
        value["severity"],
        tuple(sorted(value["categories"])),
        value["suggested_translation"].strip(),
    )


def _adjudicate(state: ReviewBatchState) -> dict[str, Any]:
    """二Reviewerの判断が異なる要素だけをLLMで裁定する。

    Args:
        state: 両Reviewer案を含むbatch state。

    Returns:
        全IDの最終Review判定。
    """

    final: dict[str, dict[str, Any]] = {}
    disputes: list[dict[str, Any]] = []
    inputs = {str(item["id"]): item for item in state["items"]}
    for item_id, fidelity in state["fidelity"].items():
        terminology = state["terminology"][item_id]
        if _decision_key(fidelity) == _decision_key(terminology):
            final[item_id] = fidelity
        else:
            disputes.append(
                {
                    **inputs[item_id],
                    "fidelity": fidelity,
                    "terminology": terminology,
                }
            )
    if disputes:
        options = ReviewOptions.model_validate(state["options"])
        chain = prompt_runnable(
            _v4_options(options, options.source, options.output_dir),
            ReviewResponse,
            (
                "英日翻訳ReviewのAdjudicatorとして、外部Reviewルールに従い"
                "reviews配列を持つobjectで回答してください。各競合IDを正確に1回"
                "含め、根拠が弱い変更は採用しないでください。"
            ),
            "Reviewルール:\n{rules}\n\n競合JSON:\n{items}",
            max_tokens=options.max_output_tokens,
            trace_name="review-enja-adjudicator",
        )
        response = chain.invoke(
            {
                "rules": state["rules"],
                "items": json.dumps(disputes, ensure_ascii=False),
            }
        )
        decided = {item.id: item.model_dump(mode="json") for item in response.reviews}
        if set(decided) != {str(item["id"]) for item in disputes}:
            raise ValueError("Adjudicator IDs must exactly match disputes")
        final.update(decided)
    return {"final": final}


def _review_graph() -> Any:
    """FidelityとTerminologyを並列実行するReview subgraphを作る。

    Returns:
        compile済みLangGraph。
    """

    builder = StateGraph(cast(Any, ReviewBatchState))
    builder.add_node("fidelity", _fidelity)
    builder.add_node("terminology", _terminology)
    builder.add_node("adjudicate", _adjudicate)
    builder.add_edge(START, "fidelity")
    builder.add_edge(START, "terminology")
    builder.add_edge("fidelity", "adjudicate")
    builder.add_edge("terminology", "adjudicate")
    builder.add_edge("adjudicate", END)
    return builder.compile()


def _review_batches(
    items: list[dict[str, Any]],
    max_chars: int,
    max_elements: int,
    max_output_tokens: int,
) -> list[list[dict[str, Any]]]:
    """Review対象を入力文字数、件数、推定出力長で分割する。

    Args:
        items: 文書順のAlignment要素。
        max_chars: 原文と訳文の合計文字数上限。
        max_elements: 要素数上限。0は無制限。
        max_output_tokens: structured outputの推定token上限。

    Returns:
        文書順を維持したbatch配列。
    """

    result: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    characters = 0
    for item in items:
        length = len(str(item["source_text"])) + len(str(item["translated_text"]))
        element_limit = max_elements > 0 and len(current) >= max_elements
        output_limit = (len(current) + 1) * 320 > max_output_tokens
        if current and (
            characters + length > max_chars or element_limit or output_limit
        ):
            result.append(current)
            current, characters = [], 0
        current.append(item)
        characters += length
    if current:
        result.append(current)
    return result


def _automatic_review(item: dict[str, Any]) -> dict[str, Any]:
    """片言語だけのAlignmentをLLMなしで欠落・追加判定する。

    Args:
        item: source_onlyまたはtranslation_onlyのAlignment。

    Returns:
        両Reviewer案と最終判定。
    """

    source_only = item["status"] == "source_only"
    category = "omission" if source_only else "addition"
    reason = (
        "No aligned Japanese translation was found."
        if source_only
        else "No aligned English source was found."
    )
    finding = ReviewFinding(
        id=str(item["id"]),
        status=ReviewStatus.NEEDS_CHANGE,
        severity=ReviewSeverity.MAJOR,
        categories=[category],
        suggested_translation="",
        reason=reason,
    ).model_dump(mode="json")
    return {"fidelity": finding, "terminology": finding, "final": finding}


def _run_review_with_fallback(
    graph: Any,
    options: ReviewOptions,
    rules: str,
    glossary: list[dict[str, str]],
    retriever: Any,
    batch: list[dict[str, Any]],
) -> Iterator[tuple[list[dict[str, Any]], dict[str, Any]]]:
    """Review失敗時にbatchを半減して成功結果を順次返す。

    Args:
        graph: compile済みReview subgraph。
        options: LLMとcontext設定。
        rules: 外部Reviewルール。
        glossary: 全用語集。
        retriever: 任意のRAG retriever。
        batch: aligned Review対象batch。

    Yields:
        成功したsub-batchとReviewer・最終判定。

    Raises:
        Exception: 1要素でもReview subgraphが失敗した場合。
    """

    try:
        evidence = _evidence(retriever, batch) if retriever else {}
        items = [
            {
                "id": item["id"],
                "source_text": item["source_text"],
                "translated_text": item["translated_text"],
                "source_pages": item["source_pages"],
                "translation_pages": item["translation_pages"],
                "glossary": glossary_matches(str(item["source_text"]), glossary),
                "rag_evidence": evidence.get(str(item["id"]), []),
            }
            for item in batch
        ]
        encoded = json.dumps(items, ensure_ascii=False)
        if len(rules) + len(encoded) + 2_000 > options.context_chars:
            raise ValueError("ReviewStage prompt exceeds context_chars")
        result = graph.invoke(
            {"options": options.model_dump(mode="json"), "items": items, "rules": rules}
        )
        yield batch, result
    except Exception:
        if len(batch) == 1:
            raise
        middle = len(batch) // 2
        LOGGER.warning(
            "ReviewStage batch failed; retrying with smaller batches elements=%s->%s",
            len(batch),
            middle,
        )
        yield from _run_review_with_fallback(
            graph, options, rules, glossary, retriever, batch[:middle]
        )
        yield from _run_review_with_fallback(
            graph, options, rules, glossary, retriever, batch[middle:]
        )


def _review_document(
    source: str,
    translation: str,
    alignments: list[dict[str, Any]],
    reviews: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """部分または完成Review成果物を文書順に組み立てる。

    Args:
        source: 英語PDFの絶対path。
        translation: 日本語PDFの絶対path。
        alignments: 全Alignment要素。
        reviews: Alignment ID別の完了Review。

    Returns:
        summaryとReview配列を持つJSON object。
    """

    ordered = [reviews[item["id"]] for item in alignments if item["id"] in reviews]
    changed = sum(item["final"]["status"] == "needs_change" for item in ordered)
    severity = {
        name: sum(item["final"]["severity"] == name for item in ordered)
        for name in ("critical", "major", "minor", "info")
    }
    return {
        "schema_version": 1,
        "source": source,
        "translation": translation,
        "summary": {
            "total": len(alignments),
            "completed": len(ordered),
            "pass": len(ordered) - changed,
            "needs_change": changed,
            "severity": severity,
        },
        "reviews": ordered,
    }


def review_stage(state: ReviewState) -> ReviewState:
    """multi-agent subgraphで英日AlignmentをレビューするLangGraph nodeを実行する。

    Args:
        state: Alignment成果物を含むgraph state。

    Returns:
        Review成果物パスを設定した部分state。
    """

    options, paths = _options(state), _paths(state)
    aligned_path = Path(state["aligned_json"])
    input_hash = hash_file(aligned_path)
    rules = read_rules(options.review_rules, DEFAULT_RULES)
    glossary = read_glossary(options.glossary)
    prompt_glossary = [
        {key: value for key, value in row.items() if key not in {"note", "reference"}}
        for row in glossary
    ]
    config_hash = hash_json(
        {
            "version": 1,
            "rules": rules,
            "glossary": prompt_glossary,
            "rag": options.review_rag,
            "context_chars": options.context_chars,
            "batch_chars": options.batch_chars,
            "max_batch_elements": options.max_batch_elements,
            "max_output_tokens": options.max_output_tokens,
            "model": os.getenv("OPENAI_MODEL"),
            "collection": os.getenv("QDRANT_COLLECTION")
            if options.review_rag
            else None,
        }
    )
    if _stage_cached(
        paths.manifest, "review", input_hash, config_hash, paths.reviewed_json
    ):
        LOGGER.info("Resumed ReviewStage output=%s", paths.reviewed_json)
        return {
            "reviewed_json": str(paths.reviewed_json),
            "completed_stage": "review",
        }
    aligned_document = read_json(aligned_path)
    alignments = cast(list[dict[str, Any]], aligned_document["alignments"])
    reviews: dict[str, dict[str, Any]] = {}
    if _stage_partial(
        paths.manifest, "review", input_hash, config_hash, paths.reviewed_json
    ):
        reviews = {
            str(item["id"]): item
            for item in read_json(paths.reviewed_json).get("reviews", [])
            if isinstance(item, dict) and item.get("id")
        }
    pending = [item for item in alignments if item["id"] not in reviews]
    write_json(
        paths.reviewed_json,
        _review_document(
            str(options.source.resolve()),
            str(options.translation.resolve()),
            alignments,
            reviews,
        ),
    )
    _record_stage(
        paths.manifest,
        "review",
        "running",
        input_hash,
        config_hash,
        paths.reviewed_json,
        {"total": len(alignments), "completed": len(reviews)},
    )
    graph = _review_graph()
    retriever = _retriever() if options.review_rag else None
    prompt_budget = max(1, options.context_chars - len(rules) - 2_000)
    for batch in _review_batches(
        pending,
        min(options.batch_chars, prompt_budget),
        options.max_batch_elements,
        options.max_output_tokens,
    ):
        aligned_batch = [item for item in batch if item["status"] == "aligned"]
        for item in batch:
            if item["status"] != "aligned":
                reviews[str(item["id"])] = {**item, **_automatic_review(item)}
        if aligned_batch:
            for completed_batch, result in _run_review_with_fallback(
                graph, options, rules, glossary, retriever, aligned_batch
            ):
                for item in completed_batch:
                    item_id = str(item["id"])
                    reviews[item_id] = {
                        **item,
                        "fidelity": result["fidelity"][item_id],
                        "terminology": result["terminology"][item_id],
                        "final": result["final"][item_id],
                    }
        write_json(
            paths.reviewed_json,
            _review_document(
                str(options.source.resolve()),
                str(options.translation.resolve()),
                alignments,
                reviews,
            ),
        )
        _record_stage(
            paths.manifest,
            "review",
            "running",
            input_hash,
            config_hash,
            paths.reviewed_json,
            {"total": len(alignments), "completed": len(reviews)},
        )
    _record_stage(
        paths.manifest,
        "review",
        "completed",
        input_hash,
        config_hash,
        paths.reviewed_json,
        {"total": len(alignments), "completed": len(alignments)},
    )
    return {
        "reviewed_json": str(paths.reviewed_json),
        "completed_stage": "review",
    }


def _markdown_text(value: Any) -> str:
    """任意の値をMarkdown本文で安全なplain textへ変換する。

    Args:
        value: 出力対象値。

    Returns:
        改行と空白を正規化した文字列。
    """

    normalized = " ".join(str(value or "").replace("\x00", "").split())
    return re.sub(r"([\\`*_[\]<>#])", r"\\\1", normalized)


def _render_report(document: dict[str, Any]) -> str:
    """Review JSONから人が読むMarkdown指摘票を作る。

    Args:
        document: 完成したReview成果物。

    Returns:
        UTF-8 Markdown文字列。
    """

    summary = document["summary"]
    severity = summary["severity"]
    lines = [
        "# 英日翻訳レビュー",
        "",
        f"- 英語原文: `{document['source']}`",
        f"- 日本語訳: `{document['translation']}`",
        f"- 対応数: {summary['total']}",
        f"- Pass: {summary['pass']}",
        f"- 要修正: {summary['needs_change']}",
        (
            "- 重大度: "
            f"Critical {severity['critical']} / Major {severity['major']} / "
            f"Minor {severity['minor']} / Info {severity['info']}"
        ),
        "",
        "## 指摘",
        "",
    ]
    issues = [
        item
        for item in document["reviews"]
        if item["final"]["status"] == "needs_change"
    ]
    if not issues:
        lines.extend(["指摘はありません。", ""])
    for item in issues:
        final = item["final"]
        lines.extend(
            [
                f"### {item['id']} — {final['severity']}",
                "",
                f"- Category: {', '.join(final['categories']) or 'unspecified'}",
                f"- Source pages: {', '.join(map(str, item['source_pages'])) or '-'}",
                (
                    "- Translation pages: "
                    f"{', '.join(map(str, item['translation_pages'])) or '-'}"
                ),
                "",
                "**Source**",
                "",
                _markdown_text(item["source_text"]),
                "",
                "**Current translation**",
                "",
                _markdown_text(item["translated_text"]),
                "",
                "**Suggested translation**",
                "",
                _markdown_text(final["suggested_translation"]) or "-",
                "",
                "**Reason**",
                "",
                _markdown_text(final["reason"]),
                "",
            ]
        )
    return "\n".join(lines)


def report_stage(state: ReviewState) -> ReviewState:
    """Review JSONからMarkdown指摘票を生成するLangGraph nodeを実行する。

    Args:
        state: Review成果物を含むgraph state。

    Returns:
        Markdown成果物パスを設定した部分state。
    """

    paths = _paths(state)
    reviewed_path = Path(state["reviewed_json"])
    input_hash = hash_file(reviewed_path)
    config_hash = hash_json({"version": 1})
    if _stage_cached(paths.manifest, "report", input_hash, config_hash, paths.report):
        LOGGER.info("Resumed ReportStage output=%s", paths.report)
        return {"report": str(paths.report), "completed_stage": "report"}
    write_bytes(paths.report, _render_report(read_json(reviewed_path)).encode("utf-8"))
    _record_stage(
        paths.manifest,
        "report",
        "completed",
        input_hash,
        config_hash,
        paths.report,
    )
    return {"report": str(paths.report), "completed_stage": "report"}


STAGES = (
    ("parse", parse_stage),
    ("alignment", alignment_stage),
    ("review", review_stage),
    ("report", report_stage),
)


def build_graph(options: ReviewOptions, checkpointer: Any | None = None) -> Any:
    """全Stageを直列接続したLangGraphを作る。

    Args:
        options: Stage retry設定。
        checkpointer: 任意のLangGraph checkpointer。

    Returns:
        compile済みLangGraph。
    """

    builder = StateGraph(cast(Any, ReviewState))
    retry = RetryPolicy(
        max_attempts=options.stage_max_attempts,
        initial_interval=options.stage_retry_initial_seconds,
        max_interval=options.stage_retry_max_seconds,
    )
    for name, action in STAGES:
        builder.add_node(name, action, retry_policy=retry)
    builder.add_edge(START, STAGES[0][0])
    for current, following in zip(STAGES, STAGES[1:]):
        builder.add_edge(current[0], following[0])
    builder.add_edge(STAGES[-1][0], END)
    return builder.compile(checkpointer=checkpointer)


def _thread_id(options: ReviewOptions) -> str:
    """英日入力と出力先に固有のLangGraph thread IDを返す。

    Args:
        options: 入出力pathを含むレビュー設定。

    Returns:
        安定した短いSHA-256文字列。
    """

    payload = json.dumps(
        [
            str(options.source.resolve()),
            str(options.translation.resolve()),
            str(options.output_dir.resolve()),
        ],
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def run(options: ReviewOptions) -> ReviewPaths:
    """永続checkpoint付き英日翻訳Review Pipelineを実行する。

    Args:
        options: 検証済みCLI設定。

    Returns:
        生成物の固定パス一覧。

    Side Effects:
        dotenvと外部サービスを利用し、成果物を保存する。
    """

    load_environment(options.env)
    configure_logging()
    paths = build_paths(options)
    paths.output_dir.mkdir(parents=True, exist_ok=True)
    configuration = {
        "configurable": {"thread_id": _thread_id(options)},
        "recursion_limit": len(STAGES) + 4,
    }
    try:
        with SqliteSaver.from_conn_string(str(paths.checkpoints)) as checkpointer:
            graph = build_graph(options, checkpointer)
            graph.invoke(
                {
                    "options": options.model_dump(mode="json"),
                    "paths": paths.model_dump(mode="json"),
                },
                configuration,
            )
    finally:
        flush_langfuse()
    LOGGER.info("Pipeline completed output=%s", paths.output_dir)
    return paths
