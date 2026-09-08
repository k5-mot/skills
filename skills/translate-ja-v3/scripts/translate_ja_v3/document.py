"""Docling文書の翻訳対象を走査する共通処理を提供する。"""

from __future__ import annotations

import re
from typing import Any

from .io import self_ref, text_of

SKIP_LABELS = {"code", "program_listing", "page_header", "page_footer"}


def translation_targets(document: dict[str, Any]) -> list[dict[str, Any]]:
    """本文、表題、表セルから英語を含む翻訳対象を文書順に集める。

    Args:
        document: Docling JSON。

    Returns:
        ID、原文、種別、更新先pathを持つ対象配列。
    """

    targets: list[dict[str, Any]] = []
    for index, item in enumerate(document.get("texts", [])):
        if not isinstance(item, dict):
            continue
        source, label = text_of(item), str(item.get("label", "paragraph"))
        if _translatable(source, label):
            headings = {"title", "section_header", "heading", "header"}
            targets.append(
                {
                    "id": self_ref(item, "texts", index),
                    "source": source,
                    "kind": "heading" if label in headings else "body",
                    "path": ["texts", index],
                }
            )
    for table_index, table in enumerate(document.get("tables", [])):
        if not isinstance(table, dict):
            continue
        caption = table.get("caption") or table.get("title")
        if isinstance(caption, str) and _translatable(caption, "caption"):
            field = "caption" if "caption" in table else "title"
            targets.append(
                {
                    "id": f"#/tables/{table_index}/caption",
                    "source": caption,
                    "kind": "heading",
                    "path": ["tables", table_index],
                    "field": field,
                }
            )
        grid = table.get("data", {}).get("grid", [])
        for row_index, row in enumerate(grid if isinstance(grid, list) else []):
            for column_index, cell in enumerate(row if isinstance(row, list) else []):
                if isinstance(cell, dict) and _translatable(text_of(cell), "cell"):
                    targets.append(
                        {
                            "id": (
                                f"#/tables/{table_index}/data/grid/"
                                f"{row_index}/{column_index}"
                            ),
                            "source": text_of(cell),
                            "kind": "body",
                            "path": [
                                "tables",
                                table_index,
                                "data",
                                "grid",
                                row_index,
                                column_index,
                            ],
                        }
                    )
    return targets


def resolve_target(document: dict[str, Any], path: list[str | int]) -> dict[str, Any]:
    """対象pathからDocling objectを返す。

    Args:
        document: Docling JSON。
        path: objectへ到達するkeyとindex。

    Returns:
        対象object。

    Raises:
        ValueError: path終端がobjectでない場合。
    """

    value: Any = document
    for part in path:
        value = value[part]
    if not isinstance(value, dict):
        raise ValueError("document target must be an object")
    return value


def batches(
    items: list[dict[str, Any]], max_chars: int, max_elements: int
) -> list[list[dict[str, Any]]]:
    """対象を文字数と任意の件数上限で分割する。

    Args:
        items: 文書順の対象。
        max_chars: 原文合計上限。
        max_elements: 件数上限。0なら無制限。

    Returns:
        順序を維持したbatch配列。
    """

    result: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for item in items:
        length = len(str(item["source"]))
        element_limit = max_elements > 0 and len(current) >= max_elements
        if current and (size + length > max_chars or element_limit):
            result.append(current)
            current, size = [], 0
        current.append(item)
        size += length
    if current:
        result.append(current)
    return result


def _translatable(text: str, label: str) -> bool:
    """要素が自然言語の翻訳対象か判定する。

    Args:
        text: 原文。
        label: Docling label。

    Returns:
        翻訳対象ならTrue。
    """

    return bool(
        text.strip() and label not in SKIP_LABELS and re.search(r"[A-Za-z]", text)
    )
