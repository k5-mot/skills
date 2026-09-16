"""v5用語集の最小契約を検証する。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.processing.quality import (
    GlossaryEntry,
    deterministic_findings,
    matching_glossary,
    protected_fragments,
    read_glossary,
)


def test_glossary_expands_eight_column_short_and_long_terms(tmp_path: Path) -> None:
    """8列schemaのshort/long用語とmetadataを展開できることを確認する。

    Args:
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    path = tmp_path / "glossary.csv"
    path.write_text(
        "english-short,english-long,japanese-short,japanese-long,kind,description,note,reference\n"
        "API,Application Programming Interface,API,アプリケーションプログラミングインターフェース,technical term,Interface,Keep abbreviation,guide\n"
        ",reading order,,読み順,term,Order,,,\n",
        encoding="utf-8",
    )
    values = read_glossary(path)
    assert [(item.source, item.target) for item in values] == [
        ("API", "API"),
        (
            "Application Programming Interface",
            "アプリケーションプログラミングインターフェース",
        ),
        ("reading order", "読み順"),
    ]
    assert values[0].notes == (
        "kind: technical term; description: Interface; "
        "note: Keep abbreviation; reference: guide"
    )


@pytest.mark.parametrize(
    "content",
    [
        "source,target\nAPI,API\n",
        "english-short,english-long,japanese-short,japanese-long,kind,description,note,reference\n"
        ",,,,,,,\n",
        "english-short,english-long,japanese-short,japanese-long,kind,description,note,reference\n"
        "API,,API,,,,,\napi,,別訳,,,,,\n",
        "english-short,english-long,japanese-short,japanese-long,kind,description,note,reference\n"
        "API,,,,,,,,\n",
    ],
)
def test_glossary_rejects_invalid_schema_values_and_duplicates(
    content: str, tmp_path: Path
) -> None:
    """必須列不足、空値、大小文字違い重複を拒否することを確認する。

    Args:
        content: 不正なCSV本文。
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    path = tmp_path / "glossary.csv"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        read_glossary(path)


def test_matching_glossary_selects_only_terms_in_body() -> None:
    """本文に語境界付きで出現する用語だけを選ぶ。

    Returns:
        なし。
    """

    glossary = [
        GlossaryEntry(source="API", target="API"),
        GlossaryEntry(source="reading order", target="読み順"),
        GlossaryEntry(source="VLM", target="VLM"),
    ]
    assert [
        item.source
        for item in matching_glossary(
            "The api defines the reading   order. APIClient is separate.", glossary
        )
    ] == ["API", "reading order"]


def test_uppercase_heading_is_translatable_and_unchanged_english_is_rejected() -> None:
    """全大文字見出しを保護せず未翻訳のままなら指摘する。

    Returns:
        なし。
    """

    source = "U.S. DEFENSE INTERESTS IN THE ARCTIC"
    assert protected_fragments(source) == ["U.S"]
    assert "untranslated" in {
        item.category for item in deterministic_findings(source, source, [])
    }


def test_windows_paths_are_protected() -> None:
    """UNC、root相対、相対、空白付きWindows pathを保護する。

    Returns:
        なし。
    """

    source = (
        r"Use \\server\share\file.txt, \Windows\file.ini, folder\file.txt, and "
        r'"C:\Program Files\App\app.exe" or C:/Temp/app.exe.'
    )
    assert protected_fragments(source) == [
        r"\\server\share\file.txt",
        r"\Windows\file.ini",
        r"folder\file.txt",
        r'"C:\Program Files\App\app.exe"',
        "C:/Temp/app.exe.",
    ]
    assert protected_fragments("Compare input/output formats") == []
