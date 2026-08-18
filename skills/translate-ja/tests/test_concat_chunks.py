"""concat_chunks.py の単体テスト。"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from concat_chunks import concat_chunks, validate_markdown  # noqa: E402


def test_concat_chunks_uses_translated_text_and_normalizes_typo() -> None:
    """翻訳済み本文を chunk_id 順で連結し、Chunks(JS) を補正する。"""

    markdown = concat_chunks(
        [
            {"chunk_id": "chunk-0002", "translated_text": "本文"},
            {"chunk_id": "chunk-0001", "translated_text": "# Chunks(JS)"},
        ]
    )
    assert markdown.startswith("# Chunks(JA)")
    assert markdown.endswith("\n")


def test_validate_markdown_reports_unclosed_code_fence() -> None:
    """閉じていない code fence は警告になる。"""

    assert validate_markdown("```python\nprint('x')\n") == [
        "fenced code block is not closed"
    ]
