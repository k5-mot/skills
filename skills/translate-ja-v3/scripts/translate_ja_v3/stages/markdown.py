"""MarkdownStageで翻訳済みDocling JSONを描画する。"""

from __future__ import annotations

import re
from typing import Any

from ..config import PipelineState, state_paths
from ..io import (
    LOGGER,
    hash_file,
    hash_json,
    read_json,
    record_stage,
    stage_cached,
    text_of,
    write_bytes,
)


def _render_value(item: dict[str, Any]) -> str:
    """翻訳metadataを優先して描画文字列を返す。

    Args:
        item: Docling要素。

    Returns:
        Markdownへ出力する文字列。
    """

    meta = item.get("translate_ja_v3")
    if isinstance(meta, dict) and meta.get("render_text"):
        return str(meta["render_text"])
    return text_of(item) or str(item.get("caption") or item.get("title") or "")


def _inline_code(text: str, item: dict[str, Any]) -> str:
    """StructureStageが特定したexact spanをbacktickで囲む。

    Args:
        text: 描画対象文字列。
        item: inline code metadataを持つ要素。

    Returns:
        inline code適用済み文字列。
    """

    spans = item.get("structure_ja_v3", {}).get("inline_code_spans", [])
    for span in sorted(spans if isinstance(spans, list) else [], key=len, reverse=True):
        text = text.replace(span, f"`{span}`")
    return text


def _table(table: dict[str, Any]) -> str:
    """Docling tableをMarkdown tableへ変換する。

    Args:
        table: Docling table要素。

    Returns:
        captionを含むMarkdown断片。
    """

    rows: list[list[str]] = []
    grid = table.get("data", {}).get("grid", [])
    for row in grid if isinstance(grid, list) else []:
        values = []
        for cell in row if isinstance(row, list) else []:
            value = (
                _inline_code(_render_value(cell), cell)
                if isinstance(cell, dict)
                else str(cell)
            )
            values.append(value.replace("|", "\\|").replace("\n", "<br>"))
        rows.append(values)
    if not rows:
        return ""
    width = max(map(len, rows))
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines: list[str] = []
    caption = _render_value(table)
    if caption:
        lines.extend([f"**{caption}**", ""])
    lines.append("| " + " | ".join(rows[0]) + " |")
    lines.append("| " + " | ".join(["---"] * width) + " |")
    lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
    return "\n".join(lines)


def markdown(document: dict[str, Any]) -> str:
    """Docling collectionsを文書順のMarkdownへ変換する。

    Args:
        document: ReviewStage成果物。

    Returns:
        UTF-8 Markdown文字列。
    """

    entries: list[tuple[int, int, str, dict[str, Any]]] = []
    order = 0
    for group in ("texts", "tables", "pictures"):
        for item in document.get(group, []):
            if not isinstance(item, dict):
                continue
            prov = item.get("prov") or [{}]
            page = (
                prov[0].get("page_no", 10**9)
                if isinstance(prov, list) and prov
                else 10**9
            )
            entries.append((int(page), order, group, item))
            order += 1
    parts: list[str] = []
    for _page, _order, group, item in sorted(entries):
        if group == "tables":
            value = _table(item)
        elif group == "pictures":
            uri = item.get("image", {}).get("uri") or item.get("uri")
            value = f"![{text_of(item) or 'image'}]({uri})" if uri else ""
        else:
            text = _render_value(item).strip()
            label = str(item.get("label", ""))
            if label in {"code", "program_listing"}:
                value = f"```\n{text}\n```"
            elif label in {"title", "section_header", "heading", "header"}:
                value = f"{'#' * max(1, min(6, int(item.get('level', 1))))} {text}"
            else:
                value = text
        if value.strip():
            parts.append(value.strip())
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(parts)).strip() + "\n"


def markdown_stage(state: PipelineState) -> PipelineState:
    """Review済みJSONをMarkdownへ変換するLangGraph node。

    Args:
        state: Review成果物を含むgraph state。

    Returns:
        Markdown成果物パスを設定した部分state。
    """

    paths = state_paths(state)
    input_hash, config_hash = hash_file(paths.reviewed_json), hash_json({"version": 1})
    if stage_cached(
        paths.manifest, "markdown", input_hash, config_hash, paths.markdown
    ):
        LOGGER.info("Resumed MarkdownStage output=%s", paths.markdown)
    else:
        record_stage(
            paths.manifest,
            "markdown",
            "running",
            input_hash,
            config_hash,
            paths.markdown,
        )
        write_bytes(
            paths.markdown, markdown(read_json(paths.reviewed_json)).encode("utf-8")
        )
        record_stage(
            paths.manifest,
            "markdown",
            "completed",
            input_hash,
            config_hash,
            paths.markdown,
        )
    return {"current_path": str(paths.markdown), "completed_stage": "markdown"}
