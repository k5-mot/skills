"""正規化済み文書の小さな内部モデルを定義する。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

InlineKind = Literal["text", "code", "link", "line_break"]
InlineMark = Literal[
    "strong",
    "emphasis",
    "strikethrough",
    "underline",
    "subscript",
    "superscript",
]
BlockKind = Literal[
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
AlertKind = Literal["note", "tip", "important", "warning", "caution"]


class Inline(BaseModel):
    """段落内の文字列と保護対象または装飾を表す。"""

    id: str
    text: str = ""
    kind: InlineKind = "text"
    marks: list[InlineMark] = Field(default_factory=list)
    href: str | None = None


class TableCell(BaseModel):
    """表内の一つのセルと翻訳状態を表す。"""

    row: int
    column: int
    rowspan: int = 1
    colspan: int = 1
    header: bool = False
    source: list[Inline] = Field(default_factory=list)
    translated: list[Inline] | None = None
    reviewed: list[Inline] | None = None


class Block(BaseModel):
    """ページ内の意味構造を持つ一つの要素を表す。"""

    id: str
    order: int
    kind: BlockKind
    bbox: tuple[float, float, float, float] | None = None
    source: list[Inline] = Field(default_factory=list)
    translated: list[Inline] | None = None
    reviewed: list[Inline] | None = None
    level: int | None = None
    ordered: bool = False
    checked: bool | None = None
    language: str | None = None
    alert_kind: AlertKind | None = None
    asset_path: str | None = None
    alt_text: str | None = None
    caption: list[Inline] = Field(default_factory=list)
    translated_caption: list[Inline] | None = None
    reviewed_caption: list[Inline] | None = None
    cells: list[TableCell] = Field(default_factory=list)


class Page(BaseModel):
    """PDFの一ページとその文書要素を表す。"""

    number: int
    width: float | None = None
    height: float | None = None
    blocks: list[Block] = Field(default_factory=list)


class Document(BaseModel):
    """翻訳工程が共有する正規化済み文書全体を表す。"""

    pages: list[Page] = Field(default_factory=list)


def inline_text(values: list[Inline]) -> str:
    """Inline列から検索やLLM入力に使うplain textを作る。

    Args:
        values: 連結するInline列。

    Returns:
        改行を含めて連結した文字列。
    """

    return "".join("\n" if item.kind == "line_break" else item.text for item in values)


def block_text(block: Block, reviewed: bool = True) -> str:
    """Blockから利用可能な最新のplain textを返す。

    Args:
        block: 対象Block。
        reviewed: Review済み文字列を優先するかどうか。

    Returns:
        Review済み、翻訳済み、原文の優先順で選んだ文字列。
    """

    values = (
        block.reviewed
        if reviewed and block.reviewed is not None
        else block.translated
        if block.translated is not None
        else block.source
    )
    return inline_text(values)


def page_text(page: Page, reviewed: bool = True) -> str:
    """ページ内Blockを文書順にplain textへ連結する。

    Args:
        page: 対象ページ。
        reviewed: Review済み文字列を優先するかどうか。

    Returns:
        空要素を除いて改行連結したページ本文。
    """

    return "\n".join(
        text
        for block in sorted(page.blocks, key=lambda item: item.order)
        if (text := block_text(block, reviewed))
    )
