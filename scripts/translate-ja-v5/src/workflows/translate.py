"""PDFから検証済み日本語DOCXを生成するworkflowを実装する。"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from src.adapters.docling import convert_pdf
from src.adapters.langfuse import flush_safely, observed, update_current
from src.adapters.pandoc import (
    check_pandoc,
    create_docx,
    pdf_pages_text,
    render_cover,
    render_page,
)
from src.config import Backend, Settings, read_rules
from src.model import Document, Page, page_text
from src.processing.normalize import normalize_docling
from src.processing.quality import GlossaryEntry, matching_glossary, read_glossary
from src.processing.render import render_document, validate_document
from src.processing.structure import structure_page
from src.processing.translation import apply_layer, translate_page, translation_units
from src.state import (
    OutputLock,
    atomic_write_json,
    atomic_write_text,
    invalidate_state,
    load_json,
    new_state,
    progress_lines,
    sha256_file,
)


def _config_snapshot(settings: Settings, backend: Backend) -> dict[str, str | None]:
    """Resume判定に必要な設定だけを抽出する。

    Args:
        settings: 実行設定。
        backend: 翻訳backend。

    Returns:
        認証情報を含まない設定mapping。
    """

    return {
        "structure_model": settings.structure_model,
        "translation_model": settings.translation_model
        if backend == "openai"
        else None,
        "review_model": settings.review_model,
        "embedding_model": settings.embedding_model,
        "backend": backend,
    }


def _page_path(directory: Path, page_number: int) -> Path:
    """ページ別JSONの固定pathを作る。

    Args:
        directory: Stage directory。
        page_number: 1始まりページ番号。

    Returns:
        `page_000001.json`形式のpath。
    """

    return directory / f"page_{page_number:06d}.json"


def _clear_known_work(work: Path) -> None:
    """force時にv5が所有する内部成果物だけを削除する。

    Args:
        work: `.work` directory。

    Returns:
        なし。

    Side Effects:
        run.lock以外の既知artifactを削除する。
    """

    for name in ("parsed.json", "normalized.json", "state.json", "document.ja.md"):
        (work / name).unlink(missing_ok=True)
    for name in ("structured", "translated", "reviewed"):
        target = work / name
        if target.exists():
            shutil.rmtree(target)


@observed("review-page", capture_input=False)
def _review_page(
    page: Page, rules: str, settings: Settings, glossary: list[GlossaryEntry]
) -> Page:
    """ページ内の各翻訳Inlineを共有Review graphで検証する。

    Args:
        page: translated層を持つページ。
        rules: Review Rules。
        settings: ReviewとQdrant設定。
        glossary: 用語集。

    Returns:
        reviewed層を持つページ。
    """

    from src.workflows.review import run_review

    # 原文側の走査順を流用せず、実在するtranslated層だけをReview対象にする。
    translated_page = page.model_copy(deep=True)
    pairs: list[tuple[str, str, str]] = []
    source_by_id = dict(translation_units(page))
    for block in translated_page.blocks:
        groups = [block.translated or [], block.translated_caption or []]
        groups.extend(cell.translated or [] for cell in block.cells)
        for values in groups:
            for item in values:
                if item.id in source_by_id:
                    pairs.append((item.id, source_by_id[item.id], item.text))
    update_current(
        input={
            "page": page.model_dump(),
            "rules": rules,
            "glossary": [
                item.model_dump()
                for item in matching_glossary(
                    "\n".join(source for _item_id, source, _target in pairs),
                    glossary,
                )
            ],
        }
    )
    reviewed: dict[str, str] = {}
    for item_id, source, target in pairs:
        try:
            reviewed[item_id] = run_review(
                source,
                target,
                rules,
                settings,
                matching_glossary(source, glossary),
            ).text
        except RuntimeError as error:
            raise RuntimeError(f"Review failed for {item_id}: {error}") from error
    result = apply_layer(page, reviewed, "reviewed")
    update_current(output=result.model_dump())
    return result


def _load_page(path: Path) -> Page | None:
    """ページ別JSONを再利用できる場合だけPageとして読む。

    Args:
        path: 読み込むJSON。

    Returns:
        検証済みPage。欠損または破損時はNone。
    """

    try:
        value = load_json(path)
        return Page.model_validate(value) if value is not None else None
    except (OSError, ValueError):
        return None


def _load_document(path: Path) -> Document | None:
    """正規化済みJSONを再利用できる場合だけDocumentとして読む。

    Args:
        path: 読み込むJSON。

    Returns:
        検証済みDocument。欠損または破損時はNone。
    """

    try:
        value = load_json(path)
        return Document.model_validate(value) if value is not None else None
    except (OSError, ValueError):
        return None


def _load_parsed(path: Path) -> dict[str, Any] | None:
    """Docling JSONを再利用できる場合だけobjectとして読む。

    Args:
        path: 読み込むJSON。

    Returns:
        Docling object。欠損または破損時はNone。
    """

    try:
        value = load_json(path)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


@observed("translate-workflow", capture_input=False)
def run_translation(
    source: Path,
    output_root: Path,
    settings: Settings,
    backend: Backend = "openai",
    glossary_path: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> Path | None:
    """PDF翻訳workflowをResume可能な状態で最後まで実行する。

    Args:
        source: 入力PDF。
        output_root: source stem directoryを作るroot。
        settings: 検証済み実行設定。
        backend: 翻訳backend。
        glossary_path: 任意のv5用語集。
        dry_run: 計画表示だけにするか。
        force: 内部成果物を全て再構築するか。

    Returns:
        生成DOCX path。dry-runはNone。
    """

    if source.suffix.casefold() != ".pdf":
        raise ValueError("translate input must be a PDF")
    if not source.is_file():
        raise FileNotFoundError(source)
    check_pandoc()
    source_hash = sha256_file(source)
    page_count = len(pdf_pages_text(source))
    if page_count < 1:
        raise ValueError("PDF has no pages")
    output = output_root / source.stem
    work = output / ".work"
    state_path = work / "state.json"
    config = _config_snapshot(settings, backend)
    # lock前の読み取りは計画表示用であり、実書込みはlock取得後だけ行う。
    existing = load_json(state_path)
    state = (
        existing
        if isinstance(existing, dict)
        else new_state(source_hash, list(range(2, page_count + 1)), config)
    )
    state = invalidate_state(state, source_hash, config, force)
    for line in progress_lines(state):
        print(line)
    if dry_run:
        return None
    glossary = read_glossary(glossary_path)
    update_current(
        input={"source": str(source), "source_hash": source_hash, "backend": backend}
    )
    with OutputLock(work):
        if force:
            # 出力DOCXは成功するまで残し、再構築対象はResume用artifactに限定する。
            _clear_known_work(work)
            state = new_state(source_hash, list(range(2, page_count + 1)), config)
        atomic_write_json(state_path, state)
        try:
            parsed_path = work / "parsed.json"
            parsed = (
                _load_parsed(parsed_path)
                if state["stages"]["parse"] == "done"
                else None
            )
            if parsed is None:
                parsed = convert_pdf(
                    source,
                    parsed_path,
                    work / "structured" / "assets",
                    settings.docling_url or "",
                    settings.docling_api_key,
                )
                state["stages"]["parse"] = "done"
                # artifact保存後にstateを進め、doneなのに成果物がない状態を作らない。
                atomic_write_json(state_path, state)
            normalized_path = work / "normalized.json"
            document = (
                _load_document(normalized_path)
                if state["stages"]["normalize"] == "done"
                else None
            )
            if document is None:
                document = normalize_docling(parsed)
                atomic_write_text(
                    normalized_path, document.model_dump_json(indent=2) + "\n"
                )
                state["stages"]["normalize"] = "done"
                atomic_write_json(state_path, state)
            source_pages = {page.number: page for page in document.pages}
            structure_rules = read_rules(settings, "structure")
            translation_rules = read_rules(settings, "translation")
            review_rules = read_rules(settings, "review")
            completed: list[Page] = []
            for number in range(2, page_count + 1):
                # 表紙は画像として別途転記する契約なので、本文pipelineは2ページ目から始める。
                page_state = state["pages"][str(number)]
                try:
                    structured_path = _page_path(work / "structured", number)
                    structured = (
                        _load_page(structured_path)
                        if page_state["structure"] == "done"
                        else None
                    )
                    if structured is None:
                        print(
                            f"Structure: page {number}/{page_count} started",
                            flush=True,
                        )
                        page_state["structure"] = "pending"
                        page_state["translate"] = "pending"
                        page_state["review"] = "pending"
                        atomic_write_json(state_path, state)
                        image = render_page(
                            source,
                            number,
                            work / "structured" / f"page_{number:06d}.png",
                        )
                        structured = structure_page(
                            source_pages[number], image, structure_rules, settings
                        )
                        atomic_write_text(
                            structured_path, structured.model_dump_json(indent=2) + "\n"
                        )
                        page_state["structure"] = "done"
                        atomic_write_json(state_path, state)
                        print(
                            f"Structure: page {number}/{page_count} success",
                            flush=True,
                        )
                    translated_path = _page_path(work / "translated", number)
                    translated_page = (
                        _load_page(translated_path)
                        if page_state["translate"] == "done"
                        else None
                    )
                    if translated_page is None:
                        print(
                            f"Translate: page {number}/{page_count} started",
                            flush=True,
                        )
                        page_state["translate"] = "pending"
                        page_state["review"] = "pending"
                        atomic_write_json(state_path, state)
                        translated_page = translate_page(
                            structured,
                            page_text(
                                source_pages.get(number - 1, Page(number=number - 1)),
                                False,
                            ),
                            page_text(
                                source_pages.get(number + 1, Page(number=number + 1)),
                                False,
                            ),
                            translation_rules,
                            glossary,
                            settings,
                            backend,
                        )
                        atomic_write_text(
                            translated_path,
                            translated_page.model_dump_json(indent=2) + "\n",
                        )
                        page_state["translate"] = "done"
                        atomic_write_json(state_path, state)
                        print(
                            f"Translate: page {number}/{page_count} success",
                            flush=True,
                        )
                    reviewed_path = _page_path(work / "reviewed", number)
                    reviewed_page = (
                        _load_page(reviewed_path)
                        if page_state["review"] == "done"
                        else None
                    )
                    if reviewed_page is None:
                        print(f"Review: page {number}/{page_count} started", flush=True)
                        page_state["review"] = "pending"
                        atomic_write_json(state_path, state)
                        reviewed_page = _review_page(
                            translated_page, review_rules, settings, glossary
                        )
                        atomic_write_text(
                            reviewed_path,
                            reviewed_page.model_dump_json(indent=2) + "\n",
                        )
                        page_state["review"] = "done"
                        atomic_write_json(state_path, state)
                        print(f"Review: page {number}/{page_count} success", flush=True)
                    completed.append(reviewed_page)
                except BaseException as error:
                    active = next(
                        (
                            name
                            for name in ("structure", "translate", "review")
                            if page_state[name] != "done"
                        ),
                        "review",
                    )
                    page_state[active] = "failed"
                    state["last_error"] = {
                        "page": number,
                        "stage": active,
                        "message": str(error),
                    }
                    atomic_write_json(state_path, state)
                    raise
            for stage in ("structure", "translate", "review"):
                state["stages"][stage] = "done"
            final_document = Document(pages=completed)
            cover = render_cover(source, work / "structured" / "cover.png")
            validate_document(final_document, work)
            markdown_path = work / "document.ja.md"
            atomic_write_text(
                markdown_path,
                render_document(final_document, Path("structured") / cover.name),
            )
            state["stages"]["markdown"] = "done"
            atomic_write_json(state_path, state)
            docx_path = output / "document.ja.docx"
            create_docx(
                markdown_path, docx_path, settings.templates_dir / "template.docx"
            )
            state["stages"]["docx"] = "done"
            state["last_error"] = None
            atomic_write_json(state_path, state)
            update_current(output={"docx": str(docx_path)})
            return docx_path
        finally:
            flush_safely()
