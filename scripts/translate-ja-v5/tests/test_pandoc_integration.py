"""実Pandocが利用できる環境でDOCX packageを検証する。"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from src.adapters.pandoc import create_docx
from src.model import Block, Document, Inline, Page, TableCell
from src.processing.render import render_document
from src.state import atomic_write_text


def test_real_pandoc_creates_openable_structured_docx(tmp_path: Path) -> None:
    """実Pandocで表紙、見出し、表、図、脚注を持つDOCXを生成する。

    Args:
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    if shutil.which("pandoc") is None:
        pytest.skip("pandoc is not installed")
    cover = tmp_path / "cover.png"
    figure = tmp_path / "figure.png"
    Image.new("RGB", (600, 800), "white").save(cover)
    Image.new("RGB", (200, 100), "blue").save(figure)
    document = Document(
        pages=[
            Page(
                number=2,
                blocks=[
                    Block(
                        id="heading",
                        order=0,
                        kind="heading",
                        level=1,
                        reviewed=[Inline(id="heading-inline", text="見出し")],
                    ),
                    Block(
                        id="table",
                        order=1,
                        kind="table",
                        reviewed_caption=[Inline(id="table-caption", text="表題")],
                        cells=[
                            TableCell(
                                row=0,
                                column=0,
                                header=True,
                                reviewed=[Inline(id="cell", text="値")],
                            )
                        ],
                    ),
                    Block(
                        id="figure",
                        order=2,
                        kind="figure",
                        asset_path="figure.png",
                        alt_text="図",
                        reviewed_caption=[Inline(id="figure-caption", text="図題")],
                    ),
                    Block(
                        id="footnote",
                        order=3,
                        kind="footnote",
                        reviewed=[Inline(id="footnote-inline", text="脚注")],
                    ),
                ],
            )
        ]
    )
    markdown = tmp_path / "document.ja.md"
    output = tmp_path / "document.ja.docx"
    atomic_write_text(markdown, render_document(document, Path("cover.png")))
    template = Path(__file__).parents[1] / "templates" / "template.docx"
    create_docx(markdown, output, template)
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        xml = "".join(
            archive.read(name).decode("utf-8")
            for name in names
            if name.startswith("word/") and name.endswith(".xml")
        )
        assert "word/styles.xml" in names
        assert len([name for name in names if name.startswith("word/media/")]) >= 2
        assert all(value in xml for value in ("見出し", "表題", "図題", "脚注"))
