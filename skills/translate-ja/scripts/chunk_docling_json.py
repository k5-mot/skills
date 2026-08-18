"""Docling schema JSON から翻訳単位の Chunk JSONL を生成する。"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from time import perf_counter
from typing import Any

from io_utils import configure_logging, read_json, write_jsonl

LOGGER = logging.getLogger("translate-ja.chunk_docling_json")


HEADING_LABELS = {"title", "section_header", "heading", "header"}
CODE_LABELS = {"code", "program_listing"}
HTML_LABELS = {"html"}


def _label(item: dict[str, Any]) -> str:
    """Docling item の label を小文字で返す。"""

    return str(item.get("label") or item.get("type") or "").lower()


def _text(item: dict[str, Any]) -> str:
    """Docling item から本文テキストを取り出す。"""

    for key in ("text", "orig", "content"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _self_ref(item: dict[str, Any], group: str, index: int) -> str:
    """Docling item の self_ref または推定 JSON pointer を返す。"""

    return str(item.get("self_ref") or f"#/{group}/{index}")


def _page_numbers(item: dict[str, Any]) -> list[int]:
    """Docling item の prov からページ番号を抽出する。"""

    pages: set[int] = set()
    prov = item.get("prov")
    if isinstance(prov, list):
        for entry in prov:
            if isinstance(entry, dict) and isinstance(entry.get("page_no"), int):
                pages.add(entry["page_no"])
    return sorted(pages)


def _format_pages(pages: list[int] | set[int] | tuple[int, ...]) -> str:
    """ログ用にページ番号リストを短く表現する。"""

    ordered = sorted({page for page in pages if isinstance(page, int)})
    if not ordered:
        return "unknown"
    ranges: list[str] = []
    start = previous = ordered[0]
    for page in ordered[1:]:
        if page == previous + 1:
            previous = page
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = page
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def _heading_level(item: dict[str, Any]) -> int:
    """見出しレベルを Docling item から推定する。"""

    value = item.get("level") or item.get("heading_level")
    if isinstance(value, int) and value > 0:
        return min(value, 6)
    return 1 if _label(item) == "title" else 2


def _render_text_item(item: dict[str, Any]) -> str:
    """Docling text item を Markdown 断片へ変換する。"""

    text = _text(item)
    if not text:
        return ""
    label = _label(item)
    if label in HEADING_LABELS:
        return f"{'#' * _heading_level(item)} {text}"
    if label in CODE_LABELS:
        return f"```\n{text}\n```"
    if label in HTML_LABELS or text.lstrip().startswith("<"):
        return text
    return text


def _table_matrix_from_item(item: dict[str, Any]) -> list[list[str]]:
    """Docling table item から可能な範囲でセル行列を復元する。"""

    data = item.get("data")
    if not isinstance(data, dict):
        return []
    grid = data.get("grid")
    if isinstance(grid, list) and all(isinstance(row, list) for row in grid):
        return [
            [
                str(cell.get("text") if isinstance(cell, dict) else cell or "").strip()
                for cell in row
            ]
            for row in grid
        ]
    cells = data.get("table_cells") or data.get("cells")
    if not isinstance(cells, list):
        return []
    max_row = -1
    max_col = -1
    normalized: list[tuple[int, int, str]] = []
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        row = cell.get("start_row_offset_idx", cell.get("row", cell.get("row_idx", 0)))
        col = cell.get("start_col_offset_idx", cell.get("col", cell.get("col_idx", 0)))
        if not isinstance(row, int) or not isinstance(col, int):
            continue
        text = str(cell.get("text") or cell.get("content") or "").strip()
        normalized.append((row, col, text))
        max_row = max(max_row, row)
        max_col = max(max_col, col)
    if max_row < 0 or max_col < 0:
        return []
    matrix = [["" for _ in range(max_col + 1)] for _ in range(max_row + 1)]
    for row, col, text in normalized:
        matrix[row][col] = text
    return matrix


def render_table_item(item: dict[str, Any]) -> str:
    """Docling table item を Markdown table へ変換する。"""

    matrix = _table_matrix_from_item(item)
    if not matrix:
        return _text(item)
    width = max(len(row) for row in matrix)
    rows = [row + [""] * (width - len(row)) for row in matrix]
    header = rows[0] if rows else []
    separator = ["---"] * width
    body = rows[1:] if len(rows) > 1 else []

    def line(row: list[str]) -> str:
        """Markdown table の 1 行を作る。"""

        return "| " + " | ".join(cell.replace("\n", " ") for cell in row) + " |"

    return "\n".join([line(header), line(separator), *(line(row) for row in body)])


def _assets(item: dict[str, Any]) -> list[str]:
    """Docling item から画像などの参照 asset を抽出する。"""

    refs: list[str] = []
    image = item.get("image")
    if isinstance(image, dict) and isinstance(image.get("uri"), str):
        refs.append(image["uri"])
    return refs


def iter_doc_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Docling JSON から処理対象 item を文書順に集める。"""

    items: list[dict[str, Any]] = []
    for group in ("texts", "tables", "pictures"):
        values = data.get(group)
        if not isinstance(values, list):
            continue
        for index, value in enumerate(values):
            if isinstance(value, dict):
                copied = dict(value)
                copied["_group"] = group
                copied["_index"] = index
                items.append(copied)
    items.sort(
        key=lambda item: (_page_numbers(item) or [10**9])[0],
    )
    return items


def _chunk_kind(item: dict[str, Any]) -> str:
    """Docling item から chunk kind を決める。"""

    group = item.get("_group")
    label = _label(item)
    if group == "tables":
        return "table"
    if group == "pictures":
        return "image"
    if label in CODE_LABELS:
        return "code"
    if label in HTML_LABELS:
        return "html"
    return "text"


def _validate_chunks(chunks: list[dict[str, Any]]) -> None:
    """Chunk JSONL の基本制約を検証する。"""

    seen: set[str] = set()
    for chunk in chunks:
        chunk_id = chunk["chunk_id"]
        if chunk_id in seen:
            raise ValueError(f"duplicate chunk_id: {chunk_id}")
        seen.add(chunk_id)
        if (
            chunk.get("translatable")
            and not str(chunk.get("source_text") or "").strip()
        ):
            raise ValueError(f"empty translatable chunk: {chunk_id}")
        if not chunk.get("source_node_refs"):
            raise ValueError(f"empty source_node_refs: {chunk_id}")
        text = str(chunk.get("source_text") or "")
        if text.count("```") % 2:
            raise ValueError(f"unclosed code fence: {chunk_id}")


def build_chunks(
    data: dict[str, Any], *, min_chars: int = 1000, max_chars: int = 2000
) -> list[dict[str, Any]]:
    """Docling JSON から見出しパス付き chunk 配列を作る。"""

    chunks: list[dict[str, Any]] = []
    header_path: list[str] = []
    pending_blocks: list[str] = []
    pending_refs: list[str] = []
    pending_pages: set[int] = set()
    pending_assets: list[str] = []

    def flush() -> None:
        """保留中の text blocks を 1 chunk として確定する。"""

        nonlocal pending_blocks, pending_refs, pending_pages, pending_assets
        if not pending_blocks:
            return
        source_text = "\n\n".join(
            block for block in pending_blocks if block.strip()
        ).strip()
        if not source_text:
            pending_blocks = []
            pending_refs = []
            pending_pages = set()
            pending_assets = []
            return
        chunks.append(
            {
                "chunk_id": f"chunk-{len(chunks) + 1:04d}",
                "kind": "text",
                "header_path": list(header_path),
                "source_text": source_text,
                "translatable": True,
                "char_count": len(source_text),
                "source_node_refs": list(pending_refs),
                "page_numbers": sorted(pending_pages),
                "assets": list(dict.fromkeys(pending_assets)),
            }
        )
        pending_blocks = []
        pending_refs = []
        pending_pages = set()
        pending_assets = []

    for item in iter_doc_items(data):
        kind = _chunk_kind(item)
        group = str(item.get("_group"))
        index = int(item.get("_index", 0))
        ref = _self_ref(item, group, index)
        pages = _page_numbers(item)
        assets = _assets(item)
        rendered = (
            render_table_item(item) if kind == "table" else _render_text_item(item)
        )
        if not rendered:
            continue
        if kind in {"table", "code", "html", "image"}:
            flush()
            chunks.append(
                {
                    "chunk_id": f"chunk-{len(chunks) + 1:04d}",
                    "kind": kind,
                    "header_path": list(header_path),
                    "source_text": rendered,
                    "translatable": kind != "code",
                    "char_count": len(rendered),
                    "source_node_refs": [ref],
                    "page_numbers": pages,
                    "assets": assets,
                }
            )
            continue
        if _label(item) in HEADING_LABELS:
            level = min(_heading_level(item), 6)
            header_path = header_path[: level - 1]
            header_path.append(rendered)
            if (
                pending_blocks
                and sum(len(block) for block in pending_blocks) >= min_chars
            ):
                flush()
        pending_blocks.append(rendered)
        pending_refs.append(ref)
        pending_pages.update(pages)
        pending_assets.extend(assets)
        pending_len = sum(len(block) for block in pending_blocks)
        if pending_len >= max_chars and not re.match(r"^#{1,6}\s", pending_blocks[-1]):
            flush()
    flush()
    _validate_chunks(chunks)
    return chunks


def main() -> int:
    """CLI 引数を読み、Chunk JSONL を生成する。"""

    configure_logging()
    parser = argparse.ArgumentParser(
        description="Build Chunk JSONL from Docling schema JSON"
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--min-chars", type=int, default=1000)
    parser.add_argument("--max-chars", type=int, default=2000)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    output = Path(args.output)
    if output.exists() and not args.force:
        LOGGER.info("既存出力を再利用します output=%s", output)
        return 0
    data = read_json(args.input)
    if not isinstance(data, dict):
        raise ValueError(f"Docling JSON object expected: {args.input}")
    chunks = build_chunks(data, min_chars=args.min_chars, max_chars=args.max_chars)
    for chunk in chunks:
        LOGGER.info(
            "チャンクを生成しました chunk=%s kind=%s pages=%s refs=%s chars=%s",
            chunk.get("chunk_id"),
            chunk.get("kind"),
            _format_pages(chunk.get("page_numbers") or []),
            ",".join(str(ref) for ref in chunk.get("source_node_refs") or []),
            chunk.get("char_count"),
        )
    write_jsonl(output, chunks)
    LOGGER.info("チャンク生成が完了しました chunks=%s output=%s", len(chunks), output)
    return 0


if __name__ == "__main__":
    started_at = perf_counter()
    try:
        exit_code = main()
    finally:
        LOGGER.info("処理時間 %.3f 秒", perf_counter() - started_at)
    sys.exit(exit_code)
