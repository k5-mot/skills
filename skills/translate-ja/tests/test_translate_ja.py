"""translate_ja.py の単体テスト。"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from translate_ja import (  # noqa: E402
    build_translation_messages,
    load_dictionary_csv,
    select_glossary_entries,
)


def test_load_dictionary_csv_trims_headers_and_uses_last_duplicate(
    tmp_path: Path,
) -> None:
    """辞書 CSV は空白を除去し、同一 english は後勝ちにする。"""

    csv_path = tmp_path / "dictionary.csv"
    csv_path.write_text(
        '"english", "japanese", "genre", "description"\n'
        '"DoD", "国防省", "軍事用語", "古い訳"\n'
        '"DoD", "米国国防総省", "軍事用語", "米国の1省庁"\n',
        encoding="utf-8",
    )
    glossary = load_dictionary_csv(csv_path)
    assert glossary.raw_count == 2
    assert glossary.effective_count == 1
    assert glossary.entries[0].english == "DoD"
    assert glossary.entries[0].japanese == "米国国防総省"
    assert glossary.sha256


def test_build_translation_messages_includes_matching_glossary(tmp_path: Path) -> None:
    """チャンクに現れる用語が翻訳プロンプトに入る。"""

    csv_path = tmp_path / "dictionary.csv"
    csv_path.write_text(
        "english,japanese,genre,description\nDoD,米国国防総省,軍事用語,米国の1省庁\n",
        encoding="utf-8",
    )
    glossary = load_dictionary_csv(csv_path)
    entries = select_glossary_entries("The DoD will publish guidance.", glossary)
    messages = build_translation_messages(
        "The DoD will publish guidance.", glossary_entries=entries
    )
    assert "DoD => 米国国防総省" in messages[1]["content"]
