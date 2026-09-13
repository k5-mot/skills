"""内部文書モデルとDocling正規化を検証する。"""

from __future__ import annotations

from typing import Any, cast

import pytest

from src.model import Block, BlockKind, Document, Inline, Page, TableCell
from src.processing.normalize import TEXT_KINDS, normalize_docling


def test_document_round_trip_covers_supported_structure() -> None:
    """全Block種別と主要Inline属性がJSON往復で失われないことを確認する。

    Returns:
        なし。
    """

    kinds = [
        "paragraph",
        "heading",
        "list_item",
        "blockquote",
        "alert",
        "code",
        "formula",
        "table",
        "figure",
        "footnote",
        "horizontal_rule",
    ]
    blocks = [
        Block(
            id=f"b{index}",
            order=index,
            kind=cast(BlockKind, kind),
            source=[
                Inline(
                    id=f"i{index}",
                    text="Text",
                    kind="link",
                    href="https://example.com",
                    marks=["strong"],
                )
            ],
            level=2 if kind in {"heading", "list_item"} else None,
            checked=True if kind == "list_item" else None,
            alert_kind="warning" if kind == "alert" else None,
            asset_path="assets/image.png" if kind == "figure" else None,
            cells=[
                TableCell(
                    row=0,
                    column=0,
                    colspan=2,
                    header=True,
                    source=[Inline(id="cell", text="Cell")],
                )
            ]
            if kind == "table"
            else [],
        )
        for index, kind in enumerate(kinds)
    ]
    document = Document(pages=[Page(number=1, width=100, height=200, blocks=blocks)])
    assert Document.model_validate_json(document.model_dump_json()) == document


def _docling_document() -> dict[str, Any]:
    """正規化test向けの既知要素Docling documentを作る。

    Returns:
        text、table、picture、groupを持つDocling JSON。
    """

    return {
        "schema_name": "DoclingDocument",
        "version": "1.0",
        "pages": {"1": {"size": {"width": 100, "height": 200}}},
        "body": {
            "children": [
                {"$ref": "#/groups/0"},
                {"$ref": "#/tables/0"},
                {"$ref": "#/pictures/0"},
            ]
        },
        "furniture": {"children": []},
        "groups": [
            {
                "self_ref": "#/groups/0",
                "label": "group",
                "children": [{"$ref": "#/texts/0"}, {"$ref": "#/texts/1"}],
            }
        ],
        "texts": [
            {
                "self_ref": "#/texts/0",
                "label": "section_header",
                "text": "Title",
                "level": 2,
                "prov": [{"page_no": 1}],
            },
            {
                "self_ref": "#/texts/1",
                "label": "checkbox_selected",
                "text": "Done",
                "prov": [{"page_no": 1}],
            },
        ],
        "tables": [
            {
                "self_ref": "#/tables/0",
                "label": "table",
                "caption": "Table",
                "prov": [{"page_no": 1}],
                "data": {
                    "table_cells": [
                        {
                            "text": "Cell",
                            "start_row_offset_idx": 0,
                            "end_row_offset_idx": 1,
                            "start_col_offset_idx": 0,
                            "end_col_offset_idx": 2,
                            "column_header": True,
                        }
                    ]
                },
            }
        ],
        "pictures": [
            {
                "self_ref": "#/pictures/0",
                "label": "picture",
                "caption": "Figure",
                "image": {"uri": "artifacts/pic.png"},
                "prov": [{"page_no": 1}],
            }
        ],
        "key_value_items": [],
        "form_items": [],
    }


def test_normalize_preserves_order_and_geometry() -> None:
    """既知Docling要素が順序、階層、表span、assetを保つことを確認する。

    Returns:
        なし。
    """

    document = normalize_docling(_docling_document())
    page = document.pages[0]
    assert [block.kind for block in page.blocks] == [
        "heading",
        "list_item",
        "table",
        "figure",
    ]
    assert page.blocks[0].level == 2
    assert page.blocks[1].checked is True
    assert page.blocks[2].cells[0].colspan == 2
    assert page.blocks[3].asset_path == "structured/assets/pic.png"


def test_normalize_rejects_content_bearing_unknown_leaf() -> None:
    """内容を持つ未知labelがref付きで失敗することを確認する。

    Returns:
        なし。
    """

    document = _docling_document()
    document["texts"].append(
        {
            "self_ref": "#/texts/2",
            "label": "key_value_region",
            "text": "Name: value",
            "prov": [{"page_no": 1}],
        }
    )
    with pytest.raises(ValueError, match=r"#/texts/2.*key_value_region"):
        normalize_docling(document)


def test_normalize_allows_empty_unknown_and_group() -> None:
    """空の未知要素と内容を持たないgroupを許容することを確認する。

    Returns:
        なし。
    """

    document = _docling_document()
    document["texts"].append(
        {
            "self_ref": "#/texts/2",
            "label": "future_empty",
            "text": "",
            "prov": [{"page_no": 1}],
        }
    )
    assert len(normalize_docling(document).pages[0].blocks) == 4


def test_normalize_rejects_picture_without_asset() -> None:
    """画像参照がないpictureを黙って失わないことを確認する。

    Returns:
        なし。
    """

    document = _docling_document()
    document["pictures"][0].pop("image")
    with pytest.raises(ValueError, match="picture asset"):
        normalize_docling(document)


@pytest.mark.parametrize(("label", "kind"), sorted(TEXT_KINDS.items()))
def test_normalize_maps_every_supported_text_label(label: str, kind: BlockKind) -> None:
    """全ての既知Docling text labelを対応Blockへ変換する。

    Args:
        label: Docling label。
        kind: 期待するv5 Block種別。

    Returns:
        なし。
    """

    document = _docling_document()
    document["body"] = {"children": [{"$ref": "#/texts/0"}]}
    document["texts"] = [
        {
            "self_ref": "#/texts/0",
            "label": label,
            "text": "x",
            "latex": "x",
            "prov": [{"page_no": 1}],
        }
    ]
    document["tables"] = []
    document["pictures"] = []
    assert normalize_docling(document).pages[0].blocks[0].kind == kind


def test_normalize_rejects_unresolved_reference() -> None:
    """解決不能なDocling refを参照文字列付きで拒否する。

    Returns:
        なし。
    """

    document = _docling_document()
    document["body"] = {"children": [{"$ref": "#/texts/999"}]}
    with pytest.raises(ValueError, match=r"#/texts/999"):
        normalize_docling(document)
