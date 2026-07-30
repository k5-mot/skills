"""chunk_docling_json.py の単体テスト。"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from chunk_docling_json import _format_pages, build_chunks  # noqa: E402


def test_build_chunks_keeps_heading_with_following_body() -> None:
    """見出しと直後の本文は同じ chunk に入る。"""

    data = {
        "texts": [
            {"self_ref": "#/texts/0", "label": "section_header", "level": 2, "text": "Strategy", "prov": [{"page_no": 1}]},
            {"self_ref": "#/texts/1", "label": "paragraph", "text": "The department will act.", "prov": [{"page_no": 1}]},
        ]
    }
    chunks = build_chunks(data, min_chars=10, max_chars=30)
    assert len(chunks) == 1
    assert chunks[0]["header_path"] == ["## Strategy"]
    assert chunks[0]["source_text"].startswith("## Strategy")
    assert "The department will act." in chunks[0]["source_text"]


def test_build_chunks_does_not_split_tables_over_max_chars() -> None:
    """表は max_chars を超えても 1 chunk として扱う。"""

    data = {
        "tables": [
            {
                "self_ref": "#/tables/0",
                "data": {
                    "grid": [
                        ["Term", "Meaning"],
                        ["DoD", "Department of Defense" * 20],
                    ]
                },
                "prov": [{"page_no": 2}],
            }
        ]
    }
    chunks = build_chunks(data, min_chars=10, max_chars=20)
    assert len(chunks) == 1
    assert chunks[0]["kind"] == "table"
    assert chunks[0]["source_text"].startswith("| Term | Meaning |")


def test_format_pages_compacts_page_ranges() -> None:
    """ログ用ページ表記は連続ページを範囲化する。"""

    assert _format_pages([1, 2, 3, 5]) == "1-3,5"
    assert _format_pages([]) == "unknown"
