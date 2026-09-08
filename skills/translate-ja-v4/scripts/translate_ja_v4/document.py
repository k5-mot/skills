"""Docling文書の翻訳対象を走査する共通処理を提供する。"""

from __future__ import annotations

import re
from typing import Any

from .io import self_ref, text_of

SKIP_LABELS = {"code", "program_listing", "page_header", "page_footer"}
APPENDIX_HEADING_RE = re.compile(r"^\s*APPENDIX\s+[A-Z0-9]+\b", re.IGNORECASE)


def _rect(
    bbox: Any, page_height: float | None = None
) -> tuple[float, float, float, float] | None:
    """DoclingまたはPDF spanのbboxを比較可能な矩形へ変換する。

    Args:
        bbox: l、t、r、bを持つbbox候補。
        page_height: TOPLEFT座標をBOTTOMLEFTへ変換するページ高。

    Returns:
        left、bottom、right、top。bboxが不正ならNone。
    """

    if not isinstance(bbox, dict):
        return None
    try:
        left, right = float(bbox["l"]), float(bbox["r"])
        first, second = float(bbox["b"]), float(bbox["t"])
    except (KeyError, TypeError, ValueError):
        return None
    if str(bbox.get("coord_origin", "BOTTOMLEFT")).upper() == "TOPLEFT":
        if page_height is None:
            return None
        first, second = page_height - first, page_height - second
    bottom, top = sorted((first, second))
    return min(left, right), bottom, max(left, right), top


def matching_spans(
    document: dict[str, Any], item: dict[str, Any]
) -> list[dict[str, Any]]:
    """Docling要素と座標が重なる同一ページのPDF spanを返す。

    Args:
        document: pagesにtext_spansを持つDocling JSON。
        item: provとbboxを持つDocling要素。

    Returns:
        要素面積またはspan面積の15%以上が重なるspan配列。
    """

    prov = item.get("prov")
    if not isinstance(prov, list) or not prov or not isinstance(prov[0], dict):
        return []
    entry = prov[0]
    page = document.get("pages", {}).get(str(entry.get("page_no")), {})
    spans = page.get("text_spans", []) if isinstance(page, dict) else []
    size = page.get("size", {}) if isinstance(page, dict) else {}
    raw_height = size.get("height") if isinstance(size, dict) else None
    page_height = float(raw_height) if isinstance(raw_height, (int, float)) else None
    target = _rect(entry.get("bbox"), page_height)
    if target is None:
        return []
    left, bottom, right, top = target
    target_area = max(0.0, right - left) * max(0.0, top - bottom)
    matched: list[dict[str, Any]] = []
    for span in spans if isinstance(spans, list) else []:
        if not isinstance(span, dict):
            continue
        candidate = _rect(span.get("bbox"), page_height)
        if candidate is None:
            continue
        span_left, span_bottom, span_right, span_top = candidate
        overlap = max(0.0, min(right, span_right) - max(left, span_left)) * max(
            0.0, min(top, span_top) - max(bottom, span_bottom)
        )
        span_area = max(0.0, span_right - span_left) * max(0.0, span_top - span_bottom)
        if overlap and overlap >= min(target_area, span_area) * 0.15:
            matched.append(span)
    return matched


def translation_targets(document: dict[str, Any]) -> list[dict[str, Any]]:
    """本文、表題、表セルから英語を含む翻訳対象を文書順に集める。

    Args:
        document: Docling JSON。

    Returns:
        ID、原文、種別、更新先pathを持つ対象配列。
    """

    targets: list[dict[str, Any]] = []
    in_appendix = False
    for index, item in enumerate(document.get("texts", [])):
        if not isinstance(item, dict):
            continue
        source, label = text_of(item), str(item.get("label", "paragraph"))
        headings = {"title", "section_header", "heading", "header"}
        if label in headings and APPENDIX_HEADING_RE.match(source):
            in_appendix = True
        if label in headings and in_appendix:
            continue
        if _translatable(source, label):
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
