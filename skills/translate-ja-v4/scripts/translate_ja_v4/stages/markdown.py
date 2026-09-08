"""MarkdownStageで翻訳済みDocling JSONを描画する。"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

from ..config import PipelineState, state_paths
from ..document import iter_table_cells
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

FENCE_PATTERN = re.compile(r"^(`{3,}|~{3,})(.*)$")
IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
PLACEHOLDER_PATTERN = re.compile(r"ZXQKEEPX*\d{6}QXZ")


class MarkdownValidationError(ValueError):
    """生成Markdownの構文またはローカル参照違反を表す。"""


def _render_value(item: dict[str, Any]) -> str:
    """翻訳metadataを優先して描画文字列を返す。

    Args:
        item: Docling要素。

    Returns:
        Markdownへ出力する文字列。
    """

    meta = item.get("translate_ja_v4")
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

    spans = item.get("structure_ja_v4", {}).get("inline_code_spans", [])
    for span in sorted(spans if isinstance(spans, list) else [], key=len, reverse=True):
        text = text.replace(span, f"`{span}`")
    return text


def _code_block(text: str) -> str:
    """内容と衝突しない長さのbacktick fenceでcode blockを作る。

    Args:
        text: code block本文。

    Returns:
        fenced code block。
    """

    longest = max(
        (len(match.group(0)) for match in re.finditer(r"`+", text)), default=0
    )
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def _validate_markdown(value: str, output_dir: Path) -> None:
    """Markdownのfence、table、画像参照、制御文字を検証する。

    Args:
        value: 検証するMarkdown文字列。
        output_dir: `artifacts/` を含む成果物directory。

    Returns:
        なし。

    Raises:
        MarkdownValidationError: 構文または画像参照が不正な場合。
    """

    errors: list[str] = []
    controls = [
        character
        for character in value
        if ord(character) < 32 and character not in "\n\r\t"
    ]
    if controls:
        errors.append("unsupported control characters")
    if PLACEHOLDER_PATTERN.search(value):
        errors.append("unresolved translation placeholder")

    fence: tuple[str, int, int] | None = None
    outside_lines: list[tuple[int, str]] = []
    for line_number, line in enumerate(value.splitlines(), start=1):
        match = FENCE_PATTERN.match(line)
        if fence is not None:
            marker, length, _opening_line = fence
            stripped = line.strip()
            if stripped and set(stripped) == {marker} and len(stripped) >= length:
                fence = None
            continue
        if match:
            marker = match.group(1)
            fence = (marker[0], len(marker), line_number)
        else:
            outside_lines.append((line_number, line))
    if fence is not None:
        errors.append(f"unclosed code fence at line {fence[2]}")

    table_width: int | None = None
    for line_number, line in outside_lines:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            width = len(re.findall(r"(?<!\\)\|", stripped)) - 1
            if width < 1:
                errors.append(f"invalid table row at line {line_number}")
            elif table_width is not None and width != table_width:
                errors.append(f"inconsistent table width at line {line_number}")
            table_width = width
        else:
            table_width = None

    for raw_uri in IMAGE_PATTERN.findall("\n".join(line for _, line in outside_lines)):
        uri = unquote(raw_uri.strip().split(maxsplit=1)[0].strip("<>"))
        parsed = urlparse(uri)
        pure = PurePosixPath(uri)
        if parsed.scheme or Path(uri).is_absolute() or ".." in pure.parts:
            errors.append(f"unsafe image URI: {raw_uri}")
        elif not output_dir.joinpath(*pure.parts).is_file():
            errors.append(f"missing image: {raw_uri}")
    if errors:
        raise MarkdownValidationError("; ".join(errors))


def _table(table: dict[str, Any], table_index: int) -> str:
    """Docling tableをMarkdown tableへ変換する。

    Args:
        table: Docling table要素。
        table_index: documentのtables配列内index。

    Returns:
        captionを含むMarkdown断片。
    """

    cells = list(iter_table_cells(table, table_index))
    if not cells:
        return ""
    height = max(cell[3] for cell in cells) + 1
    width = max(cell[4] for cell in cells) + 1
    rows = [["" for _column in range(width)] for _row in range(height)]
    for _ref, _path, cell, row, column in cells:
        value = _inline_code(_render_value(cell), cell)
        rows[row][column] = value.replace("|", "\\|").replace("\n", "<br>")
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

    entries: list[tuple[int, int, str, dict[str, Any], int]] = []
    order = 0
    for group in ("texts", "tables", "pictures"):
        for index, item in enumerate(document.get(group, [])):
            if not isinstance(item, dict):
                continue
            prov = item.get("prov") or [{}]
            page = (
                prov[0].get("page_no", 10**9)
                if isinstance(prov, list) and prov
                else 10**9
            )
            entries.append((int(page), order, group, item, index))
            order += 1
    parts: list[str] = []
    for _page, _order, group, item, index in sorted(entries):
        if group == "tables":
            value = _table(item, index)
        elif group == "pictures":
            uri = item.get("image", {}).get("uri") or item.get("uri")
            value = f"![{text_of(item) or 'image'}]({uri})" if uri else ""
        else:
            text = _render_value(item).strip()
            label = str(item.get("label", ""))
            if label in {"code", "program_listing"}:
                value = _code_block(text)
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
    input_hash, config_hash = hash_file(paths.reviewed_json), hash_json({"version": 2})
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
        value = markdown(read_json(paths.reviewed_json))
        _validate_markdown(value, paths.output_dir)
        write_bytes(paths.markdown, value.encode("utf-8"))
        record_stage(
            paths.manifest,
            "markdown",
            "completed",
            input_hash,
            config_hash,
            paths.markdown,
            {"validated": True},
        )
    return {"current_path": str(paths.markdown), "completed_stage": "markdown"}
