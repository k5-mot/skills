"""PandocによるDOCX入出力とPDF表紙画像化を提供する。"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import pypdfium2 as pdfium


def check_pandoc() -> None:
    """v5が必要とするPandoc機能が存在するか検査する。

    Returns:
        なし。

    Raises:
        RuntimeError: Pandocまたは必要option/extensionがない場合。
    """

    executable = shutil.which("pandoc")
    if not executable:
        raise RuntimeError("pandoc is required")
    help_text = subprocess.run(
        [executable, "--help"], check=True, capture_output=True, text=True
    ).stdout
    missing = [
        option
        for option in ("--list-of-figures", "--list-of-tables", "--number-sections")
        if option not in help_text
    ]
    extensions = subprocess.run(
        [executable, "--list-extensions=docx"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if "native_numbering" not in extensions:
        missing.append("docx+native_numbering")
    if missing:
        raise RuntimeError(f"pandoc lacks required features: {', '.join(missing)}")


def create_docx(markdown: Path, output: Path, template: Path) -> None:
    """固定したPandoc契約でMarkdownからDOCXを生成する。

    Args:
        markdown: 内部Markdown。
        output: 最終DOCX。
        template: reference DOCX。

    Returns:
        なし。
    """

    check_pandoc()
    executable = shutil.which("pandoc") or "pandoc"
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent, prefix=f".{output.stem}.", suffix=".docx"
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        subprocess.run(
            [
                executable,
                str(markdown.resolve()),
                "--from",
                "markdown",
                "--to",
                "docx+native_numbering",
                "--standalone",
                "--reference-doc",
                str(template.resolve()),
                "--resource-path",
                str(markdown.parent.resolve()),
                "--toc",
                "--toc-depth",
                "6",
                "--list-of-figures",
                "--list-of-tables",
                "--number-sections",
                "--output",
                str(temporary.resolve()),
            ],
            check=True,
        )
        try:
            with zipfile.ZipFile(temporary) as archive:
                required = {"[Content_Types].xml", "word/document.xml"}
                if not required.issubset(archive.namelist()) or archive.testzip():
                    raise RuntimeError("Pandoc generated an invalid DOCX package")
        except zipfile.BadZipFile as error:
            raise RuntimeError("Pandoc generated an invalid DOCX package") from error
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def docx_to_text(path: Path) -> str:
    """PandocでDOCXを見出し付きplain textへ変換する。

    Args:
        path: 入力DOCX。

    Returns:
        UTF-8文字列。
    """

    check_pandoc()
    return subprocess.run(
        ["pandoc", str(path), "--to", "plain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def render_cover(source: Path, output: Path, dpi: int = 150) -> Path:
    """PDF第1ページを指定DPI相当のPNGへ変換する。

    Args:
        source: 入力PDF。
        output: PNG保存先。
        dpi: rasterizeする解像度。

    Returns:
        保存したPNG path。

    Raises:
        ValueError: PDFが空の場合。
    """

    output.parent.mkdir(parents=True, exist_ok=True)
    with pdfium.PdfDocument(source) as pdf:
        if len(pdf) == 0:
            raise ValueError("PDF has no pages")
        page = pdf[0]
        try:
            bitmap = page.render(scale=dpi / 72)
            try:
                bitmap.to_pil().save(output, format="PNG")
            finally:
                bitmap.close()
        finally:
            page.close()
    return output


def render_page(source: Path, page_number: int, output: Path, dpi: int = 120) -> Path:
    """PDFの指定ページをStructure用PNGへ変換する。

    Args:
        source: 入力PDF。
        page_number: 1始まりページ番号。
        output: PNG保存先。
        dpi: rasterizeする解像度。

    Returns:
        保存したPNG path。

    Raises:
        ValueError: ページ番号が範囲外の場合。
    """

    output.parent.mkdir(parents=True, exist_ok=True)
    with pdfium.PdfDocument(source) as pdf:
        if page_number < 1 or page_number > len(pdf):
            raise ValueError(f"PDF page is out of range: {page_number}")
        page = pdf[page_number - 1]
        try:
            bitmap = page.render(scale=dpi / 72)
            try:
                bitmap.to_pil().save(output, format="PNG")
            finally:
                bitmap.close()
        finally:
            page.close()
    return output


def pdf_pages_text(path: Path) -> list[str]:
    """PDFの各ページからtext layerを抽出する。

    Args:
        path: 入力PDF。

    Returns:
        ページ順のplain text列。
    """

    values: list[str] = []
    with pdfium.PdfDocument(path) as pdf:
        for index in range(len(pdf)):
            page = pdf[index]
            try:
                text_page = page.get_textpage()
                try:
                    values.append(text_page.get_text_range())
                finally:
                    text_page.close()
            finally:
                page.close()
    return values
