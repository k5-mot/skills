"""正規化済み文書を決定的にPandoc Markdownへ変換する。"""

from __future__ import annotations

import html
import re
from pathlib import Path

from src.model import Block, Document, Inline, TableCell, inline_text

CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _escape(text: str) -> str:
    """Markdown制御文字をplain textとしてescapeする。

    Args:
        text: 出力する文字列。

    Returns:
        Markdownで同じ文字として表示される文字列。
    """

    return re.sub(r"([\\`*{}\[\]()#+.!_|>~-])", r"\\\1", text)


def render_inlines(values: list[Inline]) -> str:
    """Inline列をPandoc Markdownへ変換する。

    Args:
        values: 文書順のInline列。

    Returns:
        Markdown inline文字列。
    """

    rendered: list[str] = []
    for item in values:
        if item.kind == "line_break":
            rendered.append("  \n")
            continue
        if item.kind == "code":
            value = f"`{item.text.replace('`', '``')}`"
        else:
            value = _escape(item.text)
        for mark in item.marks:
            if mark == "strong":
                value = f"**{value}**"
            elif mark == "emphasis":
                value = f"*{value}*"
            elif mark == "strikethrough":
                value = f"~~{value}~~"
            elif mark == "underline":
                value = f'[{value}]{{custom-style="Underline"}}'
            elif mark == "subscript":
                value = f"~{value}~"
            elif mark == "superscript":
                value = f"^{value}^"
        if item.kind == "link":
            value = f"[{value}]({_escape(item.href or '')})"
        rendered.append(value)
    return "".join(rendered)


def _current(block: Block) -> list[Inline]:
    """Blockの最終出力に使うInline列を選ぶ。

    Args:
        block: 対象Block。

    Returns:
        Review済み、翻訳済み、原文の優先順で選んだInline列。
    """

    return (
        block.reviewed
        if block.reviewed is not None
        else block.translated
        if block.translated is not None
        else block.source
    )


def _cell_current(cell: TableCell) -> list[Inline]:
    """TableCellの最終出力に使うInline列を選ぶ。

    Args:
        cell: 対象cell。

    Returns:
        Review済み、翻訳済み、原文の優先順で選んだInline列。
    """

    return (
        cell.reviewed
        if cell.reviewed is not None
        else cell.translated
        if cell.translated is not None
        else cell.source
    )


def _caption_current(block: Block) -> list[Inline]:
    """図表題の最終出力に使うInline列を選ぶ。

    Args:
        block: 図または表Block。

    Returns:
        Review済み、翻訳済み、原文の優先順で選んだInline列。
    """

    return (
        block.reviewed_caption
        if block.reviewed_caption is not None
        else block.translated_caption
        if block.translated_caption is not None
        else block.caption
    )


def _render_table(block: Block) -> str:
    """Table Blockをrowspan対応HTML tableへ変換する。

    Args:
        block: table種別のBlock。

    Returns:
        Pandocが読めるHTML table。
    """

    rows: dict[int, list[TableCell]] = {}
    for cell in block.cells:
        rows.setdefault(cell.row, []).append(cell)
    parts = ["<table>"]
    caption = _caption_current(block)
    if caption:
        parts.append(f"<caption>{html.escape(inline_text(caption))}</caption>")
    for row in sorted(rows):
        parts.append("<tr>")
        for cell in sorted(rows[row], key=lambda value: value.column):
            tag = "th" if cell.header else "td"
            attributes = ""
            if cell.rowspan > 1:
                attributes += f' rowspan="{cell.rowspan}"'
            if cell.colspan > 1:
                attributes += f' colspan="{cell.colspan}"'
            parts.append(
                f"<{tag}{attributes}>{html.escape(inline_text(_cell_current(cell)))}</{tag}>"
            )
        parts.append("</tr>")
    parts.append("</table>")
    return "\n".join(parts)


def render_block(block: Block) -> str:
    """一つのBlockをPandoc Markdownへ変換する。

    Args:
        block: 対象Block。

    Returns:
        Blockに対応するMarkdown断片。
    """

    text = render_inlines(_current(block))
    if block.kind == "heading":
        return (
            f"{'#' * max(1, min(6, block.level or 1))} {text} {{#{_anchor(block.id)}}}"
        )
    if block.kind == "list_item":
        marker = "1." if block.ordered else "-"
        task = "" if block.checked is None else f"[{'x' if block.checked else ' '}] "
        return f"{'  ' * max(0, (block.level or 1) - 1)}{marker} {task}{text}"
    if block.kind == "blockquote":
        return "\n".join(f"> {line}" for line in text.splitlines())
    if block.kind == "alert":
        label = (block.alert_kind or "note").upper()
        return f"> [!{label}]\n" + "\n".join(f"> {line}" for line in text.splitlines())
    if block.kind == "code":
        code = inline_text(_current(block))
        longest_run = max((len(value) for value in re.findall(r"`+", code)), default=0)
        fence = "`" * max(3, longest_run + 1)
        return f"{fence}{block.language or ''}\n{code}\n{fence}"
    if block.kind == "formula":
        return f"$$\n{inline_text(_current(block))}\n$$"
    if block.kind == "table":
        return _render_table(block)
    if block.kind == "figure":
        caption = render_inlines(_caption_current(block))
        return f"![{_escape(block.alt_text or '')}]({_escape(block.asset_path or '')})\n\n{caption}"
    if block.kind == "footnote":
        anchor = _anchor(block.id)
        return f"[^{anchor}]\n\n[^{anchor}]: {text}"
    if block.kind == "horizontal_rule":
        return "---"
    return text


def _anchor(value: str) -> str:
    """任意のrefから安全なMarkdown anchorを作る。

    Args:
        value: Docling ref等の安定ID。

    Returns:
        英数字、underscore、hyphenだけのanchor。
    """

    return re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-") or "item"


def validate_document(document: Document, asset_root: Path) -> None:
    """Rendererが黙って壊れた出力を作らないよう文書を検証する。

    Args:
        document: 検証する文書。
        asset_root: 相対assetの基準directory。

    Returns:
        なし。

    Raises:
        ValueError: 制御文字、欠損asset、表shapeが不正な場合。
    """

    anchors = {
        _anchor(block.id)
        for page in document.pages
        for block in page.blocks
        if block.kind == "heading"
    }
    for page in document.pages:
        for block in page.blocks:
            text = inline_text(_current(block))
            if CONTROL_RE.search(text):
                raise ValueError(f"unsupported control character: {block.id}")
            if block.kind == "figure":
                if not block.asset_path or not (asset_root / block.asset_path).exists():
                    raise ValueError(f"unresolved figure asset: {block.id}")
            if block.kind == "table":
                positions: set[tuple[int, int]] = set()
                for cell in block.cells:
                    position = (cell.row, cell.column)
                    if position in positions or min(cell.rowspan, cell.colspan) < 1:
                        raise ValueError(f"invalid table shape: {block.id}")
                    positions.add(position)
            inline_groups = [_current(block), _caption_current(block)]
            if block.kind == "table":
                inline_groups.extend(_cell_current(cell) for cell in block.cells)
            for item in (item for group in inline_groups for item in group):
                if item.kind == "link" and item.href and item.href.startswith("#"):
                    if item.href[1:] not in anchors:
                        raise ValueError(f"unresolved internal reference: {block.id}")


def render_document(document: Document, cover_path: Path | None = None) -> str:
    """文書全体を表紙付きPandoc Markdownへ変換する。

    Args:
        document: 変換する正規化済み文書。
        cover_path: 任意の表紙画像path。

    Returns:
        UTF-8 Markdown文字列。
    """

    parts: list[str] = []
    if cover_path is not None:
        parts.extend(
            [
                f"![表紙]({_escape(cover_path.as_posix())}){{width=100%}}",
                "```{=openxml}",
                '<w:p><w:r><w:br w:type="page"/></w:r></w:p>',
                "```",
            ]
        )
    for page in document.pages:
        for block in sorted(page.blocks, key=lambda value: value.order):
            parts.append(render_block(block))
    return "\n\n".join(part for part in parts if part) + "\n"
