"""Docling JSONをv5の文書モデルへ正規化する。"""

from __future__ import annotations

from typing import Any, Iterator, cast

from src.model import Block, BlockKind, Document, Inline, InlineKind, Page, TableCell

COLLECTIONS = ("texts", "tables", "pictures", "key_value_items", "form_items", "groups")
SKIPPED_LABELS = {"page_header", "page_footer", "document_index"}
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


def _walk_refs(document: dict[str, Any], value: Any) -> Iterator[dict[str, Any]]:
    """tree内refを文書順に展開する。

    Args:
        document: Docling document。
        value: tree nodeまたは値。

    Yields:
        groupを除く参照先要素。
    """

    if isinstance(value, list):
        for child in value:
            yield from _walk_refs(document, child)
        return
    if not isinstance(value, dict):
        return
    ref = value.get("$ref")
    target = _resolve(document, ref) if isinstance(ref, str) else value
    label = str(target.get("label", ""))
    children = target.get("children", [])
    if label == "group" or (children and not _visible_content(target)):
        yield from _walk_refs(document, children)
    elif isinstance(ref, str):
        yield target
    else:
        yield from _walk_refs(document, children)


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
    ref: str, text: str, kind: InlineKind = "text", href: str | None = None
) -> list[Inline]:
    """Docling文字列を一つのInline列へ変換する。

    Args:
        ref: 安定IDのprefix。
        text: 可視文字列。
        kind: Inline種別。
        href: link URL。

    Returns:
        空文字なら空、それ以外は一要素のInline列。
    """

    if not text:
        return []
    return [Inline(id=f"{ref}/inline/0", text=text, kind=kind, href=href)]


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


def _block(item: dict[str, Any], order: int) -> Block | None:
    """一つのDocling要素をBlockへ変換する。

    Args:
        item: Docling要素。
        order: ページ内の文書順。

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
        caption = item.get("caption") or item.get("title") or ""
        return Block(
            id=ref,
            order=order,
            kind="table",
            bbox=_bbox(item),
            caption=_inline(f"{ref}/caption", str(caption)),
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
        caption = item.get("caption") or item.get("text") or ""
        return Block(
            id=ref,
            order=order,
            kind="figure",
            bbox=_bbox(item),
            asset_path=uri,
            alt_text=str(item.get("alt_text") or caption),
            caption=_inline(f"{ref}/caption", str(caption)),
        )
    kind = TEXT_KINDS.get(label)
    if kind is None:
        if not _visible_content(item):
            return None
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
        source=_inline(ref, text, inline_kind),
        level=int(level)
        if isinstance(level, int)
        else 1
        if kind == "heading"
        else None,
        ordered=bool(item.get("enumerated")),
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
    if not isinstance(raw_pages, dict) or not raw_pages:
        raise ValueError("Docling pages must be a non-empty object")
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
    ordered_items = list(_walk_refs(document, document.get("body", {})))
    for collection in COLLECTIONS[:-1]:
        values = document.get(collection, [])
        if not isinstance(values, list):
            raise ValueError(f"Docling collection must be a list: {collection}")
        ordered_items.extend(item for item in values if isinstance(item, dict))
    page_orders: dict[int, int] = {number: 0 for number in pages}
    for item in ordered_items:
        ref = str(item.get("self_ref") or "")
        if ref in seen:
            continue
        seen.add(ref)
        number = _page_number(item)
        if number not in pages:
            raise ValueError(
                f"Docling element references missing page: ref={ref} page={number}"
            )
        block = _block(item, page_orders[number])
        if block is not None:
            pages[number].blocks.append(block)
            page_orders[number] += 1
    return Document(pages=[pages[number] for number in sorted(pages)])
