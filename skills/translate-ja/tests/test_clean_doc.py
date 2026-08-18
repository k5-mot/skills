"""clean_doc.py の単体テスト。"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from clean_doc import clean_docling_json  # noqa: E402


def test_clean_doc_reduces_excess_symbols_but_keeps_urls() -> None:
    """過剰記号は縮約し、URL 内の記号列は壊さない。"""

    data = {
        "texts": [
            {
                "self_ref": "#/texts/0",
                "label": "paragraph",
                "text": "Wait......  see https://example.com/a...b and ・・・・・・・・ now ----",
            }
        ]
    }
    cleaned, changes = clean_docling_json(data)
    text = cleaned["texts"][0]["text"]
    assert "Wait..." in text
    assert "https://example.com/a...b" in text
    assert "・・・" in text
    assert "----" not in text
    assert changes


def test_clean_doc_does_not_touch_code_nodes() -> None:
    """code label の text は成形対象から除外する。"""

    data = {
        "texts": [
            {"self_ref": "#/texts/0", "label": "code", "text": "x    =    '......'"}
        ]
    }
    cleaned, changes = clean_docling_json(data)
    assert cleaned["texts"][0]["text"] == "x    =    '......'"
    assert changes == []
