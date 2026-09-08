"""NormalizeStageで座標順とDocling参照を補正する。"""

from __future__ import annotations

import copy
import re
from typing import Any, cast

from ..config import PipelineState, state_paths
from ..document import matching_spans
from ..io import (
    LOGGER,
    hash_file,
    hash_json,
    read_json,
    record_stage,
    self_ref,
    stage_cached,
    text_of,
    write_json,
)

COLLECTIONS = ("groups", "texts", "pictures", "tables", "key_value_items", "form_items")
REMOVED_LABELS = {"page_header", "page_footer"}
FRAGMENT_LABELS = {"text", "paragraph", "list_item", "code", "program_listing"}
INDEX_TITLE_RE = re.compile(
    r"^(?:table of contents|contents|list of figures|list of tables|目次|図目次|表目次)$",
    re.IGNORECASE,
)


def _position(item: dict[str, Any], index: int) -> tuple[int, float, float, int] | None:
    """text要素の読み順sort keyを返す。

    Args:
        item: Docling text要素。
        index: 元の配列index。

    Returns:
        page、上端、左端、元index。座標がなければNone。
    """

    prov = item.get("prov")
    if not isinstance(prov, list) or not prov or not isinstance(prov[0], dict):
        return None
    entry = prov[0]
    bbox = entry.get("bbox")
    if not isinstance(bbox, dict):
        return None
    page = int(entry.get("page_no", 10**9))
    top = float(bbox.get("t", bbox.get("top", 0)))
    bottom = float(bbox.get("b", bbox.get("bottom", 0)))
    left = float(bbox.get("l", bbox.get("left", 0)))
    origin = str(bbox.get("coord_origin", "BOTTOMLEFT")).upper()
    vertical = min(top, bottom) if origin == "TOPLEFT" else -max(top, bottom)
    return page, vertical, left, index


def _replace_refs(value: Any, mapping: dict[str, str], removed: set[str]) -> None:
    """JSON内の参照を再採番し、削除済み参照を除去する。

    Args:
        value: 更新対象JSON値。
        mapping: 旧refから新refへの対応。
        removed: 削除する旧ref集合。

    Returns:
        なし。

    Side Effects:
        渡されたJSON objectを直接更新する。
    """

    if isinstance(value, dict):
        for key, child in list(value.items()):
            child_ref = child.get("$ref") if isinstance(child, dict) else None
            if key == "$ref" and isinstance(child, str):
                value[key] = mapping.get(child, child)
            elif isinstance(child_ref, str) and child_ref in removed:
                if key == "parent":
                    value[key] = {"$ref": "#/body"}
                else:
                    del value[key]
            else:
                _replace_refs(child, mapping, removed)
    elif isinstance(value, list):
        value[:] = [
            child
            for child in value
            if not (
                isinstance(child, dict)
                and isinstance(child.get("$ref"), str)
                and child["$ref"] in removed
            )
        ]
        for child in value:
            _replace_refs(child, mapping, removed)


def _page_no(item: dict[str, Any]) -> int | None:
    """要素の先頭ページ番号を返す。

    Args:
        item: Docling要素。

    Returns:
        ページ番号。存在しなければNone。
    """

    prov = item.get("prov")
    if isinstance(prov, list) and prov and isinstance(prov[0], dict):
        page = prov[0].get("page_no")
        return page if isinstance(page, int) else None
    return None


def _parent_ref(item: dict[str, Any]) -> str:
    """要素の親refを返す。

    Args:
        item: Docling要素。

    Returns:
        親ref。存在しなければ空文字。
    """

    parent = item.get("parent")
    return str(parent.get("$ref", "")) if isinstance(parent, dict) else ""


def _compact(document: dict[str, Any], removed: set[str]) -> None:
    """top-level collectionを詰め、全参照を更新する。

    Args:
        document: 更新対象Docling JSON。
        removed: 削除する旧ref集合。

    Returns:
        なし。

    Side Effects:
        collectionとJSON参照を直接更新する。
    """

    mapping: dict[str, str] = {}
    for group in COLLECTIONS:
        values = document.get(group)
        if not isinstance(values, list):
            continue
        kept: list[Any] = []
        for index, value in enumerate(values):
            if not isinstance(value, dict):
                kept.append(value)
                continue
            item = cast(dict[str, Any], value)
            old = self_ref(item, group, index)
            if old in removed:
                continue
            new = f"#/{group}/{len(kept)}"
            item["self_ref"] = new
            mapping[old] = new
            kept.append(item)
        document[group] = kept
    _replace_refs(document, mapping, removed)


def _noise_refs(document: dict[str, Any]) -> set[str]:
    """header/footer、図中文字、文書indexの削除対象refを返す。

    Args:
        document: ParseStage成果物。

    Returns:
        NormalizeStageで削除するtop-level ref集合。
    """

    labeled_items = (
        item
        for group in COLLECTIONS
        for item in document.get(group, [])
        if isinstance(item, dict)
    )
    index_pages = {
        page
        for item in labeled_items
        if (
            item.get("label") == "document_index"
            or (
                item.get("label") in {"title", "section_header", "heading", "header"}
                and INDEX_TITLE_RE.match(text_of(item).strip())
            )
        )
        and (page := _page_no(item)) is not None
    }
    picture_items = [
        item for item in document.get("pictures", []) if isinstance(item, dict)
    ]
    picture_refs = {
        self_ref(item, "pictures", index)
        for index, item in enumerate(document.get("pictures", []))
        if isinstance(item, dict)
    }
    parents = {
        self_ref(item, group, index): _parent_ref(item)
        for group in COLLECTIONS
        for index, item in enumerate(document.get(group, []))
        if isinstance(item, dict)
    }

    def inside_picture(item: dict[str, Any]) -> bool:
        """要素の親階層にpictureがあるか判定する。

        Args:
            item: 判定対象要素。

        Returns:
            図内要素ならTrue。
        """

        parent = _parent_ref(item)
        visited: set[str] = set()
        while parent and parent not in visited:
            if parent in picture_refs:
                return True
            visited.add(parent)
            parent = parents.get(parent, "")
        return False

    def overlaps_picture(item: dict[str, Any]) -> bool:
        """text bboxの大部分がpicture bbox内にあるか判定する。

        Args:
            item: 判定対象text。

        Returns:
            text面積の80%以上が同一ページpictureと重なる場合はTrue。
        """

        target = _bbox(item)
        if target is None:
            return False
        left, bottom, right, top, origin = target
        area = max(0.0, right - left) * max(0.0, top - bottom)
        for picture in picture_items:
            candidate = _bbox(picture)
            if (
                candidate is None
                or _page_no(picture) != _page_no(item)
                or candidate[4] != origin
            ):
                continue
            overlap = max(
                0.0, min(right, candidate[2]) - max(left, candidate[0])
            ) * max(0.0, min(top, candidate[3]) - max(bottom, candidate[1]))
            if area > 0 and overlap >= area * 0.8:
                return True
        return False

    removed: set[str] = set()
    for group in COLLECTIONS:
        for index, item in enumerate(document.get(group, [])):
            if not isinstance(item, dict):
                continue
            ref = self_ref(item, group, index)
            page = _page_no(item)
            if page in index_pages or (
                group == "texts"
                and (
                    item.get("label") in REMOVED_LABELS
                    or (
                        item.get("label") != "caption"
                        and (inside_picture(item) or overlaps_picture(item))
                    )
                )
            ):
                removed.add(ref)
    return removed


def _bbox(item: dict[str, Any]) -> tuple[float, float, float, float, str] | None:
    """text要素のbboxを数値化する。

    Args:
        item: Docling text要素。

    Returns:
        left、bottom、right、top、座標原点。bboxがなければNone。
    """

    prov = item.get("prov")
    entry = prov[0] if isinstance(prov, list) and prov else None
    box = entry.get("bbox") if isinstance(entry, dict) else None
    if not isinstance(box, dict):
        return None
    try:
        left, right = sorted((float(box["l"]), float(box["r"])))
        bottom, top = sorted((float(box["b"]), float(box["t"])))
    except (KeyError, TypeError, ValueError):
        return None
    return left, bottom, right, top, str(box.get("coord_origin", "BOTTOMLEFT"))


def _same_line(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """二つのtext bboxが同じ行で近接するか判定する。

    Args:
        left: 文書順で先のtext。
        right: 文書順で後のtext。

    Returns:
        同一行の断片とみなせる場合はTrue。
    """

    first, second = _bbox(left), _bbox(right)
    if first is None or second is None or first[4] != second[4]:
        return False
    vertical = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    height = min(first[3] - first[1], second[3] - second[1])
    gap = max(0.0, second[0] - first[2])
    return height > 0 and vertical / height >= 0.5 and gap <= height * 2


def _nearby(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """同じPDF span内で縦方向に近い断片か判定する。

    Args:
        left: 文書順で先のtext。
        right: 文書順で後のtext。

    Returns:
        行高の1.5倍以内ならTrue。
    """

    first, second = _bbox(left), _bbox(right)
    if first is None or second is None or first[4] != second[4]:
        return False
    height = max(first[3] - first[1], second[3] - second[1])
    gap = (
        first[1] - second[3]
        if first[4].upper() == "BOTTOMLEFT"
        else second[1] - first[3]
    )
    return max(0.0, gap) <= height * 1.5


def _paragraph_continuation(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """隣接する本文が改行で分割された同一段落か保守的に判定する。

    Args:
        left: 文書順で先の本文。
        right: 文書順で後の本文。

    Returns:
        左端が揃い、近接し、前半が終端記号で終わらない場合はTrue。
    """

    first, second = _bbox(left), _bbox(right)
    if first is None or second is None or first[4] != second[4]:
        return False
    height = max(first[3] - first[1], second[3] - second[1])
    aligned = abs(first[0] - second[0]) <= max(6, height)
    return (
        aligned
        and _nearby(left, right)
        and not re.search(r"[.!?。！？:：;；]\s*$", text_of(left))
    )


def _merge_value(left: str, right: str) -> str:
    """text断片を不要な二重空白なしで連結する。

    Args:
        left: 前半文字列。
        right: 後半文字列。

    Returns:
        空白を補った連結文字列。
    """

    if not left or not right or left[-1].isspace() or right[0].isspace():
        return left + right
    separator = "" if re.match(r"^[,.;:!?)]", right) else " "
    return left + separator + right


def _merge_text_fragments(document: dict[str, Any]) -> set[str]:
    """座標とPDF spanが示す明白なtext断片を連結する。

    Args:
        document: 読み順補正済みDocling JSON。

    Returns:
        連結後に削除する右側text ref集合。

    Side Effects:
        左側textへ原文とbboxを統合する。
    """

    texts = document.get("texts")
    if not isinstance(texts, list):
        return set()
    removed: set[str] = set()
    index = 0
    while index + 1 < len(texts):
        left, right = texts[index], texts[index + 1]
        if not isinstance(left, dict) or not isinstance(right, dict):
            index += 1
            continue
        labels = {str(left.get("label")), str(right.get("label"))}
        compatible = left.get("label") == right.get("label") or labels <= {
            "text",
            "paragraph",
        }
        left_spans = {span.get("id") for span in matching_spans(document, left)}
        right_spans = {span.get("id") for span in matching_spans(document, right)}
        same_span = bool(left_spans & right_spans)
        merge = (
            labels <= FRAGMENT_LABELS
            and compatible
            and _parent_ref(left) == _parent_ref(right)
            and _page_no(left) == _page_no(right)
            and (
                _same_line(left, right)
                or (same_span and _nearby(left, right))
                or (
                    labels <= {"text", "paragraph"}
                    and _paragraph_continuation(left, right)
                )
            )
        )
        if not merge:
            index += 1
            continue
        separator = "\n" if labels & {"code", "program_listing"} else None
        left["text"] = (
            f"{text_of(left)}{separator}{text_of(right)}"
            if separator
            else _merge_value(text_of(left), text_of(right))
        )
        if isinstance(left.get("orig"), str) and isinstance(right.get("orig"), str):
            left["orig"] = _merge_value(left["orig"], right["orig"])
        left.setdefault("normalize_ja_v4", {}).setdefault("merged_refs", []).append(
            self_ref(right, "texts", index + 1)
        )
        removed.add(self_ref(right, "texts", index + 1))
        texts.pop(index + 1)
    return removed


def _table_width(table: dict[str, Any]) -> int:
    """table gridの最大列数を返す。

    Args:
        table: Docling table。

    Returns:
        最大列数。gridがなければ0。
    """

    data = table.get("data", {})
    declared = data.get("num_cols") if isinstance(data, dict) else None
    if isinstance(declared, int) and declared > 0:
        return declared
    grid = data.get("grid", []) if isinstance(data, dict) else []
    return max((len(row) for row in grid if isinstance(row, list)), default=0)


def _table_continuation(
    document: dict[str, Any], left: dict[str, Any], right: dict[str, Any]
) -> bool:
    """二つのtableが同一表の連続片か座標で判定する。

    Args:
        document: page sizeを持つDocling JSON。
        left: 前半table。
        right: 後半table。

    Returns:
        同じ列数で同一ページ隣接または改ページ継続ならTrue。
    """

    left_page, right_page = _page_no(left), _page_no(right)
    first, second = _bbox(left), _bbox(right)
    if (
        left_page is None
        or right_page is None
        or first is None
        or second is None
        or _table_width(left) == 0
        or _table_width(left) != _table_width(right)
    ):
        return False
    if left_page == right_page:
        return 0 <= first[1] - second[3] <= 24
    if right_page != left_page + 1:
        return False
    page = document.get("pages", {}).get(str(left_page), {})
    size = page.get("size", {}) if isinstance(page, dict) else {}
    height = float(size.get("height", 0)) if isinstance(size, dict) else 0
    return bool(height and first[1] <= height * 0.2 and second[3] >= height * 0.8)


def _row_values(row: Any) -> list[str]:
    """table rowを比較用のcell文字列へ変換する。

    Args:
        row: grid row候補。

    Returns:
        cell文字列配列。
    """

    if not isinstance(row, list):
        return []
    return [text_of(cell) if isinstance(cell, dict) else str(cell) for cell in row]


def _merge_table_fragments(document: dict[str, Any]) -> set[str]:
    """ページ境界または同一ページで連続するtable断片を連結する。

    Args:
        document: Docling JSON。

    Returns:
        連結後に削除する右側table ref集合。

    Side Effects:
        左側tableのgrid、prov、metadataを更新する。
    """

    tables = document.get("tables")
    if not isinstance(tables, list):
        return set()
    removed: set[str] = set()
    index = 0
    while index + 1 < len(tables):
        left, right = tables[index], tables[index + 1]
        if (
            not isinstance(left, dict)
            or not isinstance(right, dict)
            or left.get("label") != "table"
            or right.get("label") != "table"
            or not _table_continuation(document, left, right)
        ):
            index += 1
            continue
        left_grid = left.setdefault("data", {}).setdefault("grid", [])
        right_grid = right.get("data", {}).get("grid", [])
        if not isinstance(left_grid, list) or not isinstance(right_grid, list):
            index += 1
            continue
        rows = list(right_grid)
        if left_grid and rows and _row_values(left_grid[0]) == _row_values(rows[0]):
            rows = rows[1:]
        left_grid.extend(rows)
        previous_rows = int(left["data"].get("num_rows", len(left_grid) - len(rows)))
        dropped_header = bool(right_grid and rows != list(right_grid))
        row_offset = previous_rows - int(dropped_header)
        right_cells = right.get("data", {}).get("table_cells", [])
        left_cells = left["data"].setdefault("table_cells", [])
        if isinstance(left_cells, list) and isinstance(right_cells, list):
            for cell in right_cells:
                if not isinstance(cell, dict):
                    continue
                start = cell.get("start_row_offset_idx")
                end = cell.get("end_row_offset_idx")
                if dropped_header and start == 0:
                    continue
                if isinstance(start, int):
                    cell["start_row_offset_idx"] = start + row_offset
                if isinstance(end, int):
                    cell["end_row_offset_idx"] = end + row_offset
                left_cells.append(cell)
        right_rows = int(right.get("data", {}).get("num_rows", len(right_grid)))
        left["data"]["num_rows"] = previous_rows + right_rows - int(dropped_header)
        left["data"]["num_cols"] = _table_width(left)
        if isinstance(left.get("prov"), list) and isinstance(right.get("prov"), list):
            left["prov"].extend(right["prov"])
        left.setdefault("normalize_ja_v4", {}).setdefault("merged_refs", []).append(
            self_ref(right, "tables", index + 1)
        )
        removed.add(self_ref(right, "tables", index + 1))
        tables.pop(index + 1)
    return removed


def normalize(document: dict[str, Any]) -> dict[str, Any]:
    """不要要素を除去し、読み順と明白な断片を正規化する。

    Args:
        document: ParseStageのDocling JSON。

    Returns:
        参照整合済みの正規化JSON。
    """

    result = copy.deepcopy(document)
    removed = _noise_refs(result)
    if removed:
        _compact(result, removed)
    texts = result.get("texts")
    if not isinstance(texts, list):
        return result
    located: list[tuple[int, dict[str, Any]]] = []
    for index, value in enumerate(texts):
        item = cast(dict[str, Any], value) if isinstance(value, dict) else None
        if isinstance(item, dict) and _position(item, index):
            located.append((index, item))
    ordered = [
        item
        for index, item in sorted(
            located,
            key=lambda pair: _position(pair[1], pair[0]) or (10**9, 0, 0, pair[0]),
        )
    ]
    slots = [index for index, _item in located]
    previous_refs = {
        id(item): str(item.get("self_ref", f"#/texts/{index}"))
        for index, item in enumerate(texts)
        if isinstance(item, dict)
    }
    for slot, item in zip(slots, ordered, strict=True):
        texts[slot] = item
    mapping: dict[str, str] = {}
    for index, value in enumerate(texts):
        item: Any = value
        if isinstance(item, dict):
            old = previous_refs[id(item)]
            new = f"#/texts/{index}"
            item["self_ref"] = new
            mapping[old] = new
    _replace_refs(result, mapping, set())
    merged = _merge_text_fragments(result) | _merge_table_fragments(result)
    if merged:
        _compact(result, merged)
    return result


def normalize_stage(state: PipelineState) -> PipelineState:
    """Docling JSONを座標順へ正規化するLangGraph node。

    Args:
        state: 直前成果物を含むgraph state。

    Returns:
        Normalize成果物パスを設定した部分state。
    """

    paths = state_paths(state)
    source = paths.document_json
    input_hash, config_hash = hash_file(source), hash_json({"version": 2})
    if stage_cached(
        paths.manifest, "normalize", input_hash, config_hash, paths.normalized_json
    ):
        LOGGER.info("Resumed NormalizeStage output=%s", paths.normalized_json)
    else:
        record_stage(
            paths.manifest,
            "normalize",
            "running",
            input_hash,
            config_hash,
            paths.normalized_json,
        )
        write_json(paths.normalized_json, normalize(read_json(source)))
        record_stage(
            paths.manifest,
            "normalize",
            "completed",
            input_hash,
            config_hash,
            paths.normalized_json,
        )
    return {"current_path": str(paths.normalized_json), "completed_stage": "normalize"}
