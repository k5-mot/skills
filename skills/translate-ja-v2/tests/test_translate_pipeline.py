"""translate-ja-v2 pipeline の純 Python 部分を検証する。"""

from __future__ import annotations

import json
import logging
import os
import sys
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pypdfium2 as pdfium
import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from translate import (  # noqa: E402
    apply_reorder_texts,
    apply_structure_patches,
    build_stage_paths,
    build_structure_messages,
    chat_text,
    CleanStage,
    clean_document,
    ColorFormatter,
    configure_logging,
    convert_markdown_to_docx,
    convert_with_docling,
    DoclingSettings,
    docling_form_payload,
    apply_review_results,
    extract_docling_zip,
    load_dotenv_file,
    main,
    message_text_chars,
    normalize_document,
    OpenAIEmptyResponseError,
    OPENAI_MAX_OUTPUT_TOKENS,
    OpenAISettings,
    pack_translation_blocks,
    page_image_path,
    PipelineOptions,
    poll_docling_task,
    read_json,
    read_glossary_csv,
    read_translation_rules,
    render_pdf_page_images,
    render_markdown,
    review_batch,
    review_document,
    ReviewStage,
    run_pipeline,
    stage_is_resumable,
    record_stage_completion,
    structure_document,
    StructureStage,
    translate_document,
    translate_batch,
    translate_text_item,
    TranslateStage,
    write_json,
)


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


class RetryableOpenAIError(Exception):
    """OpenAI SDK の retryable error 風の例外。"""

    status_code = 429


class FlakyCompletions:
    """初回だけ retryable error を返す fake completions。"""

    def __init__(self) -> None:
        """呼び出し回数を初期化する。"""

        self.calls = 0

    def create(self, **_kwargs):  # noqa: ANN001, ANN202
        """初回は 429 相当、2回目は成功応答を返す。"""

        self.calls += 1
        if self.calls == 1:
            raise RetryableOpenAIError("rate limited")
        return _completion("再試行後")


class FlakyClient:
    """retry test 用の最小 fake client。"""

    def __init__(self) -> None:
        """FlakyClient を初期化する。"""

        completions = FlakyCompletions()
        self.completions = completions
        self.chat = type("Chat", (), {"completions": completions})()


class RecordingCompletions:
    """request messages を記録する fake completions。"""

    def __init__(self) -> None:
        """記録領域を初期化する。"""

        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs):  # noqa: ANN001, ANN202
        """request を保存し、単体またはバッチ用の固定応答を返す。"""

        self.calls.append(kwargs)
        content = kwargs["messages"][-1]["content"]
        marker = "入力JSON:\n"
        if marker in content:
            items = json.loads(content.rsplit(marker, 1)[1])
            return _completion(
                json.dumps(
                    {
                        "translations": [
                            {"id": item["id"], "translated_text": "訳文"}
                            for item in items
                        ]
                    },
                    ensure_ascii=False,
                )
            )
        return _completion("訳文")


class RecordingClient:
    """request messages を検証するための fake client。"""

    def __init__(self) -> None:
        """RecordingClient を初期化する。"""

        completions = RecordingCompletions()
        self.completions = completions
        self.chat = type("Chat", (), {"completions": completions})()


class FakeHttpResponse:
    """httpx.Response の最小 fake。"""

    def __init__(
        self,
        status_code: int,
        payload: dict[str, Any] | None = None,
        content: bytes = b"",
    ) -> None:
        """fake response を初期化する。"""

        self.status_code = status_code
        self._payload = payload or {}
        self.content = content
        self.text = str(self._payload)

    def json(self) -> dict[str, Any]:
        """JSON payload を返す。"""

        return self._payload


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


def _text_item(
    index: int,
    text: str,
    *,
    top: int | None = None,
    bottom: int | None = None,
    page: int = 1,
    origin: str = "BOTTOMLEFT",
    label: str | None = None,
) -> dict[str, Any]:
    """bbox 付きの最小 Docling text item を作る。"""

    item: dict[str, Any] = {"self_ref": f"#/texts/{index}", "text": text}
    if label:
        item["label"] = label
    if top is not None and bottom is not None:
        item["prov"] = [
            {
                "page_no": page,
                "bbox": {
                    "l": 72,
                    "t": top,
                    "r": 200,
                    "b": bottom,
                    "coord_origin": origin,
                },
            }
        ]
    return item


def test_configure_logging_uses_debug_and_colored_levels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既定ログレベルと各レベル名のANSI色を検証する。

    Args:
        monkeypatch: logging設定を記録するpytest fixture。

    Returns:
        なし。
    """

    basic_config = Mock()
    app_logger = Mock()
    app_logger.handlers = []
    monkeypatch.setattr(logging, "basicConfig", basic_config)
    monkeypatch.setattr("translate.LOGGER", app_logger)

    configure_logging()

    kwargs = basic_config.call_args.kwargs
    assert kwargs["level"] == logging.WARNING
    assert kwargs["force"] is True
    assert kwargs["handlers"][0].level == logging.WARNING
    app_logger.setLevel.assert_called_once_with(logging.DEBUG)
    app_handler = app_logger.addHandler.call_args.args[0]
    assert app_handler.level == logging.DEBUG
    assert app_logger.propagate is False
    formatter = app_handler.formatter
    assert isinstance(formatter, ColorFormatter)
    expected_colors = {
        logging.DEBUG: "\033[36mDEBUG\033[0m",
        logging.INFO: "\033[32mINFO\033[0m",
        logging.WARNING: "\033[33mWARNING\033[0m",
        logging.ERROR: "\033[31mERROR\033[0m",
        logging.CRITICAL: "\033[35mCRITICAL\033[0m",
    }
    for level, colored_name in expected_colors.items():
        record = logging.LogRecord("test", level, __file__, 1, "message", (), None)
        assert colored_name in formatter.format(record)
        assert "\033[" not in record.levelname


def test_build_stage_paths_uses_input_stem_for_raw_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raw JSONは入力stem名、後続成果物はdocument固定名を使う。"""

    monkeypatch.chdir(tmp_path)

    paths = build_stage_paths(tmp_path / "sample.pdf", None, None)

    assert paths.output_dir == tmp_path / "outputs" / "sample"
    assert paths.document_json == paths.output_dir / "sample.json"
    assert paths.normalized_json == paths.output_dir / "document.normalized.json"
    assert paths.structured_json == paths.output_dir / "document.structured.json"
    assert paths.cleaned_json == paths.output_dir / "document.cleaned.json"
    assert paths.translated_json == paths.output_dir / "document.translated.json"
    assert paths.reviewed_json == paths.output_dir / "document.reviewed.json"
    assert paths.markdown == paths.output_dir / "document.ja.md"
    assert paths.docx == paths.output_dir / "document.ja.docx"


def test_stage_resume_rejects_corrupt_output_and_changed_config(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Resumeは成果物破損または設定変更を検出して再実行対象にする。

    Args:
        tmp_path: 一時成果物を保存するpytest fixture。
        caplog: ログを検証するpytest fixture。

    Returns:
        なし。
    """

    manifest = tmp_path / "manifest.json"
    output = tmp_path / "document.normalized.json"
    write_json(output, {"texts": []})
    caplog.set_level(logging.INFO, logger="translate-ja-v2")
    record_stage_completion(manifest, "normalize", output, "input", "config")

    assert "Stage completed stage=normalize" in caplog.text
    assert stage_is_resumable(manifest, "normalize", output, "input", "config")
    assert not stage_is_resumable(manifest, "normalize", output, "input", "changed")

    output.write_text("corrupt", encoding="utf-8")

    assert not stage_is_resumable(manifest, "normalize", output, "input", "config")


def test_apply_reorder_texts_moves_selected_refs_first() -> None:
    """reorder_texts patch は指定 ref 順に texts を並べ替える。"""

    data = {
        "body": {"children": [{"$ref": "#/texts/0"}, {"$ref": "#/texts/1"}]},
        "texts": [
            {"self_ref": "#/texts/0", "text": "Body"},
            {"self_ref": "#/texts/1", "text": "Heading"},
        ],
    }
    result = apply_reorder_texts(
        data, {"op": "reorder_texts", "refs": ["#/texts/1", "#/texts/0"]}
    )

    assert result["status"] == "success"
    assert [item["text"] for item in data["texts"]] == ["Heading", "Body"]
    assert [item["self_ref"] for item in data["texts"]] == [
        "#/texts/0",
        "#/texts/1",
    ]
    assert [child["$ref"] for child in data["body"]["children"]] == [
        "#/texts/0",
        "#/texts/1",
    ]


def test_apply_merge_texts_accepts_refs_in_reverse_document_order() -> None:
    """merge_textsはrefsが文書順と逆でも参照を安全に統合する。

    Returns:
        なし。
    """

    data = {
        "body": {"children": [{"$ref": "#/texts/0"}, {"$ref": "#/texts/1"}]},
        "texts": [
            {"self_ref": "#/texts/0", "text": "first"},
            {"self_ref": "#/texts/1", "text": "second"},
        ],
    }

    result, applied = apply_structure_patches(
        data,
        [
            {
                "op": "merge_texts",
                "refs": ["#/texts/1", "#/texts/0"],
                "text": "second first",
            }
        ],
    )

    assert result["texts"] == [{"self_ref": "#/texts/0", "text": "second first"}]
    assert result["body"]["children"] == [{"$ref": "#/texts/0"}]
    assert applied[0]["status"] == "success"


def test_normalize_document_does_not_change_text_or_table_cells() -> None:
    """Normalizeは座標補正以外の本文・表セル変更を行わない。

    Returns:
        なし。
    """

    data = {
        "texts": [{"self_ref": "#/texts/0", "label": "code", "text": "print('x')"}],
        "tables": [{"self_ref": "#/tables/0", "data": {"grid": [["A   B", "C...."]]}}],
    }
    normalized, patches = normalize_document(data)

    assert normalized == data
    assert patches == []


def test_clean_document_compacts_periods_and_middle_dots_outside_code() -> None:
    """Cleanは本文と表セルだけを校正し、コード内の記号を保持する。

    Returns:
        なし。
    """

    data = {
        "texts": [
            {
                "self_ref": "#/texts/0",
                "label": "paragraph",
                "text": "Wait...... 次・・・・・・ end..",
            },
            {
                "self_ref": "#/texts/1",
                "label": "code",
                "text": "print('......・・・・・・')",
            },
            {
                "self_ref": "#/texts/2",
                "label": "section_header",
                "text": "Title......",
            },
        ],
        "tables": [
            {
                "self_ref": "#/tables/0",
                "data": {
                    "grid": [
                        [
                            {
                                "text": "Use api....call()...... or・・・・・・",
                                "structure_ja_v2": {
                                    "inline_code_spans": ["api....call()"]
                                },
                            },
                            "Raw......",
                        ]
                    ]
                },
            }
        ],
    }

    cleaned, patches = clean_document(data)

    assert cleaned["texts"][0]["text"] == "Wait... 次・・・ end.."
    assert cleaned["texts"][1]["text"] == "print('......・・・・・・')"
    assert cleaned["texts"][2]["text"] == "Title......"
    cell = cleaned["tables"][0]["data"]["grid"][0][0]
    assert cell["text"] == "Use api....call()... or・・・"
    assert cleaned["tables"][0]["data"]["grid"][0][1] == "Raw..."
    assert [patch["ref"] for patch in patches] == [
        "#/texts/0",
        "#/tables/0/data/grid/0/0",
        "#/tables/0/data/grid/0/1",
    ]


def test_clean_stage_resumes_as_completed_stage(tmp_path: Path) -> None:
    """Cleanは要素別状態を持たず、完成成果物を工程単位で再利用する。

    Args:
        tmp_path: 一時成果物を保存するpytest fixture。

    Returns:
        なし。
    """

    paths = build_stage_paths(tmp_path / "source.pdf", tmp_path / "output", None)
    document = {
        "texts": [{"self_ref": "#/texts/0", "label": "paragraph", "text": "Wait......"}]
    }
    stage = CleanStage(paths=paths)

    first = stage.run(document)
    second = stage.run(document)

    assert first == second
    assert paths.cleaned_json.exists()
    manifest = read_json(paths.manifest)
    assert [event["stage"] for event in manifest["events"]] == ["clean"]
    assert "elements" not in manifest["stages"]["clean"]


@pytest.mark.parametrize(
    ("origin", "lower", "upper"),
    [("BOTTOMLEFT", (300, 280), (700, 680)), ("TOPLEFT", (500, 520), (100, 120))],
)
def test_normalize_document_orders_bbox_and_preserves_missing_slot(
    origin: str, lower: tuple[int, int], upper: tuple[int, int]
) -> None:
    """Normalize は座標原点に応じて読み順を補正する。"""

    data = {
        "texts": [
            _text_item(0, "Lower", top=lower[0], bottom=lower[1], origin=origin),
            _text_item(1, "No coordinates"),
            _text_item(2, "Upper", top=upper[0], bottom=upper[1], origin=origin),
        ]
    }

    normalized, _patches = normalize_document(data)

    assert [item["text"] for item in normalized["texts"]] == [
        "Upper",
        "No coordinates",
        "Lower",
    ]


def test_normalize_document_updates_refs_after_coordinate_reorder() -> None:
    """Normalize は座標並べ替え後も JSON 参照を保つ。"""

    data = {
        "body": {
            "children": [
                {"$ref": "#/texts/0"},
                {"$ref": "#/texts/1"},
                {"$ref": "#/texts/2"},
            ]
        },
        "groups": [{"children": [{"$ref": "#/texts/0"}, {"$ref": "#/texts/1"}]}],
        "links": [{"$ref": "#/texts/1"}],
        "texts": [
            _text_item(0, "Lower", top=300, bottom=280),
            _text_item(1, "Upper", top=700, bottom=680),
            _text_item(2, "Next page", top=700, bottom=680, page=2),
        ],
    }

    normalized, patches = normalize_document(data)

    assert [item["text"] for item in normalized["texts"]] == [
        "Upper",
        "Lower",
        "Next page",
    ]
    assert [item["self_ref"] for item in normalized["texts"]] == [
        "#/texts/0",
        "#/texts/1",
        "#/texts/2",
    ]
    assert [child["$ref"] for child in normalized["body"]["children"]] == [
        "#/texts/0",
        "#/texts/1",
        "#/texts/2",
    ]
    assert [child["$ref"] for child in normalized["groups"][0]["children"]] == [
        "#/texts/0",
        "#/texts/1",
    ]
    assert normalized["links"][0]["$ref"] == "#/texts/0"
    assert patches[0]["rule"] == "bbox_reading_order"
    assert patches[0]["after"] == ["#/texts/1", "#/texts/0", "#/texts/2"]


def test_structure_messages_include_coordinate_corrected_bbox() -> None:
    """VLM には座標補正後の順序と bbox を渡す。"""

    normalized, _patches = normalize_document(
        {
            "texts": [
                _text_item(0, "Lower", top=300, bottom=280, label="paragraph"),
                _text_item(1, "Upper", top=700, bottom=680, label="section_header"),
            ]
        }
    )
    content = build_structure_messages(normalized)[1]["content"]

    assert isinstance(content, str)
    assert "座標補正済みDocling要素" in content
    assert '"coordinate_order": 0' in content
    assert '"t": 700.0' in content
    assert content.index("Upper") < content.index("Lower")


def test_skip_vlm_keeps_coordinate_normalization() -> None:
    """--skip-vlm は第2段階だけを省略し、Normalize の座標補正は保つ。"""

    normalized, _patches = normalize_document(
        {
            "texts": [
                _text_item(0, "Lower", top=300, bottom=280),
                _text_item(1, "Upper", top=700, bottom=680),
            ]
        }
    )
    structured, structure_patches = structure_document(normalized, skip_vlm=True)

    assert [item["text"] for item in structured["texts"]] == ["Upper", "Lower"]
    assert structure_patches == []


def test_translate_text_item_renders_heading_bilingual_and_body_ja_only() -> None:
    """見出しは英日併記、本文は和訳のみを render_text に入れる。"""

    client = FakeClient()
    settings = OpenAISettings(
        base_url="http://example.test",
        api_key="test",
        model="fake",
        timeout_seconds=1,
    )
    heading: dict[str, Any] = {"label": "section_header", "text": "Strategy"}
    body: dict[str, Any] = {"label": "paragraph", "text": "The force moves."}

    translate_text_item(heading, client, settings)
    translate_text_item(body, client, settings)

    assert heading["translate_ja_v2"]["render_text"] == "Strategy / 戦略"
    assert body["translate_ja_v2"]["render_text"] == "部隊が移動する。"


def test_translate_document_passes_glossary_hits_and_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """翻訳対象に一致した用語集と翻訳ルールだけを LLM に渡す。"""

    glossary_path = tmp_path / "glossary.csv"
    glossary_path.write_text(
        (
            "english,japanese,desc,genre,note\n"
            "Strategic Command,戦略軍,Command organization,proper noun,keep English when needed\n"
            "Unmatched Term,未使用語,Not in source,term,\n"
        ),
        encoding="utf-8",
    )
    rules_path = tmp_path / "rules.md"
    rules_path.write_text("- 固有名詞は英語のまま和訳する。\n", encoding="utf-8")
    client = RecordingClient()
    monkeypatch.setattr(
        "translate.require_openai_settings",
        lambda context_chars: OpenAISettings(
            base_url="http://example.test",
            api_key="test",
            model="fake",
            timeout_seconds=1,
            context_chars=context_chars,
        ),
    )
    monkeypatch.setattr("translate.openai_client", lambda _settings: client)
    document = {
        "texts": [
            {
                "self_ref": "#/texts/0",
                "label": "paragraph",
                "text": "Strategic Command moves.",
            }
        ]
    }

    translated = translate_document(
        document,
        glossary=read_glossary_csv(glossary_path),
        translation_rules=read_translation_rules(rules_path),
    )
    prompt = client.completions.calls[0]["messages"][1]["content"]

    assert "Strategic Command" in prompt
    assert "Unmatched Term" not in prompt
    assert "固有名詞は英語のまま和訳する" in prompt
    assert client.completions.calls[0]["max_tokens"] == OPENAI_MAX_OUTPUT_TOKENS
    assert client.completions.calls[0]["response_format"] == {"type": "json_object"}
    assert translated["texts"][0]["translate_ja_v2"]["glossary_terms"] == [
        "Strategic Command"
    ]


def test_translate_resume_keeps_completed_heading_as_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Translate再開時も完了済み見出しを後続本文の文脈に使う。

    Args:
        monkeypatch: OpenAI clientをfakeへ置き換えるpytest fixture。

    Returns:
        なし。
    """

    client = RecordingClient()
    monkeypatch.setattr(
        "translate.require_openai_settings",
        lambda context_chars: OpenAISettings(
            base_url="http://example.test",
            api_key="test",
            model="fake",
            timeout_seconds=1,
            context_chars=context_chars,
        ),
    )
    monkeypatch.setattr("translate.openai_client", lambda _settings: client)
    source = {
        "texts": [
            {
                "self_ref": "#/texts/0",
                "label": "section_header",
                "text": "Strategy",
            },
            {
                "self_ref": "#/texts/1",
                "label": "paragraph",
                "text": "The force moves.",
            },
        ]
    }
    checkpoint = json.loads(json.dumps(source))
    checkpoint["texts"][0]["translate_ja_v2"] = {
        "text_en": "Strategy",
        "text_ja": "戦略",
        "render_text": "Strategy / 戦略",
        "translated": True,
    }

    translated = translate_document(
        source,
        resume_data=checkpoint,
        completed_ids={"#/texts/0"},
    )
    prompt = client.completions.calls[0]["messages"][1]["content"]

    assert '"context": "Strategy"' in prompt
    assert translated["texts"][0]["translate_ja_v2"]["text_ja"] == "戦略"
    assert translated["texts"][1]["translate_ja_v2"]["text_ja"] == "訳文"


def test_pack_translation_blocks_keeps_sections_within_limit() -> None:
    """見出しと本文のブロックを可能な限り保って上限内へ詰める。

    Returns:
        なし。
    """

    blocks = [
        [
            {"id": "h1", "text": "H" * 100},
            {"id": "p1", "text": "A" * 1000},
        ],
        [
            {"id": "h2", "text": "H" * 100},
            {"id": "p2", "text": "B" * 1801},
        ],
    ]

    batches = pack_translation_blocks(blocks, max_chars=3000)

    assert [[item["id"] for item in batch] for batch in batches] == [
        ["h1", "p1"],
        ["h2", "p2"],
    ]
    assert all(sum(len(item["text"]) for item in batch) <= 3000 for batch in batches)


def test_translate_document_batches_text_section_and_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本文群と1表をそれぞれ1回のLLM呼び出しで翻訳する。

    Args:
        monkeypatch: OpenAI client を fake に置き換える pytest fixture。

    Returns:
        なし。
    """

    client = RecordingClient()
    captured_blocks: list[list[list[str]]] = []
    captured_limits: list[int] = []

    def record_blocks(
        blocks: list[list[dict[str, Any]]], max_chars: int = 3000
    ) -> list[list[dict[str, Any]]]:
        """意味ブロックのID構成を記録して通常どおりバッチ化する。

        Args:
            blocks: 翻訳前の意味ブロック配列。
            max_chars: 1バッチの原文文字数上限。

        Returns:
            pack_translation_blocks が作る翻訳バッチ配列。

        Side Effects:
            captured_blocks に各ブロックのIDを追加する。
        """

        captured_blocks.append(
            [[str(item["id"]) for item in block] for block in blocks]
        )
        captured_limits.append(max_chars)
        return pack_translation_blocks(blocks, max_chars)

    monkeypatch.setattr(
        "translate.require_openai_settings",
        lambda *_args: OpenAISettings(
            base_url="http://example.test",
            api_key="test",
            model="fake",
            timeout_seconds=1,
        ),
    )
    monkeypatch.setattr("translate.openai_client", lambda _settings: client)
    monkeypatch.setattr("translate.pack_translation_blocks", record_blocks)
    document = {
        "texts": [
            {
                "self_ref": "#/texts/0",
                "label": "section_header",
                "level": 2,
                "text": "Strategy",
            },
            {
                "self_ref": "#/texts/1",
                "label": "section_header",
                "level": 3,
                "text": "Operations",
            },
            {
                "self_ref": "#/texts/2",
                "label": "paragraph",
                "text": "The force moves.",
            },
            {
                "self_ref": "#/texts/3",
                "label": "paragraph",
                "text": "The force stops.",
            },
            {
                "self_ref": "#/texts/4",
                "label": "section_header",
                "level": 2,
                "text": "Resources",
            },
            {
                "self_ref": "#/texts/5",
                "label": "paragraph",
                "text": "The reserve waits.",
            },
            {
                "self_ref": "#/texts/6",
                "label": "code",
                "text": "print('do not translate')",
            },
        ],
        "tables": [
            {
                "self_ref": "#/tables/0",
                "caption": "Terms",
                "data": {
                    "grid": [
                        [
                            {
                                "text": "Run api.call()",
                                "structure_ja_v2": {
                                    "inline_code_spans": ["api.call()"]
                                },
                            },
                            {"text": "DoD"},
                        ]
                    ]
                },
            }
        ],
    }

    translated = translate_document(document, batch_chars=3000)

    assert len(client.completions.calls) == 2
    assert captured_blocks[0] == [
        ["#/texts/0", "#/texts/1", "#/texts/2", "#/texts/3"],
        ["#/texts/4", "#/texts/5"],
    ]
    assert captured_limits == [3000, 3000]
    text_prompt = client.completions.calls[0]["messages"][1]["content"]
    table_prompt = client.completions.calls[1]["messages"][1]["content"]
    assert all(
        source in text_prompt
        for source in (
            "Strategy",
            "Operations",
            "The force moves.",
            "The force stops.",
            "Resources",
            "The reserve waits.",
        )
    )
    assert '"context": "Strategy > Operations"' in text_prompt
    assert "do not translate" not in text_prompt
    assert "Terms" in table_prompt
    assert "Run api.call()" in table_prompt
    assert '"inline_code_spans": ["api.call()"]' in table_prompt
    assert translated["texts"][6]["translate_ja_v2"] == {
        "kind": "code",
        "render_text": "print('do not translate')",
        "translated": False,
    }
    assert translated["tables"][0]["translate_ja_v2"]["caption_ja"] == "訳文"


def test_translate_batch_rejects_missing_response_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """バッチ応答に入力IDの欠落があれば翻訳結果を採用しない。

    Args:
        monkeypatch: LLM 応答を差し替える pytest fixture。

    Returns:
        なし。
    """

    monkeypatch.setattr(
        "translate.chat_text",
        lambda _client, _settings, _messages, **_kwargs: json.dumps(
            {"translations": [{"id": "a", "translated_text": "訳A"}]},
            ensure_ascii=False,
        ),
    )
    settings = OpenAISettings(
        base_url="http://example.test",
        api_key="test",
        model="fake",
        timeout_seconds=1,
    )

    with pytest.raises(ValueError, match="ids do not match"):
        translate_batch(
            object(),
            settings,
            [
                {"id": "a", "text": "A", "style": "本文"},
                {"id": "b", "text": "B", "style": "本文"},
            ],
        )


def test_translate_batch_accepts_top_level_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemma4 が返すトップレベル翻訳配列も受理する。"""

    monkeypatch.setattr(
        "translate.chat_text",
        lambda _client, _settings, _messages, **_kwargs: json.dumps(
            [{"id": "a", "translated_text": "訳A"}], ensure_ascii=False
        ),
    )
    settings = OpenAISettings(
        base_url="http://example.test",
        api_key="test",
        model="fake",
        timeout_seconds=1,
    )

    assert translate_batch(
        object(), settings, [{"id": "a", "text": "A", "style": "本文"}]
    ) == {"a": "訳A"}


def test_translate_batch_splits_partial_single_entry_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """複数入力に1件だけ返る部分応答は分割して取り直す。"""

    sizes: list[int] = []

    def fake_chat(
        _client: object,
        _settings: OpenAISettings,
        messages: list[dict[str, Any]],
        **_kwargs: object,
    ) -> str:
        items = json.loads(str(messages[-1]["content"]).rsplit("入力JSON:\n", 1)[1])
        sizes.append(len(items))
        return json.dumps(
            {"id": items[0]["id"], "translated_text": "訳文"},
            ensure_ascii=False,
        )

    monkeypatch.setattr("translate.chat_text", fake_chat)
    settings = OpenAISettings(
        base_url="http://example.test",
        api_key="test",
        model="fake",
        timeout_seconds=1,
    )
    items = [
        {"id": "a", "text": "A", "style": "本文"},
        {"id": "b", "text": "B", "style": "本文"},
    ]

    assert translate_batch(object(), settings, items) == {"a": "訳文", "b": "訳文"}
    assert sizes == [2, 1, 1]


def test_chat_text_retries_retryable_openai_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 などの一時エラーは固定された待機時間で再試行する。"""

    client = FlakyClient()
    settings = OpenAISettings(
        base_url="http://example.test",
        api_key="test",
        model="fake",
        timeout_seconds=1,
    )
    delays: list[float] = []
    monkeypatch.setattr("translate.time.sleep", delays.append)

    result = chat_text(client, settings, [{"role": "user", "content": "hello"}])

    assert result == "再試行後"
    assert client.completions.calls == 2
    assert delays == [5.0]


def test_chat_text_reports_empty_content() -> None:
    """HTTP 200でも本文が空なら専用例外を返す。"""

    completions = type(
        "EmptyCompletions",
        (),
        {"create": lambda _self, **_kwargs: _completion("")},
    )()
    client = type(
        "EmptyClient",
        (),
        {"chat": type("Chat", (), {"completions": completions})()},
    )()
    settings = OpenAISettings(
        base_url="http://example.test",
        api_key="test",
        model="fake",
        timeout_seconds=1,
    )
    with pytest.raises(OpenAIEmptyResponseError):
        chat_text(client, settings, [{"role": "user", "content": "hello"}])


def test_translate_batch_splits_after_empty_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空本文になった複数要素バッチは半分に分割する。"""

    sizes: list[int] = []

    def fake_chat(
        _client: object,
        _settings: OpenAISettings,
        messages: list[dict[str, Any]],
        **_kwargs: object,
    ) -> str:
        items = json.loads(str(messages[-1]["content"]).rsplit("入力JSON:\n", 1)[1])
        sizes.append(len(items))
        if len(items) > 1:
            raise OpenAIEmptyResponseError("empty")
        return json.dumps(
            [{"id": items[0]["id"], "translated_text": "訳文"}],
            ensure_ascii=False,
        )

    monkeypatch.setattr("translate.chat_text", fake_chat)
    settings = OpenAISettings(
        base_url="http://example.test",
        api_key="test",
        model="fake",
        timeout_seconds=1,
    )
    items = [
        {"id": "a", "text": "A", "style": "本文"},
        {"id": "b", "text": "B", "style": "本文"},
    ]

    assert translate_batch(object(), settings, items) == {"a": "訳文", "b": "訳文"}
    assert sizes == [2, 1, 1]


def test_review_document_checks_neighbor_consistency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """レビュー工程は前後要素を見て表記ゆれを補正する。"""

    calls: list[list[dict[str, Any]]] = []

    def fake_chat(
        _client: object,
        _settings: OpenAISettings,
        messages: list[dict[str, Any]],
        **_kwargs: object,
    ) -> str:
        calls.append(messages)
        return "防衛省"

    monkeypatch.setattr(
        "translate.require_openai_settings",
        lambda *_args: OpenAISettings(
            base_url="http://example.test",
            api_key="test",
            model="fake",
            timeout_seconds=1,
        ),
    )
    monkeypatch.setattr("translate.openai_client", lambda _settings: object())
    monkeypatch.setattr("translate.chat_text", fake_chat)
    document = {
        "texts": [
            {
                "self_ref": "#/texts/0",
                "label": "paragraph",
                "text": "Department of Defense",
                "translate_ja_v2": {
                    "kind": "body",
                    "text_en": "Department of Defense",
                    "text_ja": "国防省",
                    "render_text": "国防省",
                    "translated": True,
                },
            },
            {
                "self_ref": "#/texts/1",
                "label": "paragraph",
                "text": "Department of Defense",
                "translate_ja_v2": {
                    "kind": "body",
                    "text_en": "Department of Defense",
                    "text_ja": "防衛省",
                    "render_text": "防衛省",
                    "translated": True,
                },
            },
        ]
    }

    reviewed, changes = review_document(document)
    prompt = calls[0][-1]["content"]

    assert changes == 1
    assert reviewed["texts"][0]["translate_ja_v2"]["text_ja"] == "防衛省"
    assert reviewed["texts"][0]["translate_ja_v2"]["render_text"] == "防衛省"
    assert "次の日本語訳: 防衛省" in prompt
    assert "日本語表記が揺れていないか" in prompt


def test_review_batch_uses_plain_text_without_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """レビュー工程は1要素ずつ通常テキスト応答を受け取る。"""

    calls: list[tuple[list[dict[str, Any]], dict[str, object]]] = []

    def fake_chat(
        _client: object,
        _settings: OpenAISettings,
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> str:
        calls.append((messages, kwargs))
        return "訳A" if "原文: A" in str(messages[-1]["content"]) else "訳B"

    monkeypatch.setattr("translate.chat_text", fake_chat)
    settings = OpenAISettings(
        base_url="http://example.test",
        api_key="test",
        model="fake",
        timeout_seconds=1,
    )

    assert review_batch(
        object(),
        settings,
        [
            {
                "id": "a",
                "source_text": "A",
                "translated_text": "元訳A",
                "kind": "本文",
            },
            {
                "id": "b",
                "source_text": "B",
                "translated_text": "元訳B",
                "kind": "本文",
            },
        ],
    ) == {"a": "訳A", "b": "訳B"}
    assert len(calls) == 2
    assert all("json_response" not in kwargs for _messages, kwargs in calls)
    assert all("返却JSON" not in str(messages[-1]["content"]) for messages, _ in calls)


def test_review_batch_keeps_original_after_empty_single_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """単一要素のレビュー応答が空なら元訳を使う。"""

    monkeypatch.setattr(
        "translate.chat_text",
        lambda _client, _settings, _messages, **_kwargs: (_ for _ in ()).throw(
            OpenAIEmptyResponseError("empty")
        ),
    )
    settings = OpenAISettings(
        base_url="http://example.test",
        api_key="test",
        model="fake",
        timeout_seconds=1,
    )

    assert review_batch(
        object(),
        settings,
        [
            {
                "id": "#/texts/20",
                "source_text": "Department of Defense",
                "translated_text": "防衛省",
                "kind": "本文",
            }
        ],
    ) == {"#/texts/20": "防衛省"}


def test_apply_review_results_updates_bilingual_render_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """レビュー結果は見出しと表タイトルの英日併記表示を保つ。"""

    heading_meta = {"text_ja": "作戦", "render_text": "Strategy / 作戦"}
    caption_meta = {"caption_ja": "用語", "caption_render": "Terms / 用語"}
    caplog.set_level(logging.DEBUG, logger="translate-ja-v2")

    changes = apply_review_results(
        [
            {
                "id": "#/texts/0",
                "kind": "heading",
                "source_text": "Strategy",
                "meta": heading_meta,
                "text_field": "text_ja",
                "render_field": "render_text",
            },
            {
                "id": "#/tables/0/caption",
                "kind": "caption",
                "source_text": "Terms",
                "meta": caption_meta,
                "text_field": "caption_ja",
                "render_field": "caption_render",
            },
        ],
        {"#/texts/0": "戦略", "#/tables/0/caption": "用語集"},
    )

    assert changes == 2
    assert heading_meta["render_text"] == "Strategy / 戦略"
    assert caption_meta["caption_render"] == "Terms / 用語集"
    assert "Review changed id=#/texts/0 changed_chars=2" in caplog.text
    assert "Review changed id=#/tables/0/caption changed_chars=1" in caplog.text


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
                        [
                            {
                                "text": "Run api.call()",
                                "structure_ja_v2": {
                                    "inline_code_spans": ["api.call()"]
                                },
                                "translate_ja_v2": {"render_text": "api.call() を実行"},
                            }
                        ],
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
    assert "| `api.call()` を実行 |" in markdown


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


def test_apply_structure_patches_merge_reindexes_references_once() -> None:
    """merge 後の self_ref と body 参照は連鎖変換せず一意に保つ。"""

    data = {
        "texts": [
            {"self_ref": "#/texts/0", "label": "paragraph", "text": "A"},
            {"self_ref": "#/texts/1", "label": "paragraph", "text": "B"},
            {"self_ref": "#/texts/2", "label": "paragraph", "text": "C"},
        ],
        "body": {
            "children": [
                {"$ref": "#/texts/0"},
                {"$ref": "#/texts/1"},
                {"$ref": "#/texts/2"},
            ]
        },
    }

    patched, applied = apply_structure_patches(
        data,
        [{"op": "merge_texts", "refs": ["#/texts/0", "#/texts/1"]}],
    )

    assert applied[0]["status"] == "success"
    assert [item["self_ref"] for item in patched["texts"]] == [
        "#/texts/0",
        "#/texts/1",
    ]
    assert [child["$ref"] for child in patched["body"]["children"]] == [
        "#/texts/0",
        "#/texts/1",
    ]


def test_apply_structure_patches_converts_and_merges_code_fragments() -> None:
    """Structure patchは本文をcode化して隣接codeと連結する。

    Returns:
        なし。
    """

    data = {
        "texts": [
            {"self_ref": "#/texts/0", "label": "code", "text": "def run():"},
            {"self_ref": "#/texts/1", "label": "paragraph", "text": "    pass"},
        ]
    }

    patched, applied = apply_structure_patches(
        data,
        [
            {"op": "set_label", "ref": "#/texts/1", "label": "code"},
            {
                "op": "merge_texts",
                "refs": ["#/texts/0", "#/texts/1"],
                "text": "def run():\n    pass",
                "label": "code",
            },
        ],
    )

    assert all(result["status"] == "success" for result in applied)
    assert patched["texts"] == [
        {
            "self_ref": "#/texts/0",
            "label": "code",
            "text": "def run():\n    pass",
        }
    ]


def test_structure_marks_exact_inline_code_in_table_cell() -> None:
    """Structure patchは表セル原文に一致するinline code spanだけを保存する。

    Returns:
        なし。
    """

    data = {
        "tables": [
            {
                "self_ref": "#/tables/0",
                "data": {"grid": [[{"text": "Run api.call() now"}]]},
            }
        ]
    }

    patched, applied = apply_structure_patches(
        data,
        [
            {
                "op": "set_table_cell_inline_code",
                "ref": "#/tables/0/data/grid/0/0",
                "code_spans": ["api.call()", "missing()"],
            }
        ],
    )

    cell = patched["tables"][0]["data"]["grid"][0][0]
    assert applied[0]["status"] == "success"
    assert cell["structure_ja_v2"]["inline_code_spans"] == ["api.call()"]


def test_structure_messages_include_table_cells_for_inline_code_detection() -> None:
    """Structure requestは表セルrefと原文をVLMへ渡す。

    Returns:
        なし。
    """

    data = {
        "tables": [
            {
                "self_ref": "#/tables/0",
                "prov": [{"page_no": 1}],
                "data": {"grid": [[{"text": "Run api.call() now"}]]},
            }
        ]
    }

    content = build_structure_messages(data)[1]["content"]

    assert isinstance(content, str)
    assert "#/tables/0/data/grid/0/0" in content
    assert "Run api.call() now" in content
    assert "set_table_cell_inline_code" in content


def test_build_structure_messages_attaches_docling_page_png(tmp_path: Path) -> None:
    """構造補正 prompt は Docling JSON の URI が指す PNG を添付する。"""

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    (artifacts_dir / "page_000001.png").write_bytes(b"png-bytes")
    data = {
        "pages": {"1": {"image": {"uri": "artifacts/page_000001.png"}}},
        "texts": [
            {
                "self_ref": "#/texts/0",
                "label": "paragraph",
                "text": "Title",
                "prov": [{"page_no": 1}],
            }
        ],
    }

    messages = build_structure_messages(data, artifacts_dir)
    content = messages[1]["content"]

    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_page_image_path_matches_page_number(tmp_path: Path) -> None:
    """ページ画像はファイル名を推測せず Docling JSON の URI から解決する。"""

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    for page_no in range(1, 13):
        (artifacts_dir / f"page_{page_no:06d}_118.png").write_bytes(b"unrelated")
    expected = artifacts_dir / "page_000118_correct.png"
    expected.write_bytes(b"page118")
    data = {"pages": {"118": {"image": {"uri": "artifacts/page_000118_correct.png"}}}}

    assert page_image_path(data, artifacts_dir, 118) == expected


def test_build_structure_messages_does_not_fallback_to_unrelated_png(
    tmp_path: Path,
) -> None:
    """ページ URI がなければ無関係な artifact を VLM に添付しない。"""

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    (artifacts_dir / "page_000001.png").write_bytes(b"unrelated")
    data = {
        "texts": [
            {
                "self_ref": "#/texts/0",
                "label": "paragraph",
                "text": "Title",
                "prov": [{"page_no": 118}],
            }
        ]
    }

    content = build_structure_messages(data, artifacts_dir)[1]["content"]

    assert isinstance(content, str)


def test_structure_document_falls_back_to_pairwise_when_page_prompt_is_large(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ページ単位 prompt が大きい場合は隣接 merge と総当たり swap へ fallback する。"""

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    (artifacts_dir / "page_000001.png").write_bytes(b"png")
    data = {
        "pages": {"1": {"image": {"uri": "artifacts/page_000001.png"}}},
        "texts": [
            _text_item(
                index,
                f"fragment {index} " + ("x" * 300),
                top=700 - index,
                bottom=690 - index,
            )
            for index in range(8)
        ],
    }
    calls: list[list[dict[str, Any]]] = []

    def fake_chat(
        _client: object,
        _settings: OpenAISettings,
        messages: list[dict[str, Any]],
        **_kwargs: object,
    ) -> str:
        """pairwise request に固定 patch を返す。"""

        calls.append(messages)
        content = str(messages[1]["content"])
        if (
            "隣接する2つ" in content
            and "#/texts/0" in content
            and "#/texts/1" in content
        ):
            return json.dumps(
                {
                    "patches": [
                        {
                            "op": "merge_texts",
                            "refs": ["#/texts/0", "#/texts/1"],
                            "text": "merged fragment",
                            "reason": "split paragraph",
                        }
                    ]
                }
            )
        return json.dumps({"patches": []})

    monkeypatch.setattr(
        "translate.require_openai_settings",
        lambda context_chars: OpenAISettings(
            base_url="http://example.test",
            api_key="test",
            model="fake",
            timeout_seconds=1,
            context_chars=context_chars,
        ),
    )
    monkeypatch.setattr("translate.openai_client", lambda _settings: object())
    monkeypatch.setattr("translate.chat_text", fake_chat)

    structured, patches = structure_document(
        data,
        skip_vlm=False,
        artifacts_dir=artifacts_dir,
        context_chars=3800,
    )

    assert structured["texts"][0]["text"] == "merged fragment"
    assert any(patch["op"] == "merge_texts" for patch in patches)
    assert any("2つのDocling text要素" in str(call[1]["content"]) for call in calls)
    assert all(message_text_chars(call) <= 3800 for call in calls)


def test_load_dotenv_file_uses_python_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.env` は python-dotenv 経由で OpenAI/Docling 系環境変数を読み込む。"""

    env_path = tmp_path / ".env"
    env_path.write_text(
        "DOCLING_SERVER_URL=http://docling.test\nOPENAI_MODEL=test-model\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DOCLING_SERVER_URL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    load_dotenv_file(env_path)

    assert os.environ["DOCLING_SERVER_URL"] == "http://docling.test"
    assert os.environ["OPENAI_MODEL"] == "test-model"


def test_docling_payload_uses_fixed_ocr_settings() -> None:
    """Docling payload は固定された OCR 設定を使う。"""

    payload = docling_form_payload(120)

    assert payload["do_ocr"] == "false"
    assert payload["force_ocr"] == "false"
    assert payload["ocr_preset"] == "tesseract"
    assert payload["ocr_lang"] == ["jpn", "jpn_vert", "eng"]


def test_docling_payload_enables_document_enrichment() -> None:
    """表構造、セル対応、コード、数式認識を常に有効にする。"""

    payload = docling_form_payload(120)

    assert payload["do_table_structure"] == "true"
    assert payload["table_mode"] == "accurate"
    assert payload["table_cell_matching"] == "true"
    assert payload["do_code_enrichment"] == "true"
    assert payload["do_formula_enrichment"] == "true"


def test_docling_payload_disables_remote_page_images() -> None:
    """Doclingではページ画像を生成せず、画像scaleを1.0に固定する。"""

    payload = docling_form_payload(120)

    assert payload["include_images"] == "true"
    assert payload["include_page_images"] == "false"
    assert payload["images_scale"] == "1.0"


def test_convert_with_docling_uses_async_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docling 変換は async endpoint を使う。"""

    input_path = tmp_path / "source.pdf"
    output_json = tmp_path / "document.json"
    artifacts_dir = tmp_path / "artifacts"
    input_path.write_bytes(b"%PDF-1.4")
    endpoints: list[str] = []

    monkeypatch.setattr(
        "translate.require_docling_settings",
        lambda: DoclingSettings(
            server_url="http://docling.test",
            api_key="test",
            timeout_seconds=120,
        ),
    )

    def fake_request(
        endpoint: str,
        _input_path: Path,
        _settings: DoclingSettings,
        request_timeout: int,
    ) -> FakeHttpResponse:
        """Docling async convert request を記録する。"""

        endpoints.append(endpoint)
        assert request_timeout == 120
        return FakeHttpResponse(200, {"task_id": "task-1"})

    def fake_poll(task_id: str, output_zip: Path, _settings: DoclingSettings) -> None:
        """async poll の代わりに zip placeholder を保存する。"""

        assert task_id == "task-1"
        output_zip.write_bytes(b"zip")

    def fake_extract(
        zip_path: Path, target_json: Path, target_artifacts_dir: Path
    ) -> None:
        """Docling zip 展開の代わりに JSON を保存する。"""

        assert zip_path.read_bytes() == b"zip"
        target_artifacts_dir.mkdir(parents=True, exist_ok=True)
        write_json(target_json, {"texts": []})

    rendered: list[Path] = []

    def fake_render(
        source_path: Path, target_json: Path, target_artifacts_dir: Path
    ) -> None:
        """ローカルページ画像生成の呼び出しを記録する。"""

        assert target_json == output_json
        assert target_artifacts_dir == artifacts_dir
        rendered.append(source_path)

    monkeypatch.setattr("translate.request_docling_convert", fake_request)
    monkeypatch.setattr("translate.poll_docling_task", fake_poll)
    monkeypatch.setattr("translate.extract_docling_zip", fake_extract)
    monkeypatch.setattr("translate.render_pdf_page_images", fake_render)

    convert_with_docling(input_path, output_json, artifacts_dir)

    assert endpoints == ["http://docling.test/v1/convert/file/async"]
    assert rendered == [input_path]
    assert output_json.exists()


def test_render_pdf_page_images_updates_docling_uris(tmp_path: Path) -> None:
    """ローカル生成した各ページPNGのURIをDocling JSONへ保存する。"""

    input_path = tmp_path / "source.pdf"
    output_json = tmp_path / "document.json"
    artifacts_dir = tmp_path / "artifacts"
    with pdfium.PdfDocument.new() as pdf:
        pdf.new_page(200, 300)
        pdf.new_page(400, 500)
        pdf.save(input_path)
    write_json(
        output_json,
        {
            "pages": {
                "1": {"page_no": 1, "size": {"width": 200, "height": 300}},
                "2": {"page_no": 2, "size": {"width": 400, "height": 500}},
            }
        },
    )

    render_pdf_page_images(input_path, output_json, artifacts_dir)

    document = read_json(output_json)
    first = document["pages"]["1"]["image"]
    second = document["pages"]["2"]["image"]
    assert first == {
        "mimetype": "image/png",
        "dpi": 72,
        "size": {"width": 200.0, "height": 300.0},
        "uri": "artifacts/page_000001.png",
    }
    assert second["uri"] == "artifacts/page_000002.png"
    assert (
        (artifacts_dir / "page_000001.png")
        .read_bytes()
        .startswith(b"\x89PNG\r\n\x1a\n")
    )
    assert (artifacts_dir / "page_000002.png").is_file()


def test_poll_docling_task_logs_poll_count_each_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Docling async poll は poll ごとに回数と status を logging する。"""

    output_zip = tmp_path / "docling.zip"
    settings = DoclingSettings(
        server_url="http://docling.test",
        api_key="test",
        timeout_seconds=120,
    )
    status_payloads = [{"status": "processing"}, {"status": "success"}]

    class FakeHttpx:
        """poll と result download を返す httpx fake。"""

        @staticmethod
        def get(url: str, **_kwargs: Any) -> FakeHttpResponse:
            """URL に応じて status または result を返す。"""

            if "/v1/status/poll/" in url:
                return FakeHttpResponse(200, status_payloads.pop(0))
            if "/v1/result/" in url:
                return FakeHttpResponse(200, content=b"zip-bytes")
            raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setitem(sys.modules, "httpx", FakeHttpx)
    monkeypatch.setattr("translate.time.sleep", lambda _seconds: None)
    caplog.set_level(logging.DEBUG, logger="translate-ja-v2")

    poll_docling_task("task-1", output_zip, settings)

    assert output_zip.read_bytes() == b"zip-bytes"
    assert "poll_count=1 status=processing" in caplog.text
    assert "poll_count=2 status=success" in caplog.text


def test_extract_docling_zip_preserves_document_and_artifact_paths(
    tmp_path: Path,
) -> None:
    """Docling ZIPのartifact階層を保ち、以前の不要物を残さず展開する。

    Args:
        tmp_path: 一時ZIPと成果物を保存するpytest fixture。

    Returns:
        なし。
    """

    zip_path = tmp_path / "result.zip"
    output_json = tmp_path / "output" / "document.json"
    artifacts_dir = output_json.parent / "artifacts"
    artifacts_dir.mkdir(parents=True)
    (artifacts_dir / "stale.png").write_bytes(b"stale")
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("sample2.json", '{"texts": []}')
        archive.writestr("result/artifacts/pages/page_000001.png", b"page")

    extract_docling_zip(zip_path, output_json, artifacts_dir)

    assert read_json(output_json) == {"texts": []}
    assert (artifacts_dir / "pages" / "page_000001.png").read_bytes() == b"page"
    assert not (artifacts_dir / "stale.png").exists()


def test_main_returns_error_for_pipeline_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Typer CLI は pipeline の例外を終了コード1にする。"""

    input_path = tmp_path / "source.pdf"
    input_path.write_bytes(b"%PDF-1.4")

    def fail(_options: PipelineOptions) -> None:
        raise RuntimeError("test failure")

    monkeypatch.setattr("translate.run_pipeline", fail)

    assert main(["--input", str(input_path)]) == 1


def test_main_accepts_character_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Typer CLI は文字数上限を PipelineOptions へ渡す。"""

    input_path = tmp_path / "source.pdf"
    input_path.write_bytes(b"%PDF-1.4")
    captured: list[PipelineOptions] = []

    def capture(options: PipelineOptions) -> object:
        captured.append(options)
        return type(
            "Result", (), {"markdown": Path("out.md"), "docx": Path("out.docx")}
        )()

    monkeypatch.setattr("translate.run_pipeline", capture)

    assert (
        main(
            [
                "--input",
                str(input_path),
                "--context-chars",
                "32000",
                "--batch-chars",
                "800",
            ]
        )
        == 0
    )
    assert captured[0].context_chars == 32000
    assert captured[0].batch_chars == 800


def test_convert_markdown_to_docx_requires_pandoc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pandoc がない環境では明示エラーにする。"""

    markdown_path = tmp_path / "source.md"
    docx_path = tmp_path / "source.docx"
    monkeypatch.setattr("translate.shutil.which", lambda _name: None)

    with pytest.raises(RuntimeError, match="pandoc is required"):
        convert_markdown_to_docx(markdown_path, docx_path, None)


def test_convert_markdown_to_docx_runs_pandoc_from_markdown_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pandoc は Markdown 相対の artifacts 画像を解決できる cwd で実行する。"""

    markdown_path = tmp_path / "out" / "source.md"
    docx_path = tmp_path / "out" / "source.docx"
    template_path = tmp_path / "template.dotx"
    markdown_path.parent.mkdir()
    markdown_path.write_text("![image](artifacts/image.png)\n", encoding="utf-8")
    template_path.write_bytes(b"template")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("translate.shutil.which", lambda _name: "/usr/bin/pandoc")

    def fake_run(command: list[str], *, check: bool, cwd: Path) -> None:
        """subprocess.run の呼び出し内容を記録する。"""

        calls.append({"command": command, "check": check, "cwd": cwd})

    monkeypatch.setattr("translate.subprocess.run", fake_run)

    convert_markdown_to_docx(markdown_path, docx_path, template_path)

    assert calls == [
        {
            "command": [
                "pandoc",
                str(markdown_path.resolve()),
                "--from",
                "markdown",
                "--to",
                "docx",
                "--output",
                str(docx_path.resolve()),
                "--reference-doc",
                str(template_path.resolve()),
            ],
            "check": True,
            "cwd": markdown_path.resolve().parent,
        }
    ]


def test_structure_stage_resumes_from_completed_page_elements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Structureは完了text refを保持し、未完了ページから再開する。

    Args:
        tmp_path: 一時成果物を保存するpytest fixture。
        monkeypatch: Structure処理をfakeへ置き換えるpytest fixture。

    Returns:
        なし。
    """

    paths = build_stage_paths(tmp_path / "source.pdf", tmp_path / "out", None)
    paths.output_dir.mkdir(parents=True)
    document = {
        "texts": [
            _text_item(0, "page 1", top=100, bottom=90, page=1),
            _text_item(1, "page 2", top=100, bottom=90, page=2),
        ]
    }
    calls: list[int | None] = []
    fail_page_two = True
    monkeypatch.setattr(
        "translate.require_openai_settings",
        lambda context_chars: OpenAISettings(
            base_url="http://example.test",
            api_key="test",
            model="fake",
            timeout_seconds=1,
            context_chars=context_chars,
        ),
    )
    monkeypatch.setattr("translate.openai_client", lambda _settings: object())

    def fake_structure_page(
        data: dict[str, Any],
        page_no: int | None,
        _client: object,
        _settings: OpenAISettings,
        _artifacts_dir: Path | None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """ページ2の初回だけ失敗するStructure処理を模倣する。

        Args:
            data: 現在の部分成果物。
            page_no: 処理対象ページ番号。
            _client: fakeでは未使用のclient。
            _settings: fakeでは未使用の設定。
            _artifacts_dir: fakeでは未使用のartifactパス。

        Returns:
            処理済みページを記録した文書と空patch。
        """

        calls.append(page_no)
        if page_no == 2 and fail_page_two:
            raise RuntimeError("interrupted")
        result = json.loads(json.dumps(data))
        result.setdefault("processed_pages", []).append(page_no)
        return result, []

    monkeypatch.setattr("translate.structure_page_with_vlm", fake_structure_page)
    stage = StructureStage(
        paths=paths,
        artifacts_dir=paths.output_dir / "artifacts",
        skip_vlm=False,
    )

    with pytest.raises(RuntimeError, match="interrupted"):
        stage.run(document)

    manifest = read_json(paths.manifest)
    assert manifest["stages"]["structure"]["status"] == "running"
    assert manifest["stages"]["structure"]["elements"] == {
        "#/texts/0": {"status": "completed"},
        "#/texts/1": {"status": "pending"},
    }

    fail_page_two = False
    structured = stage.run(document)

    assert calls == [1, 2, 2]
    assert structured["processed_pages"] == [1, 2]
    assert read_json(paths.manifest)["stages"]["structure"]["status"] == "completed"


def test_translate_stage_resumes_from_completed_element(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Translateは完了text refを再送せず次の要素から再開する。

    Args:
        tmp_path: 一時成果物を保存するpytest fixture。
        monkeypatch: 翻訳APIをfakeへ置き換えるpytest fixture。

    Returns:
        なし。
    """

    paths = build_stage_paths(tmp_path / "source.pdf", tmp_path / "out", None)
    paths.output_dir.mkdir(parents=True)
    document = {
        "texts": [
            {"self_ref": "#/texts/0", "label": "section_header", "text": "A"},
            {"self_ref": "#/texts/1", "label": "section_header", "text": "B"},
        ]
    }
    calls: list[str] = []
    fail_second = True
    monkeypatch.setenv("OPENAI_MODEL", "fake")
    monkeypatch.setattr(
        "translate.require_openai_settings",
        lambda context_chars: OpenAISettings(
            base_url="http://example.test",
            api_key="test",
            model="fake",
            timeout_seconds=1,
            context_chars=context_chars,
        ),
    )
    monkeypatch.setattr("translate.openai_client", lambda _settings: object())

    def fake_chat(
        _client: object,
        _settings: OpenAISettings,
        messages: list[dict[str, Any]],
        **_kwargs: object,
    ) -> str:
        """2要素目の初回だけ失敗する翻訳応答を返す。

        Args:
            _client: fakeでは未使用のclient。
            _settings: fakeでは未使用の設定。
            messages: 翻訳対象IDを含むmessages。
            **_kwargs: fakeでは未使用の追加引数。

        Returns:
            入力IDを維持した翻訳JSON。
        """

        nonlocal fail_second
        content = str(messages[1]["content"])
        item_id = "#/texts/0" if "#/texts/0" in content else "#/texts/1"
        calls.append(item_id)
        if item_id == "#/texts/1" and fail_second:
            raise RuntimeError("interrupted")
        return json.dumps(
            {"translations": [{"id": item_id, "translated_text": f"訳{item_id[-1]}"}]}
        )

    monkeypatch.setattr("translate.chat_text", fake_chat)
    stage = TranslateStage(paths=paths, batch_chars=1)

    with pytest.raises(RuntimeError, match="interrupted"):
        stage.run(document)

    manifest = read_json(paths.manifest)
    assert manifest["stages"]["translate"]["elements"] == {
        "#/texts/0": {"status": "completed"},
        "#/texts/1": {"status": "pending"},
    }

    fail_second = False
    translated = stage.run(document)

    assert calls == ["#/texts/0", "#/texts/1", "#/texts/1"]
    assert translated["texts"][0]["translate_ja_v2"]["text_ja"] == "訳0"
    assert translated["texts"][1]["translate_ja_v2"]["text_ja"] == "訳1"


def test_review_stage_resumes_from_completed_element(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewは完了要素を再送せず次の対象から再開する。

    Args:
        tmp_path: 一時成果物を保存するpytest fixture。
        monkeypatch: Review処理をfakeへ置き換えるpytest fixture。

    Returns:
        なし。
    """

    paths = build_stage_paths(tmp_path / "source.pdf", tmp_path / "out", None)
    paths.output_dir.mkdir(parents=True)
    document = {
        "texts": [
            {
                "self_ref": f"#/texts/{index}",
                "label": "paragraph",
                "text": source,
                "translate_ja_v2": {
                    "kind": "body",
                    "text_en": source,
                    "text_ja": f"旧{index}",
                    "render_text": f"旧{index}",
                    "translated": True,
                },
            }
            for index, source in enumerate(("A", "B"))
        ]
    }
    calls: list[str] = []
    fail_second = True
    monkeypatch.setenv("OPENAI_MODEL", "fake")
    monkeypatch.setattr(
        "translate.require_openai_settings",
        lambda context_chars: OpenAISettings(
            base_url="http://example.test",
            api_key="test",
            model="fake",
            timeout_seconds=1,
            context_chars=context_chars,
        ),
    )
    monkeypatch.setattr("translate.openai_client", lambda _settings: object())

    def fake_review_batch(
        _client: object,
        _settings: OpenAISettings,
        items: list[dict[str, Any]],
        translation_rules: str,
    ) -> dict[str, str]:
        """2要素目の初回だけ失敗するレビュー応答を返す。

        Args:
            _client: fakeでは未使用のclient。
            _settings: fakeでは未使用の設定。
            items: 1件のレビュー対象。
            translation_rules: fakeでは未使用の翻訳ルール。

        Returns:
            対象IDとレビュー後訳文の対応。
        """

        _ = translation_rules
        item_id = str(items[0]["id"])
        calls.append(item_id)
        if item_id == "#/texts/1" and fail_second:
            raise RuntimeError("interrupted")
        return {item_id: f"新{item_id[-1]}"}

    monkeypatch.setattr("translate.review_batch", fake_review_batch)
    stage = ReviewStage(paths=paths)

    with pytest.raises(RuntimeError, match="interrupted"):
        stage.run(document)

    manifest = read_json(paths.manifest)
    assert manifest["stages"]["review"]["elements"] == {
        "#/texts/0": {"status": "completed"},
        "#/texts/1": {"status": "pending"},
    }

    fail_second = False
    reviewed = stage.run(document)

    assert calls == ["#/texts/0", "#/texts/1", "#/texts/1"]
    assert reviewed["texts"][0]["translate_ja_v2"]["text_ja"] == "新0"
    assert reviewed["texts"][1]["translate_ja_v2"]["text_ja"] == "新1"


def test_run_pipeline_writes_json_markdown_and_docx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pipeline は fake 外部依存で JSON、Markdown、docx を一通り生成する。"""

    input_path = tmp_path / "source.pdf"
    input_path.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "out"
    limits: dict[str, int] = {}
    stage_calls = {"parse": 0, "translate": 0, "review": 0, "docx": 0}

    def fake_docling(
        _input_path: Path,
        output_json: Path,
        artifacts_dir: Path,
    ) -> None:
        """Docling Serve の代わりに最小 Docling JSON と page PNG を保存する。

        Args:
            _input_path: 入力ファイル。
            output_json: Docling JSON 保存先。
            artifacts_dir: artifacts 保存先。

        Returns:
            なし。
        """

        stage_calls["parse"] += 1
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (artifacts_dir / "page_000001.png").write_bytes(b"png")
        write_json(
            output_json,
            {
                "texts": [
                    {
                        "self_ref": "#/texts/0",
                        "label": "section_header",
                        "level": 2,
                        "text": "Strategy",
                    },
                    {
                        "self_ref": "#/texts/1",
                        "label": "paragraph",
                        "text": "The force moves......",
                    },
                ]
            },
        )

    def fake_translate(
        data: dict[str, object],
        glossary: list[dict[str, str]] | None = None,
        translation_rules: str = "",
        context_chars: int = 0,
        batch_chars: int = 0,
        resume_data: dict[str, object] | None = None,
        completed_ids: set[str] | None = None,
        on_progress: object | None = None,
    ) -> dict[str, object]:
        """OpenAI 翻訳の代わりに render 用 metadata を追加する。

        Args:
            data: 構造補正済み JSON。
            glossary: fake では未使用。
            translation_rules: fake では未使用。
            context_chars: fakeで記録するcontext上限。
            batch_chars: fakeで記録するbatch上限。
            resume_data: fakeでは未使用の部分成果物。
            completed_ids: fakeでは未使用の完了ID。
            on_progress: fakeでは未使用のcallback。

        Returns:
            翻訳 metadata を追加した JSON。
        """

        _ = (glossary, translation_rules, resume_data, completed_ids, on_progress)
        stage_calls["translate"] += 1
        limits.update(context_chars=context_chars, batch_chars=batch_chars)
        copied = json.loads(json.dumps(data))
        assert copied["texts"][1]["text"] == "The force moves..."
        copied["texts"][0]["translate_ja_v2"] = {"render_text": "Strategy / 戦略"}
        copied["texts"][1]["translate_ja_v2"] = {"render_text": "部隊が移動する。"}
        return copied

    def fake_docx(
        markdown_path: Path, docx_path: Path, template_path: Path | None
    ) -> None:
        """pandoc の代わりに docx ファイルを作る。

        Args:
            markdown_path: 入力 Markdown。
            docx_path: 出力 docx。
            template_path: reference doc。

        Returns:
            なし。
        """

        stage_calls["docx"] += 1
        assert markdown_path.exists()
        assert template_path is None
        docx_path.write_bytes(b"docx")

    def fake_review(
        data: dict[str, object],
        translation_rules: str = "",
        context_chars: int = 0,
        resume_data: dict[str, object] | None = None,
        completed_ids: set[str] | None = None,
        on_progress: object | None = None,
    ) -> tuple[dict[str, object], int]:
        """OpenAI レビューの代わりに入力をそのまま返す。

        Args:
            data: レビュー対象JSON。
            translation_rules: fakeでは未使用の翻訳ルール。
            context_chars: fakeで記録するcontext上限。
            resume_data: fakeでは未使用の部分成果物。
            completed_ids: fakeでは未使用の完了ID。
            on_progress: fakeでは未使用のcallback。

        Returns:
            入力JSONと変更件数0。
        """

        _ = (translation_rules, resume_data, completed_ids, on_progress)
        stage_calls["review"] += 1
        limits["review_context_chars"] = context_chars
        return data, 0

    monkeypatch.setattr("translate.convert_with_docling", fake_docling)
    monkeypatch.setattr("translate.translate_document", fake_translate)
    monkeypatch.setattr("translate.review_document", fake_review)
    monkeypatch.setattr("translate.convert_markdown_to_docx", fake_docx)

    paths = run_pipeline(
        PipelineOptions(
            input=input_path,
            output_dir=output_dir,
            output=None,
            template=None,
            skip_vlm=True,
            skip_docx=False,
            force=True,
            context_chars=32000,
            batch_chars=800,
        )
    )

    assert paths.document_json == output_dir / "source.json"
    assert paths.document_json.exists()
    assert paths.normalized_json == output_dir / "document.normalized.json"
    assert paths.structured_json == output_dir / "document.structured.json"
    assert paths.cleaned_json == output_dir / "document.cleaned.json"
    assert paths.translated_json == output_dir / "document.translated.json"
    assert paths.reviewed_json == output_dir / "document.reviewed.json"
    assert paths.markdown == output_dir / "document.ja.md"
    assert paths.docx == output_dir / "document.ja.docx"
    assert paths.normalized_json.exists()
    assert paths.structured_json.exists()
    assert paths.cleaned_json.exists()
    assert paths.translated_json.exists()
    assert paths.reviewed_json.exists()
    assert paths.markdown.read_text(encoding="utf-8").startswith("## Strategy / 戦略")
    assert "部隊が移動する。" in paths.markdown.read_text(encoding="utf-8")
    assert paths.docx.read_bytes() == b"docx"
    assert limits == {
        "context_chars": 32000,
        "batch_chars": 800,
        "review_context_chars": 32000,
    }
    run_pipeline(
        PipelineOptions(
            input=input_path,
            output_dir=output_dir,
            skip_vlm=True,
            context_chars=32000,
            batch_chars=800,
        )
    )
    assert stage_calls == {"parse": 1, "translate": 1, "review": 1, "docx": 1}
    manifest = read_json(paths.manifest)
    assert [event["stage"] for event in manifest["events"]] == [
        "start",
        "parse",
        "normalize",
        "structure",
        "clean",
        "translate",
        "review",
        "markdown",
        "docx",
        "start",
    ]
    assert manifest["schema_version"] == 2
    assert set(manifest["stages"]) == {
        "parse",
        "normalize",
        "structure",
        "clean",
        "translate",
        "review",
        "markdown",
        "docx",
    }
    assert all(state["status"] == "completed" for state in manifest["stages"].values())
