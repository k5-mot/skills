"""Docling JSONをv5の文書モデルへ正規化する。"""

from __future__ import annotations

import re
from typing import Any, Iterator, cast

from src.model import (
    Block,
    BlockKind,
    Document,
    Inline,
    InlineKind,
    InlineMark,
    Page,
    TableCell,
    inline_text,
)

COLLECTIONS = ("texts", "tables", "pictures", "key_value_items", "form_items", "groups")
SKIPPED_LABELS = {"page_header", "page_footer", "document_index"}
INDEX_TITLE_RE = re.compile(
    r"^(?:table of contents|contents|list of figures|list of tables|目次|図目次|表目次)$",
    re.IGNORECASE,
)
TEXT_KINDS = {
    "title": "heading",
    "section_header": "heading",
    "heading": "heading",
    "header": "heading",
    "paragraph": "paragraph",
    "text": "paragraph",
    "list_item": "list_item",
    "checkbox_selected": "list_item",
    "checkbox_unselected": "list_item",
    "caption": "paragraph",
    "footnote": "footnote",
    "code": "code",
    "program_listing": "code",
    "formula": "formula",
}


def _resolve(document: dict[str, Any], ref: str) -> dict[str, Any]:
    """Docling内部refをobjectへ解決する。

    Args:
        document: Docling document。
        ref: `#/collection/index`形式の参照。

    Returns:
        参照先object。

    Raises:
        ValueError: 参照が不正または解決不能の場合。
    """

    value: Any = document
    try:
        for part in ref.removeprefix("#/").split("/"):
            value = value[int(part)] if isinstance(value, list) else value[part]
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise ValueError(f"unresolved Docling ref: {ref}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Docling ref is not an object: {ref}")
    return value


def _walk_refs(
    document: dict[str, Any],
    value: Any,
    list_level: int = 0,
    ordered: bool | None = None,
) -> Iterator[tuple[dict[str, Any], int, bool | None]]:
    """tree内refを文書順に展開する。

    Args:
        document: Docling document。
        value: tree nodeまたは値。

    Yields:
        groupを除く参照先要素、親list階層、順序付きlistかどうかの組。
    """

    if isinstance(value, list):
        for child in value:
            yield from _walk_refs(document, child, list_level, ordered)
        return
    if not isinstance(value, dict):
        return
    ref = value.get("$ref")
    target = _resolve(document, ref) if isinstance(ref, str) else value
    label = str(target.get("label", ""))
    children = target.get("children", [])
    # groupは配置情報だけなので平坦化するが、可視内容を持つ親は子より先に残す。
    if children and not _visible_content(target):
        nested_level = (
            list_level + 1 if label in {"list", "ordered_list"} else list_level
        )
        nested_ordered = (
            label == "ordered_list" if label in {"list", "ordered_list"} else ordered
        )
        yield from _walk_refs(document, children, nested_level, nested_ordered)
    elif isinstance(ref, str):
        yield target, list_level, ordered
        if children:
            yield from _walk_refs(document, children, list_level, ordered)
    else:
        yield from _walk_refs(document, children, list_level, ordered)


def _visible_content(item: dict[str, Any]) -> bool:
    """要素自身が失うべきでない可視内容を持つか判定する。

    Args:
        item: Docling要素。

    Returns:
        text、画像、表dataまたは数式が存在する場合はTrue。
    """

    text = item.get("text")
    return bool(
        (isinstance(text, str) and text.strip())
        or item.get("image")
        or item.get("data")
        or item.get("latex")
    )


def _page_number(item: dict[str, Any]) -> int:
    """provenanceから1始まりページ番号を得る。

    Args:
        item: Docling要素。

    Returns:
        ページ番号。情報がない場合は1。
    """

    provenance = item.get("prov")
    if isinstance(provenance, list) and provenance and isinstance(provenance[0], dict):
        value = provenance[0].get("page_no", 1)
        if isinstance(value, int) and value > 0:
            return value
    return 1


def _bbox(item: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """provenanceのbboxを数値tupleへ変換する。

    Args:
        item: Docling要素。

    Returns:
        left、top、right、bottom。利用不能ならNone。
    """

    provenance = item.get("prov")
    if (
        not isinstance(provenance, list)
        or not provenance
        or not isinstance(provenance[0], dict)
    ):
        return None
    box = provenance[0].get("bbox")
    if not isinstance(box, dict):
        return None
    try:
        return (
            float(box["l"]),
            float(box["t"]),
            float(box["r"]),
            float(box["b"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _inline(
    ref: str,
    text: str,
    kind: InlineKind = "text",
    href: str | None = None,
    marks: list[InlineMark] | None = None,
) -> list[Inline]:
    """Docling文字列を一つのInline列へ変換する。

    Args:
        ref: 安定IDのprefix。
        text: 可視文字列。
        kind: Inline種別。
        href: link URL。
        marks: Inline装飾。

    Returns:
        空文字なら空、それ以外は一要素のInline列。
    """

    if not text:
        return []
    return [
        Inline(
            id=f"{ref}/inline/0",
            text=text,
            kind=kind,
            href=href,
            marks=marks or [],
        )
    ]


def _inline_from_item(
    item: dict[str, Any], ref: str, text: str, kind: InlineKind
) -> list[Inline]:
    """Docling TextItemのlinkと装飾をInlineへ移す。

    Args:
        item: Docling TextItem。
        ref: 安定IDのprefix。
        text: 可視文字列。
        kind: 基本Inline種別。

    Returns:
        hyperlinkとFormattingを保持したInline列。
    """

    formatting = item.get("formatting")
    marks: list[InlineMark] = []
    if isinstance(formatting, dict):
        mark_fields: tuple[tuple[str, InlineMark], ...] = (
            ("bold", "strong"),
            ("italic", "emphasis"),
            ("strikethrough", "strikethrough"),
            ("underline", "underline"),
        )
        marks.extend(mark for field, mark in mark_fields if formatting.get(field))
        script = formatting.get("script")
        if script == "sub":
            marks.append("subscript")
        elif script == "super":
            marks.append("superscript")
    hyperlink = item.get("hyperlink")
    href = str(hyperlink) if hyperlink is not None else None
    inline_kind: InlineKind = "link" if href and kind == "text" else kind
    return _inline(ref, text, inline_kind, href, marks)


def _caption_inlines(
    document: dict[str, Any], item: dict[str, Any], ref: str
) -> list[Inline]:
    """Doclingの新旧caption表現をInline列へ変換する。

    Args:
        document: ref解決元のDocling document。
        item: TableItemまたはPictureItem。
        ref: caption IDのprefix。

    Returns:
        参照先の装飾も保持したcaption Inline列。
    """

    raw = item.get("captions")
    if not raw:
        raw = item.get("caption") or item.get("title") or []
    values = raw if isinstance(raw, list) else [raw]
    result: list[Inline] = []
    for index, value in enumerate(values):
        caption_item: dict[str, Any] | None = None
        if isinstance(value, dict):
            nested_ref = value.get("$ref")
            caption_item = (
                _resolve(document, nested_ref) if isinstance(nested_ref, str) else value
            )
        if caption_item is not None:
            text = str(caption_item.get("text") or "")
            result.extend(
                _inline_from_item(caption_item, f"{ref}/caption/{index}", text, "text")
            )
        elif isinstance(value, str):
            result.extend(_inline(f"{ref}/caption/{index}", value))
    return result


def _owned_caption_refs(document: dict[str, Any]) -> set[str]:
    """図表Blockへ内包するcaption TextItemのrefを集める。

    Args:
        document: Docling document。

    Returns:
        standalone本文として重複出力しないcaption ref集合。
    """

    # captionは図表Blockへ所有させ、body側TextItemとの二重出力を防ぐ。
    refs: set[str] = set()
    for collection in ("tables", "pictures"):
        for item in document.get(collection, []):
            if not isinstance(item, dict):
                continue
            raw = item.get("captions") or item.get("caption") or []
            values = raw if isinstance(raw, list) else [raw]
            refs.update(
                str(value["$ref"])
                for value in values
                if isinstance(value, dict) and isinstance(value.get("$ref"), str)
            )
    return refs


def _index_pages(document: dict[str, Any]) -> set[int]:
    """元文書の目次類として本文から除くページを特定する。

    Args:
        document: Docling document。

    Returns:
        document index labelまたは既知の目次見出しを持つページ番号集合。
    """

    return {
        _page_number(item)
        for collection in COLLECTIONS
        for item in document.get(collection, [])
        if isinstance(item, dict)
        and (
            item.get("label") == "document_index"
            or (
                item.get("label") in {"title", "section_header", "heading", "header"}
                and INDEX_TITLE_RE.fullmatch(str(item.get("text") or "").strip())
            )
        )
    }


def _picture_content_refs(document: dict[str, Any]) -> set[str]:
    """picture配下でcaptionではない重複textのrefを集める。

    Args:
        document: Docling document。

    Returns:
        Figure asset側に任せて本文出力しないtext ref集合。
    """

    parents: dict[str, str] = {}
    picture_refs = {
        str(item.get("self_ref"))
        for item in document.get("pictures", [])
        if isinstance(item, dict) and item.get("self_ref")
    }
    for collection in COLLECTIONS:
        for item in document.get(collection, []):
            if not isinstance(item, dict) or not item.get("self_ref"):
                continue
            parent = item.get("parent")
            if isinstance(parent, dict) and isinstance(parent.get("$ref"), str):
                parents[str(item["self_ref"])] = str(parent["$ref"])
    # 親chainを辿るのは、picture直下にgroupを挟むDocling出力にも対応するため。
    result: set[str] = set()
    for item in document.get("texts", []):
        if (
            not isinstance(item, dict)
            or item.get("label") == "caption"
            or not item.get("self_ref")
        ):
            continue
        ref = str(item["self_ref"])
        parent = parents.get(ref, "")
        visited: set[str] = set()
        while parent and parent not in visited:
            if parent in picture_refs:
                result.add(ref)
                break
            visited.add(parent)
            parent = parents.get(parent, "")
    return result


def _integer(value: Any, default: int) -> int:
    """Doclingの任意値を安全に整数へ変換する。

    Args:
        value: 変換候補。
        default: 変換不能時の値。

    Returns:
        整数値。
    """

    return value if isinstance(value, int) else default


def _cell_text(cell: dict[str, Any]) -> str:
    """Docling table cellから文字列を得る。

    Args:
        cell: table cell object。

    Returns:
        textまたは空文字列。
    """

    value = cell.get("text") or cell.get("text_content") or ""
    return str(value)


def _table_cells(item: dict[str, Any], ref: str) -> list[TableCell]:
    """既知のDocling table schemaをTableCell列へ変換する。

    Args:
        item: Docling table要素。
        ref: tableの安定ID。

    Returns:
        行列位置を持つcell列。

    Raises:
        ValueError: 対応するcell配列がない場合。
    """

    data = item.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"unsupported Docling table: {ref}")
    cells: list[TableCell] = []
    grid = data.get("grid")
    if isinstance(grid, list):
        # 旧grid形式にはspan情報がないため、配列位置をそのまま座標にする。
        for row_index, row in enumerate(grid):
            if not isinstance(row, list):
                continue
            for column_index, cell in enumerate(row):
                if isinstance(cell, dict):
                    typed_cell = cast(dict[str, Any], cell)
                    cell_ref = f"{ref}/cell/{row_index}/{column_index}"
                    cells.append(
                        TableCell(
                            row=row_index,
                            column=column_index,
                            header=bool(
                                typed_cell.get("column_header")
                                or typed_cell.get("row_header")
                            ),
                            source=_inline(cell_ref, _cell_text(typed_cell)),
                        )
                    )
        return cells
    values = data.get("table_cells") or data.get("cells")
    if not isinstance(values, list):
        raise ValueError(f"unsupported Docling table: {ref}")
    # 新形式は半開区間のoffsetからrowspan/colspanを復元する。
    for index, cell in enumerate(values):
        if not isinstance(cell, dict):
            continue
        typed_cell = cast(dict[str, Any], cell)
        row = _integer(typed_cell.get("start_row_offset_idx", typed_cell.get("row")), 0)
        column = _integer(
            typed_cell.get("start_col_offset_idx", typed_cell.get("col")), 0
        )
        row_end = _integer(typed_cell.get("end_row_offset_idx"), row + 1)
        column_end = _integer(typed_cell.get("end_col_offset_idx"), column + 1)
        cell_ref = f"{ref}/cell/{index}"
        cells.append(
            TableCell(
                row=row,
                column=column,
                rowspan=max(1, row_end - row),
                colspan=max(1, column_end - column),
                header=bool(
                    typed_cell.get("column_header") or typed_cell.get("row_header")
                ),
                source=_inline(cell_ref, _cell_text(typed_cell)),
            )
        )
    return cells


def _block(
    document: dict[str, Any],
    item: dict[str, Any],
    order: int,
    list_level: int = 0,
    parent_ordered: bool | None = None,
) -> Block | None:
    """一つのDocling要素をBlockへ変換する。

    Args:
        document: ref解決元のDocling document。
        item: Docling要素。
        order: ページ内の文書順。
        list_level: 親ListGroupから得た1始まり階層。
        parent_ordered: 親ListGroupが順序付きかどうか。

    Returns:
        対応Block。仕様上除外する要素はNone。

    Raises:
        ValueError: 内容を持つ未知要素または欠損assetの場合。
    """

    ref = str(item.get("self_ref") or f"#/unknown/{order}")
    label = str(item.get("label", ""))
    if label in SKIPPED_LABELS:
        return None
    if label == "table":
        return Block(
            id=ref,
            order=order,
            kind="table",
            bbox=_bbox(item),
            caption=_caption_inlines(document, item, ref),
            cells=_table_cells(item, ref),
        )
    if label == "picture":
        image = item.get("image")
        uri = image.get("uri") if isinstance(image, dict) else None
        uri = uri or item.get("uri")
        if not isinstance(uri, str) or not uri:
            raise ValueError(f"picture asset is missing: ref={ref} label={label}")
        relative = uri.removeprefix("artifacts/")
        uri = f"structured/assets/{relative}"
        caption = _caption_inlines(document, item, ref)
        return Block(
            id=ref,
            order=order,
            kind="figure",
            bbox=_bbox(item),
            asset_path=uri,
            alt_text=str(item.get("alt_text") or inline_text(caption)),
            caption=caption,
        )
    kind = TEXT_KINDS.get(label)
    if kind is None:
        if not _visible_content(item):
            return None
        # 内容のある未知要素をparagraphへ丸めると欠損に気付けないためfail-fastする。
        raise ValueError(
            f"unsupported content-bearing Docling element: ref={ref} label={label}"
        )
    text = str(item.get("text") or item.get("latex") or "")
    inline_kind = "code" if kind in {"code", "formula"} else "text"
    level = item.get("level") if kind == "heading" else None
    checked = (
        True
        if label == "checkbox_selected"
        else False
        if label == "checkbox_unselected"
        else None
    )
    return Block(
        id=ref,
        order=order,
        kind=cast(BlockKind, kind),
        bbox=_bbox(item),
        source=_inline_from_item(item, ref, text, inline_kind),
        level=int(level)
        if isinstance(level, int)
        else 1
        if kind == "heading"
        else max(1, list_level)
        if kind == "list_item"
        else None,
        ordered=bool(item.get("enumerated"))
        if "enumerated" in item
        else bool(parent_ordered),
        checked=checked,
        language=str(item.get("language")) if item.get("language") else None,
    )


def normalize_docling(document: dict[str, Any]) -> Document:
    """Docling documentをページ順のv5 Documentへ変換する。

    Args:
        document: Docling Serveが返したJSON object。

    Returns:
        正規化済みDocument。

    Raises:
        ValueError: schema、ページ、参照、要素が契約を満たさない場合。
    """

    if document.get("schema_name") != "DoclingDocument":
        raise ValueError(f"unsupported Docling schema: {document.get('schema_name')!r}")
    raw_pages = document.get("pages")
    if not isinstance(raw_pages, dict):
        raise ValueError("Docling pages must be a non-empty object")
    if not raw_pages:
        origin = document.get("origin", {})
        mimetype = origin.get("mimetype") if isinstance(origin, dict) else None
        if mimetype not in {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        }:
            raise ValueError("Docling pages must be a non-empty object")
        # Office文書はDoclingがpageを返さないため、全要素を仮想ページへ載せる。
        raw_pages = {"1": {}}
    pages: dict[int, Page] = {}
    for raw_number, raw_page in raw_pages.items():
        number = int(raw_number)
        size = raw_page.get("size", {}) if isinstance(raw_page, dict) else {}
        pages[number] = Page(
            number=number,
            width=float(size["width"])
            if isinstance(size, dict) and "width" in size
            else None,
            height=float(size["height"])
            if isinstance(size, dict) and "height" in size
            else None,
        )
    seen: set[str] = set()
    caption_refs = _owned_caption_refs(document)
    index_pages = _index_pages(document)
    picture_content_refs = _picture_content_refs(document)
    # bodyの文書順を正本とし、bodyに現れないcollection要素だけを後段で補完する。
    ordered_items = list(_walk_refs(document, document.get("body", {})))
    for collection in COLLECTIONS[:-1]:
        values = document.get(collection, [])
        if not isinstance(values, list):
            raise ValueError(f"Docling collection must be a list: {collection}")
        ordered_items.extend(
            (
                item,
                1
                if str(item.get("label", ""))
                in {"list_item", "checkbox_selected", "checkbox_unselected"}
                else 0,
                None,
            )
            for item in values
            if isinstance(item, dict)
        )
    page_orders: dict[int, int] = {number: 0 for number in pages}
    for item, list_level, parent_ordered in ordered_items:
        ref = str(item.get("self_ref") or "")
        if ref in seen:
            continue
        seen.add(ref)
        if ref in caption_refs or ref in picture_content_refs:
            continue
        number = _page_number(item)
        if number not in pages:
            raise ValueError(
                f"Docling element references missing page: ref={ref} page={number}"
            )
        if number in index_pages:
            continue
        block = _block(document, item, page_orders[number], list_level, parent_ordered)
        if block is not None:
            pages[number].blocks.append(block)
            page_orders[number] += 1
    return Document(pages=[pages[number] for number in sorted(pages)])
