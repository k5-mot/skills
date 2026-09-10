#!/usr/bin/env python3
"""英日PDFを対応付け、v4 ReviewStageで指摘事項だけを生成する。"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any, cast

import typer
from pydantic import BaseModel, ConfigDict, Field

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
SKILLS_DIR = Path(__file__).resolve().parents[3]
REVIEW_ENJA_SCRIPTS = SKILLS_DIR / "review-enja" / "scripts"
for import_path in (SCRIPTS_DIR, REVIEW_ENJA_SCRIPTS):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from review_enja.pipeline import (  # noqa: E402
    ReviewOptions as AlignmentOptions,
)
from review_enja.pipeline import (  # noqa: E402
    alignment_stage,
    build_paths as build_alignment_paths,
    parse_stage,
)
from translate_ja_v4.config import (  # noqa: E402
    PipelineOptions,
    build_paths,
    initial_state,
)
from translate_ja_v4.io import (  # noqa: E402
    LOGGER,
    configure_logging,
    load_environment,
    read_json,
    write_bytes,
    write_json,
)
from translate_ja_v4.llm import flush_langfuse  # noqa: E402
from translate_ja_v4.stages.review import review_stage as v4_review_stage  # noqa: E402

app = typer.Typer(
    add_completion=False,
    help="Review a Japanese PDF against its English source without editing either PDF",
)


class ReviewDocxOptions(BaseModel):
    """英日PDFレビューCLIの設定を保持する。"""

    model_config = ConfigDict(frozen=True)

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
    pdf_chunk_pages: int = Field(default=10, ge=1)
    review_rag: bool = False
    force: bool = False


class ReviewDocxPaths(BaseModel):
    """英日PDFレビューの主要成果物pathを保持する。"""

    model_config = ConfigDict(frozen=True)

    aligned_json: Path
    translated_json: Path
    reviewed_json: Path
    findings_json: Path
    report: Path


def _alignment_options(options: ReviewDocxOptions) -> AlignmentOptions:
    """CLI設定をreview-enjaのParse・Alignment設定へ変換する。

    Args:
        options: 英日PDFレビュー設定。

    Returns:
        ParseStageとAlignmentStage用の設定。
    """
    return AlignmentOptions(
        source=options.source,
        translation=options.translation,
        output_dir=options.output_dir,
        env=options.env,
        glossary=options.glossary,
        review_rules=options.review_rules,
        context_chars=options.context_chars,
        batch_chars=options.batch_chars,
        max_batch_elements=options.max_batch_elements,
        max_output_tokens=options.max_output_tokens,
        request_timeout_seconds=options.request_timeout_seconds,
        max_retries=options.max_retries,
        retry_initial_seconds=options.retry_initial_seconds,
        retry_max_seconds=options.retry_max_seconds,
        pdf_chunk_pages=options.pdf_chunk_pages,
        review_rag=options.review_rag,
        force=options.force,
    )


def _align_pdfs(options: ReviewDocxOptions) -> list[dict[str, Any]]:
    """既存のParse・Normalize・Alignmentで英日PDFを対応付ける。

    Args:
        options: 入出力、Docling、LLM設定。

    Returns:
        原文、訳文、ページを持つ順序付きAlignment配列。

    Side Effects:
        DoclingとLLMを呼び、中間JSONとmanifestを出力する。
    """
    alignment_options = _alignment_options(options)
    paths = build_alignment_paths(alignment_options)
    paths.output_dir.mkdir(parents=True, exist_ok=True)
    state: dict[str, Any] = {
        "options": alignment_options.model_dump(mode="json"),
        "paths": paths.model_dump(mode="json"),
    }
    state.update(parse_stage(cast(Any, state)))
    state.update(alignment_stage(cast(Any, state)))
    document = read_json(paths.aligned_json)
    alignments = document.get("alignments")
    if not isinstance(alignments, list) or not all(
        isinstance(item, dict) for item in alignments
    ):
        raise ValueError("Alignment output has no valid alignments array")
    return cast(list[dict[str, Any]], alignments)


def _translated_document(alignments: list[dict[str, Any]]) -> dict[str, Any]:
    """Alignmentをv4 ReviewStage入力のDocling風documentへ変換する。

    Args:
        alignments: review-enjaが生成した英日対応配列。

    Returns:
        `translate_ja_v4` metadataを持つReviewStage入力。
    """
    texts: list[dict[str, Any]] = []
    for alignment in alignments:
        source_text = str(alignment.get("source_text", "")).strip()
        if not source_text:
            continue
        translated_text = str(alignment.get("translated_text", "")).strip()
        texts.append(
            {
                "self_ref": f"#/texts/{len(texts)}",
                "label": "text",
                "text": source_text,
                "review_docx": alignment,
                "translate_ja_v4": {
                    "text_ja": translated_text,
                    "render_text": translated_text,
                },
            }
        )
    return {
        "schema_name": "DoclingDocument",
        "version": "1.0.0",
        "name": "review-docx",
        "texts": texts,
        "tables": [],
        "pictures": [],
        "pages": {},
    }


def _review_options(options: ReviewDocxOptions) -> PipelineOptions:
    """CLI設定をv4 ReviewStage用PipelineOptionsへ変換する。

    Args:
        options: 英日PDFレビュー設定。

    Returns:
        ReviewStageが利用するv4設定。
    """
    return PipelineOptions(
        input=options.source,
        output_dir=options.output_dir,
        env=options.env,
        glossary=options.glossary,
        review_rules=options.review_rules,
        context_chars=options.context_chars,
        batch_chars=options.batch_chars,
        max_batch_elements=options.max_batch_elements,
        max_output_tokens=options.max_output_tokens,
        request_timeout_seconds=options.request_timeout_seconds,
        max_retries=options.max_retries,
        retry_initial_seconds=options.retry_initial_seconds,
        retry_max_seconds=options.retry_max_seconds,
        pdf_chunk_pages=options.pdf_chunk_pages,
        review_rag=options.review_rag,
    )


def _review_alignments(
    options: ReviewDocxOptions,
    alignments: list[dict[str, Any]],
) -> tuple[dict[str, Any], ReviewDocxPaths]:
    """Alignmentを保存して既存v4 ReviewStageを直接実行する。

    Args:
        options: ReviewStage設定。
        alignments: 英日対応配列。

    Returns:
        Review済みdocumentと主要成果物path。

    Side Effects:
        `document.translated.json`、Review成果物、manifestを保存しLLMを呼ぶ。
    """
    review_options = _review_options(options)
    paths = build_paths(review_options)
    paths.output_dir.mkdir(parents=True, exist_ok=True)
    write_json(paths.translated_json, _translated_document(alignments))
    state = initial_state(review_options, paths)
    state["current_path"] = str(paths.translated_json)
    v4_review_stage(state)
    return (
        read_json(paths.reviewed_json),
        ReviewDocxPaths(
            aligned_json=options.output_dir.resolve() / "document.aligned.json",
            translated_json=paths.translated_json,
            reviewed_json=paths.reviewed_json,
            findings_json=paths.output_dir / "review.findings.json",
            report=paths.output_dir / "review.md",
        ),
    )


def _finding(
    alignment: dict[str, Any],
    suggested: str,
    reason: str,
    category: str,
) -> dict[str, Any]:
    """一つのAlignmentから指摘事項objectを作る。

    Args:
        alignment: 原文、訳文、ページを持つ対応情報。
        suggested: ReviewStageが選んだ修正案。
        reason: ReviewStageまたは自動判定の理由。
        category: revision、omission、additionのいずれか。

    Returns:
        JSONへ保存する指摘事項。
    """
    return {
        "id": alignment.get("id"),
        "category": category,
        "source_pages": alignment.get("source_pages", []),
        "translation_pages": alignment.get("translation_pages", []),
        "source_text": alignment.get("source_text", ""),
        "current_translation": alignment.get("translated_text", ""),
        "suggested_translation": suggested,
        "reason": reason,
    }


def _findings_document(
    reviewed: dict[str, Any],
    alignments: list[dict[str, Any]],
    options: ReviewDocxOptions,
) -> dict[str, Any]:
    """ReviewStage出力から修正が必要な項目だけを抽出する。

    Args:
        reviewed: v4 ReviewStageが生成したdocument。
        alignments: 全英日対応配列。
        options: 入力PDF pathを持つ設定。

    Returns:
        summaryと指摘事項配列を持つ監査用JSON。
    """
    findings: list[dict[str, Any]] = []
    handled_ids: set[str] = set()
    for item in reviewed.get("texts", []):
        if not isinstance(item, dict) or not isinstance(item.get("review_docx"), dict):
            continue
        alignment = item["review_docx"]
        alignment_id = str(alignment.get("id", ""))
        handled_ids.add(alignment_id)
        metadata = item.get("translate_ja_v4", {})
        if not isinstance(metadata, dict):
            continue
        current = str(alignment.get("translated_text", "")).strip()
        suggested = str(metadata.get("text_ja", current)).strip()
        status = str(alignment.get("status", "aligned"))
        if status == "aligned" and suggested == current:
            continue
        review = metadata.get("review_ja_v4", {})
        reason = str(review.get("reason", "")) if isinstance(review, dict) else ""
        findings.append(
            _finding(
                alignment,
                suggested,
                reason or "The source has no usable Japanese translation.",
                "omission" if status == "source_only" else "revision",
            )
        )
    for alignment in alignments:
        alignment_id = str(alignment.get("id", ""))
        if alignment_id in handled_ids or alignment.get("status") != "translation_only":
            continue
        findings.append(
            _finding(
                alignment,
                "",
                "The Japanese PDF contains text with no aligned English source.",
                "addition",
            )
        )
    return {
        "schema_version": 1,
        "source": str(options.source.resolve()),
        "translation": str(options.translation.resolve()),
        "summary": {"alignments": len(alignments), "findings": len(findings)},
        "findings": findings,
    }


def _markdown_text(value: Any) -> str:
    """任意の値をMarkdown本文で安全なplain textへ変換する。

    Args:
        value: 出力対象値。

    Returns:
        改行と空白、Markdown記号を正規化した文字列。
    """
    normalized = " ".join(str(value or "").replace("\x00", "").split())
    return re.sub(r"([\\`*_[\]<>#])", r"\\\1", normalized)


def _render_report(document: dict[str, Any]) -> str:
    """指摘事項JSONから人が読むMarkdownを生成する。

    Args:
        document: summaryとfindingsを持つJSON object。

    Returns:
        UTF-8 Markdown文字列。
    """
    summary = document["summary"]
    lines = [
        "# 英日翻訳レビュー",
        "",
        f"- 原文PDF: `{document['source']}`",
        f"- 翻訳PDF: `{document['translation']}`",
        f"- 対応数: {summary['alignments']}",
        f"- 指摘数: {summary['findings']}",
        "",
        "## 指摘事項",
        "",
    ]
    if not document["findings"]:
        lines.extend(["指摘はありません。", ""])
    for finding in document["findings"]:
        lines.extend(
            [
                f"### {_markdown_text(finding['id'])} — {finding['category']}",
                "",
                f"- 原文ページ: {', '.join(map(str, finding['source_pages'])) or '-'}",
                f"- 翻訳ページ: {', '.join(map(str, finding['translation_pages'])) or '-'}",
                "",
                "**原文**",
                "",
                _markdown_text(finding["source_text"]) or "-",
                "",
                "**現在の翻訳**",
                "",
                _markdown_text(finding["current_translation"]) or "-",
                "",
                "**修正案**",
                "",
                _markdown_text(finding["suggested_translation"]) or "-",
                "",
                "**理由**",
                "",
                _markdown_text(finding["reason"]) or "-",
                "",
            ]
        )
    return "\n".join(lines)


def run(options: ReviewDocxOptions) -> ReviewDocxPaths:
    """英日PDFを解析・対応付けし、v4 ReviewStageの指摘票を作る。

    Args:
        options: 入出力と外部サービスの設定。

    Returns:
        Alignment、Review、指摘事項、Markdownのpath。

    Side Effects:
        DoclingとLLMを呼び、出力directoryへJSONとMarkdownを保存する。
    """
    load_environment(options.env)
    configure_logging()
    try:
        alignments = _align_pdfs(options)
        reviewed, paths = _review_alignments(options, alignments)
        findings = _findings_document(reviewed, alignments, options)
        write_json(paths.findings_json, findings)
        write_bytes(paths.report, _render_report(findings).encode("utf-8"))
    finally:
        flush_langfuse()
    LOGGER.info(
        "PDF review completed findings=%s report=%s",
        len(findings["findings"]),
        paths.report,
    )
    return paths


@app.command()
def cli(
    source: Annotated[Path, typer.Option(help="English source PDF path")],
    translation: Annotated[Path, typer.Option(help="Japanese translation PDF path")],
    output_dir: Annotated[Path, typer.Option(help="review output directory")],
    env: Annotated[Path, typer.Option(help="dotenv path")] = Path(".env"),
    glossary: Annotated[Path | None, typer.Option(help="review glossary CSV")] = None,
    review_rules: Annotated[
        Path | None, typer.Option(help="external ReviewStage rules file")
    ] = None,
    context_chars: Annotated[int, typer.Option(min=1)] = 50_000,
    batch_chars: Annotated[int, typer.Option(min=1)] = 20_000,
    max_batch_elements: Annotated[int, typer.Option(min=0)] = 0,
    max_output_tokens: Annotated[int, typer.Option(min=256)] = 16_384,
    request_timeout_seconds: Annotated[int, typer.Option(min=1)] = 1_800,
    max_retries: Annotated[int, typer.Option(min=0)] = 5,
    retry_initial_seconds: Annotated[float, typer.Option(min=0)] = 1,
    retry_max_seconds: Annotated[float, typer.Option(min=0)] = 30,
    pdf_chunk_pages: Annotated[int, typer.Option(min=1)] = 10,
    review_rag: Annotated[bool, typer.Option()] = False,
    force: Annotated[bool, typer.Option()] = False,
) -> None:
    """二つのPDFを比較し、元ファイルを編集せず指摘事項を列挙する。

    Args:
        source: 英語原文PDF。
        translation: 日本語翻訳PDF。
        output_dir: JSONとMarkdownの出力directory。
        env: dotenvファイル。
        glossary: Review用CSV用語集。
        review_rules: ReviewStage外部ルール。
        context_chars: prompt全体の最大文字数。
        batch_chars: Alignment・Review batchの最大文字数。
        max_batch_elements: batch要素数上限。0は無制限。
        max_output_tokens: LLM応答token上限。
        request_timeout_seconds: 外部APIのtimeout秒数。
        max_retries: 初回失敗後のAPI再試行回数。
        retry_initial_seconds: API再試行の初期待機秒数。
        retry_max_seconds: API再試行の最大待機秒数。
        pdf_chunk_pages: Doclingへ一度に送るPDFページ数。
        review_rag: Qdrant RAGをReviewStageで使うか。
        force: PDF Parse cacheを無視するか。

    Returns:
        なし。

    Side Effects:
        外部サービスを呼び、出力directoryへレビュー成果物を保存する。
    """
    try:
        paths = run(
            ReviewDocxOptions(
                source=source,
                translation=translation,
                output_dir=output_dir,
                env=env,
                glossary=glossary,
                review_rules=review_rules,
                context_chars=context_chars,
                batch_chars=batch_chars,
                max_batch_elements=max_batch_elements,
                max_output_tokens=max_output_tokens,
                request_timeout_seconds=request_timeout_seconds,
                max_retries=max_retries,
                retry_initial_seconds=retry_initial_seconds,
                retry_max_seconds=retry_max_seconds,
                pdf_chunk_pages=pdf_chunk_pages,
                review_rag=review_rag,
                force=force,
            )
        )
    except KeyboardInterrupt:
        LOGGER.error("PDF review was interrupted")
        raise typer.Exit(code=130) from None
    except Exception as error:
        LOGGER.exception("PDF review failed: %s", error)
        raise typer.Exit(code=1) from None
    typer.echo(f"JSON: {paths.findings_json}")
    typer.echo(f"Markdown: {paths.report}")


def main(argv: list[str] | None = None) -> int:
    """CLI entrypointを実行して終了codeを返す。

    Args:
        argv: 引数列。Noneならsys.argvを使う。

    Returns:
        process終了code。

    Side Effects:
        CLI指定に従い英日PDFレビューを実行する。
    """
    command = typer.main.get_command(app)
    try:
        result = command.main(args=argv, standalone_mode=False)
    except typer.Exit as error:
        return int(error.exit_code or 0)
    return int(result or 0)


if __name__ == "__main__":
    started_at = perf_counter()
    exit_code = main()
    LOGGER.debug("Elapsed time %.3f seconds", perf_counter() - started_at)
    sys.exit(exit_code)
