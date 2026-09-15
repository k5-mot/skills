"""v5用語集の最小契約を検証する。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.processing.quality import (
    deterministic_findings,
    protected_fragments,
    read_glossary,
)


def test_glossary_accepts_source_target_and_optional_notes(tmp_path: Path) -> None:
    """二必須列と任意notes列をUTF-8で読めることを確認する。

    Args:
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    path = tmp_path / "glossary.csv"
    path.write_text(
        "source,target,notes\nAPI,APIインターフェース,指定訳\n", encoding="utf-8"
    )
    assert read_glossary(path)[0].target == "APIインターフェース"


@pytest.mark.parametrize(
    "content",
    [
        "english,japanese\nAPI,API\n",
        "source,target\nAPI,\n",
        "source,target\nAPI,一\napi,二\n",
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
