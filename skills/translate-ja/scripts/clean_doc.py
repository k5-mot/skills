"""Docling schema JSON のテキストを保守的に成形する。"""

from __future__ import annotations

import argparse
import copy
import logging
import re
from pathlib import Path
from time import perf_counter
from typing import Any

from io_utils import configure_logging, read_json, write_json

LOGGER = logging.getLogger("translate-ja.clean_doc")

URL_RE = re.compile(r"https?://[^\s)>\"]+")
EXCLUDED_LABELS = {"code", "formula", "table_cell", "page_header", "page_footer"}


def _node_label(node: dict[str, Any]) -> str:
    """Docling ノードの label を小文字で返す。"""

    return str(node.get("label") or node.get("type") or "").lower()


def _page_numbers(node: dict[str, Any]) -> list[int]:
    """Docling ノードの prov からページ番号を抽出する。"""

    pages: set[int] = set()
    prov = node.get("prov")
    if isinstance(prov, list):
        for entry in prov:
            if isinstance(entry, dict) and isinstance(entry.get("page_no"), int):
                pages.add(entry["page_no"])
    return sorted(pages)


def _format_pages(pages: list[int]) -> str:
    """ログ用にページ番号リストを短く表現する。"""

    if not pages:
        return "unknown"
    ranges: list[str] = []
    start = previous = pages[0]
    for page in pages[1:]:
        if page == previous + 1:
            previous = page
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = page
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def should_clean_node(node: dict[str, Any]) -> bool:
    """成形対象にしてよいテキストノードかを判定する。"""

    label = _node_label(node)
    if any(excluded in label for excluded in EXCLUDED_LABELS):
        return False
    if node.get("content_layer") == "furniture":
        return False
    return True


def clean_text(value: str) -> str:
    """URL を保護しながら過剰な記号と空白を縮約する。"""

    urls: list[str] = []

    def stash_url(match: re.Match[str]) -> str:
        """URL を一時プレースホルダーへ退避する。"""

        urls.append(match.group(0))
        return f"__TRANSLATE_JA_URL_{len(urls) - 1}__"

    text = URL_RE.sub(stash_url, value)
    text = re.sub(r"\.{4,}", "...", text)
    text = re.sub(r"・{4,}", "・・・", text)
    text = re.sub(r"[‐‑‒–—―-]{4,}", "---", text)
    text = re.sub(r"([_=~*])\1{5,}", r"\1\1\1", text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    for index, url in enumerate(urls):
        text = text.replace(f"__TRANSLATE_JA_URL_{index}__", url)
    return text


def clean_docling_json(data: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Docling JSON を再帰的に走査し、text フィールドを成形する。"""

    result = copy.deepcopy(data)
    changes: list[dict[str, Any]] = []

    def visit(value: Any, pointer: str, parent: dict[str, Any] | None = None) -> None:
        """JSON ツリーを再帰的に訪問する。"""

        if isinstance(value, dict):
            if "text" in value and isinstance(value["text"], str) and should_clean_node(value):
                before = value["text"]
                after = clean_text(before)
                if before != after:
                    value["text"] = after
                    pages = _page_numbers(value)
                    changes.append(
                        {
                            "rule": "conservative_text_cleanup",
                            "node_ref": value.get("self_ref") or pointer,
                            "page_numbers": pages,
                            "before_chars": len(before),
                            "after_chars": len(after),
                            "before_sample": before[:120],
                            "after_sample": after[:120],
                        }
                    )
            for key, child in value.items():
                visit(child, f"{pointer}/{key}", value)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{pointer}/{index}", parent)

    visit(result, "#")
    return result, changes


def main() -> int:
    """CLI 引数を読み、Docling JSON のテキスト成形を実行する。"""

    configure_logging()
    parser = argparse.ArgumentParser(description="Clean Docling schema JSON text nodes")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=False)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    output = Path(args.output)
    if output.exists() and not args.force:
        LOGGER.info("既存出力を再利用します output=%s", output)
        return 0
    data = read_json(args.input)
    if not isinstance(data, dict):
        raise ValueError(f"Docling JSON object expected: {args.input}")
    cleaned, changes = clean_docling_json(data)
    write_json(output, cleaned)
    for change in changes:
        LOGGER.info(
            "テキスト成形を適用しました ref=%s pages=%s before_chars=%s after_chars=%s",
            change.get("node_ref"),
            _format_pages(change.get("page_numbers") or []),
            change.get("before_chars"),
            change.get("after_chars"),
        )
    if args.report:
        write_json(
            args.report,
            {
                "schema_version": 1,
                "input_path": args.input,
                "output_path": args.output,
                "change_count": len(changes),
                "changes": changes,
            },
        )
    LOGGER.info("テキスト成形が完了しました changes=%s output=%s", len(changes), output)
    return 0


if __name__ == "__main__":
    started_at = perf_counter()
    try:
        exit_code = main()
    finally:
        LOGGER.info("処理時間 %.3f 秒", perf_counter() - started_at)
    raise SystemExit(exit_code)
