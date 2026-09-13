"""決定的Markdown、表紙、DOCX前検証を確認する。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.model import Block, Document, Inline, Page, TableCell
from src.processing.render import render_document, validate_document


def _document() -> Document:
    """全主要Markdown構造を含む文書を作る。

    Returns:
        Renderer test用Document。
    """

    return Document(
        pages=[
            Page(
                number=2,
                blocks=[
                    Block(
                        id="h",
                        order=0,
                        kind="heading",
                        level=2,
                        source=[Inline(id="hi", text="Heading", marks=["strong"])],
                    ),
                    Block(
                        id="l",
                        order=1,
                        kind="list_item",
                        level=2,
                        checked=True,
                        source=[Inline(id="li", text="Task")],
                    ),
                    Block(
                        id="q",
                        order=2,
                        kind="blockquote",
                        source=[Inline(id="qi", text="Quote")],
                    ),
                    Block(
                        id="a",
                        order=3,
                        kind="alert",
                        alert_kind="warning",
                        source=[Inline(id="ai", text="Warn")],
                    ),
                    Block(
                        id="c",
                        order=4,
                        kind="code",
                        language="python",
                        source=[Inline(id="ci", text="print('x')", kind="code")],
                    ),
                    Block(
                        id="f",
                        order=5,
                        kind="formula",
                        source=[Inline(id="fi", text="x^2", kind="code")],
                    ),
                    Block(
                        id="t",
                        order=6,
                        kind="table",
                        caption=[Inline(id="tc", text="Table")],
                        cells=[
                            TableCell(
                                row=0,
                                column=0,
                                colspan=2,
                                header=True,
                                source=[Inline(id="cell", text="Value")],
                            )
                        ],
                    ),
                    Block(
                        id="p",
                        order=7,
                        kind="figure",
                        asset_path="assets/pic.png",
                        alt_text="Alt",
                        caption=[Inline(id="pc", text="Figure")],
                    ),
                    Block(
                        id="n",
                        order=8,
                        kind="footnote",
                        source=[Inline(id="ni", text="Footnote")],
                    ),
                    Block(id="r", order=9, kind="horizontal_rule"),
                    Block(
                        id="i",
                        order=10,
                        kind="paragraph",
                        source=[
                            Inline(
                                id="ii",
                                text="Link",
                                kind="link",
                                href="https://example.com",
                                marks=["underline", "superscript"],
                            )
                        ],
                    ),
                ],
            )
        ]
    )


def test_render_document_is_deterministic_and_complete() -> None:
    """全主要構造が同じMarkdownへ決定的に変換されることを確認する。

    Returns:
        なし。
    """

    document = _document()
    first = render_document(document, Path("cover.png"))
    assert first == render_document(document, Path("cover.png"))
    for value in (
        "## **Heading**",
        "[x] Task",
        "> Quote",
        "[!WARNING]",
        "```python",
        "<table>",
        "colspan",
        "![Alt]",
        "[^n]",
        "custom-style",
        'w:type="page"',
    ):
        assert value in first


def test_validate_document_checks_assets_and_control_characters(tmp_path: Path) -> None:
    """欠損assetと制御文字をblock ref付きで拒否することを確認する。

    Args:
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    document = _document()
    with pytest.raises(ValueError, match="p"):
        validate_document(document, tmp_path)
    asset = tmp_path / "assets" / "pic.png"
    asset.parent.mkdir()
    asset.write_bytes(b"png")
    document.pages[0].blocks[0].source[0].text = "bad\x01"
    with pytest.raises(ValueError, match="h"):
        validate_document(document, tmp_path)


def test_validate_document_rejects_duplicate_table_position(tmp_path: Path) -> None:
    """同じ座標を持つ表cellを不正shapeとして拒否することを確認する。

    Args:
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    document = Document(
        pages=[
            Page(
                number=2,
                blocks=[
                    Block(
                        id="table-ref",
                        order=0,
                        kind="table",
                        cells=[TableCell(row=0, column=0), TableCell(row=0, column=0)],
                    )
                ],
            )
        ]
    )
    with pytest.raises(ValueError, match="table-ref"):
        validate_document(document, tmp_path)


def test_render_code_chooses_a_safe_fence() -> None:
    """code本文内のbacktickより長いfenceを選ぶことを確認する。

    Returns:
        なし。
    """

    block = Block(
        id="code",
        order=0,
        kind="code",
        source=[Inline(id="code-inline", kind="code", text="```\n````")],
    )
    rendered = render_document(Document(pages=[Page(number=2, blocks=[block])]))
    assert rendered.startswith("`````\n")
    assert rendered.endswith("\n`````\n")


def test_validate_document_rejects_unknown_internal_link(tmp_path: Path) -> None:
    """存在しない見出しへの内部linkを拒否することを確認する。

    Args:
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    document = Document(
        pages=[
            Page(
                number=2,
                blocks=[
                    Block(
                        id="paragraph",
                        order=0,
                        kind="paragraph",
                        source=[
                            Inline(id="link", kind="link", text="参照", href="#missing")
                        ],
                    )
                ],
            )
        ]
    )
    with pytest.raises(ValueError, match="paragraph"):
        validate_document(document, tmp_path)
