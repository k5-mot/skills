"""翻訳済み Chunk JSONL を日本語 Markdown へ連結する。"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from time import perf_counter

from io_utils import configure_logging, read_jsonl, atomic_write_text

LOGGER = logging.getLogger("translate-ja.concat_chunks")


def _format_pages(pages: object) -> str:
    """ログ用に chunk のページ番号リストを短く表現する。"""

    if not isinstance(pages, list):
        return "unknown"
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


def _resolve_input(path: str | Path) -> Path:
    """入力パスがディレクトリなら chunks.ja.jsonl を補完する。"""

    target = Path(path)
    if target.is_dir():
        return target / "chunks.ja.jsonl"
    return target


def validate_markdown(markdown: str) -> list[str]:
    """Markdown の基本構造を検証し、警告リストを返す。"""

    warnings: list[str] = []
    if markdown.count("```") % 2:
        warnings.append("fenced code block is not closed")
    for line_no, line in enumerate(markdown.splitlines(), start=1):
        if not line.startswith("|") or line.count("|") < 2:
            continue
        if re.fullmatch(r"\|\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?", line.strip()):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not cells:
            warnings.append(f"empty markdown table row at line {line_no}")
    return warnings


def concat_chunks(rows: list[dict[str, object]]) -> str:
    """chunk_id 順に translated_text を連結して Markdown を作る。"""

    sorted_rows = sorted(rows, key=lambda row: str(row.get("chunk_id") or ""))
    parts: list[str] = []
    previous = ""
    for row in sorted_rows:
        text = str(row.get("translated_text") or row.get("source_text") or "").strip()
        text = text.replace("Chunks(JS)", "Chunks(JA)")
        if not text:
            continue
        if previous and previous == text:
            continue
        parts.append(text)
        previous = text
    markdown = "\n\n".join(parts)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip() + "\n"
    return markdown


def main() -> int:
    """CLI 引数を読み、翻訳済み chunks を Markdown へ連結する。"""

    configure_logging()
    parser = argparse.ArgumentParser(
        description="Concatenate translated chunks into Markdown"
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    output = Path(args.output)
    if output.exists() and not args.force:
        LOGGER.info("既存出力を再利用します output=%s", output)
        return 0
    rows = read_jsonl(_resolve_input(args.input))
    for row in rows:
        chunk_id = str(row.get("chunk_id") or "unknown")
        pages = _format_pages(row.get("page_numbers"))
        text = str(row.get("translated_text") or row.get("source_text") or "")
        if row.get("status") == "fallback_source":
            LOGGER.warning(
                "原文 fallback chunk を連結します chunk=%s pages=%s error=%s",
                chunk_id,
                pages,
                row.get("error"),
            )
        for warning in validate_markdown(text):
            LOGGER.warning(
                "Markdown chunk 検証警告 chunk=%s pages=%s warning=%s",
                chunk_id,
                pages,
                warning,
            )
    markdown = concat_chunks(rows)
    warnings = validate_markdown(markdown)
    for warning in warnings:
        LOGGER.warning("Markdown 全体検証警告: %s", warning)
    fallback_count = sum(1 for row in rows if row.get("status") == "fallback_source")
    LOGGER.info(
        "Markdown 連結が完了しました chunks=%s fallback_source=%s",
        len(rows),
        fallback_count,
    )
    atomic_write_text(output, markdown)
    return 0


if __name__ == "__main__":
    started_at = perf_counter()
    try:
        exit_code = main()
    finally:
        LOGGER.info("処理時間 %.3f 秒", perf_counter() - started_at)
    sys.exit(exit_code)
