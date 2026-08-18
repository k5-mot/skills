"""realign_doc_struct_with_llm.py の純 Python 部分の単体テスト。"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from realign_doc_struct_with_llm import (  # noqa: E402
    _parse_patch_response,
    _stream_chat,
    apply_patches,
)


def test_apply_patches_sets_label_text_and_level() -> None:
    """補正パッチは Docling JSON の対象ノードに適用される。"""

    data = {"texts": [{"self_ref": "#/texts/0", "label": "paragraph", "text": "Title"}]}
    results = apply_patches(
        data,
        [
            {"op": "set_label", "ref": "#/texts/0", "label": "section_header"},
            {"op": "set_level", "ref": "#/texts/0", "level": 2},
            {"op": "set_text", "ref": "#/texts/0", "text": "Strategy"},
        ],
    )
    assert all(result["status"] == "success" for result in results)
    assert data["texts"][0] == {
        "self_ref": "#/texts/0",
        "label": "section_header",
        "text": "Strategy",
        "level": 2,
    }


def test_parse_patch_response_accepts_fenced_json() -> None:
    """LLM が JSON をコードフェンスで包んでも補正パッチを取り出せる。"""

    patches = _parse_patch_response(
        """```json
{"patches":[{"op":"set_label","ref":"#/texts/0","label":"section_header"}]}
```"""
    )

    assert patches == [
        {"op": "set_label", "ref": "#/texts/0", "label": "section_header"}
    ]


def test_parse_patch_response_accepts_preamble_before_json() -> None:
    """LLM が短い前置きを付けても patches object を抽出できる。"""

    patches = _parse_patch_response(
        """補正案です。
{"patches":[{"op":"set_level","ref":"#/texts/1","level":2}]}
"""
    )

    assert patches == [{"op": "set_level", "ref": "#/texts/1", "level": 2}]


def test_parse_patch_response_rejects_empty_response_with_clear_error() -> None:
    """空応答は JSONDecodeError ではなく原因が分かるエラーにする。"""

    with pytest.raises(ValueError, match="empty"):
        _parse_patch_response("")


def test_stream_chat_falls_back_to_non_stream_when_stream_has_no_content() -> None:
    """stream の delta.content が空なら非 stream で本文を取り直す。"""

    class FakeCompletions:
        def __init__(self) -> None:
            self.calls: list[bool] = []

        def create(self, **kwargs):  # noqa: ANN001, ANN202
            self.calls.append(bool(kwargs["stream"]))
            if kwargs["stream"]:
                return []
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(message=SimpleNamespace(content='{"patches":[]}'))
                ]
            )

    completions = FakeCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    settings = SimpleNamespace(model="test-model")

    response = _stream_chat(
        client, settings=settings, messages=[], headers={}, unit_id="page-0001"
    )

    assert response == '{"patches":[]}'
    assert completions.calls == [True, False]
