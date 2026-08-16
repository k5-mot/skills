"""translate-ja-v2 pipeline の純 Python 部分を検証する。"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from translate import (  # noqa: E402
    apply_reorder_texts,
    apply_structure_patches,
    clean_text,
    normalize_document,
    render_markdown,
    translate_text_item,
)


class FakeSettings:
    """テスト用 OpenAI 設定を表す。

    Args:
        なし。

    Returns:
        chat_text に渡せる最小属性を持つ設定。
    """

    model = "fake"
    base_url = "http://example.test"
    api_key = "test"
    timeout_seconds = 1


class FakeCompletions:
    """翻訳 request に固定応答を返す fake completions。

    Args:
        なし。

    Returns:
        OpenAI SDK の completions 互換 object。
    """

    def create(self, **kwargs):  # noqa: ANN001, ANN202
        """Chat Completions create の最小 fake を返す。

        Args:
            **kwargs: OpenAI SDK と同じ呼び出し引数。

        Returns:
            choices[0].message.content を持つ object。
        """

        content = kwargs["messages"][-1]["content"]
        if "Strategy" in content:
            return _completion("戦略")
        if "The force moves." in content:
            return _completion("部隊が移動する。")
        return _completion("訳文")


class FakeClient:
    """OpenAI client の最小 fake を表す。

    Args:
        なし。

    Returns:
        chat.completions.create を持つ object。
    """

    def __init__(self) -> None:
        """FakeClient を初期化する。

        Args:
            なし。

        Returns:
            なし。
        """

        self.chat = type("Chat", (), {"completions": FakeCompletions()})()


def _completion(text: str):
    """OpenAI SDK 風の completion object を作る。

    Args:
        text: 応答本文。

    Returns:
        choices[0].message.content を持つ object。
    """

    message = type("Message", (), {"content": text})()
    choice = type("Choice", (), {"message": message})()
    return type("Completion", (), {"choices": [choice]})()


def test_clean_text_preserves_url_and_compacts_noise() -> None:
    """URL を壊さずに過剰な記号と空白を縮約する。"""

    text = "See   https://example.com/a---b .... ----"

    assert clean_text(text) == "See https://example.com/a---b ... ---"


def test_apply_reorder_texts_moves_selected_refs_first() -> None:
    """reorder_texts patch は指定 ref 順に texts を並べ替える。"""

    data = {
        "texts": [
            {"self_ref": "#/texts/0", "text": "Body"},
            {"self_ref": "#/texts/1", "text": "Heading"},
        ]
    }
    result = apply_reorder_texts(
        data, {"op": "reorder_texts", "refs": ["#/texts/1", "#/texts/0"]}
    )

    assert result["status"] == "success"
    assert [item["text"] for item in data["texts"]] == ["Heading", "Body"]


def test_normalize_document_marks_code_and_cleans_table_cells() -> None:
    """normalize はコードを翻訳対象外にし、表セルを整形する。"""

    data = {
        "texts": [{"self_ref": "#/texts/0", "label": "code", "text": "print('x')"}],
        "tables": [{"self_ref": "#/tables/0", "data": {"grid": [["A   B", "C...."]]}}],
    }
    normalized, patches = normalize_document(data)

    assert normalized["texts"][0]["translate_ja_v2"]["kind"] == "code"
    assert normalized["tables"][0]["data"]["grid"][0][0]["text"] == "A B"
    assert normalized["tables"][0]["data"]["grid"][0][1]["text"] == "C..."
    assert len(patches) == 2


def test_translate_text_item_renders_heading_bilingual_and_body_ja_only() -> None:
    """見出しは英日併記、本文は和訳のみを render_text に入れる。"""

    client = FakeClient()
    settings = FakeSettings()
    heading = {"label": "section_header", "text": "Strategy"}
    body = {"label": "paragraph", "text": "The force moves."}

    translate_text_item(heading, client, settings)  # type: ignore[arg-type]
    translate_text_item(body, client, settings)  # type: ignore[arg-type]

    assert heading["translate_ja_v2"]["render_text"] == "Strategy / 戦略"
    assert body["translate_ja_v2"]["render_text"] == "部隊が移動する。"


def test_render_markdown_uses_translated_json_fields() -> None:
    """renderer は JSON に付与された翻訳フィールドから Markdown を作る。"""

    data = {
        "texts": [
            {
                "self_ref": "#/texts/0",
                "label": "section_header",
                "level": 2,
                "text": "Strategy",
                "translate_ja_v2": {"render_text": "Strategy / 戦略"},
            },
            {
                "self_ref": "#/texts/1",
                "label": "paragraph",
                "text": "The force moves.",
                "translate_ja_v2": {"render_text": "部隊が移動する。"},
            },
        ],
        "tables": [
            {
                "self_ref": "#/tables/0",
                "caption": "Terms",
                "translate_ja_v2": {"caption_render": "Terms / 用語"},
                "data": {
                    "grid": [
                        [{"text": "Name", "translate_ja_v2": {"render_text": "名称"}}],
                        [{"text": "DoD", "translate_ja_v2": {"render_text": "DoD"}}],
                    ]
                },
            }
        ],
    }

    markdown = render_markdown(data)

    assert "## Strategy / 戦略" in markdown
    assert "部隊が移動する。" in markdown
    assert "**Terms / 用語**" in markdown
    assert "| 名称 |" in markdown


def test_apply_structure_patches_supports_label_level_and_reorder() -> None:
    """構造 patch は label、level、順序をまとめて適用できる。"""

    data = {
        "texts": [
            {"self_ref": "#/texts/0", "label": "paragraph", "text": "Body"},
            {"self_ref": "#/texts/1", "label": "paragraph", "text": "Title"},
        ]
    }
    patched, applied = apply_structure_patches(
        data,
        [
            {"op": "set_label", "ref": "#/texts/1", "label": "section_header"},
            {"op": "set_level", "ref": "#/texts/1", "level": 2},
            {"op": "reorder_texts", "refs": ["#/texts/1", "#/texts/0"]},
        ],
    )

    assert all(entry["status"] == "success" for entry in applied)
    assert patched["texts"][0]["text"] == "Title"
    assert patched["texts"][0]["label"] == "section_header"
    assert patched["texts"][0]["level"] == 2
