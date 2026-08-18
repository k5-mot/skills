"""translate-ja-v2 pipeline の純 Python 部分を検証する。"""

from __future__ import annotations

import sys
import os
import zipfile
from pathlib import Path
from typing import Any

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from translate import (  # noqa: E402
    apply_reorder_texts,
    apply_structure_patches,
    build_structure_messages,
    chat_text,
    collect_page_image_paths,
    clean_text,
    convert_markdown_to_docx,
    docling_form_payload,
    env_bool,
    load_dotenv_file,
    main,
    normalize_document,
    OpenAISettings,
    PipelineOptions,
    read_json,
    render_markdown,
    run_pipeline,
    translate_text_item,
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


def test_chat_text_retries_retryable_openai_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 などの一時エラーは設定回数内で再試行する。"""

    client = FlakyClient()
    settings = OpenAISettings(
        base_url="http://example.test",
        api_key="test",
        model="fake",
        timeout_seconds=1,
    )
    monkeypatch.setenv("TRANSLATE_JA_V2_OPENAI_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("TRANSLATE_JA_V2_OPENAI_RETRY_INITIAL_SECONDS", "0")
    monkeypatch.setenv("TRANSLATE_JA_V2_OPENAI_RETRY_MAX_SECONDS", "0")

    result = chat_text(client, settings, [{"role": "user", "content": "hello"}])

    assert result == "再試行後"
    assert client.completions.calls == 2


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


def test_build_structure_messages_attaches_docling_page_png(tmp_path: Path) -> None:
    """構造補正 prompt は Docling のページ PNG を VLM content として添付する。"""

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    (artifacts_dir / "page_000001.png").write_bytes(b"png-bytes")
    data = {"texts": [{"self_ref": "#/texts/0", "label": "paragraph", "text": "Title"}]}

    messages = build_structure_messages(data, artifacts_dir)
    content = messages[1]["content"]

    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_collect_page_image_paths_prefers_page_png(tmp_path: Path) -> None:
    """VLM に添付する画像は page PNG を優先する。"""

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    figure = artifacts_dir / "image_000001.png"
    page = artifacts_dir / "page_000001.png"
    figure.write_bytes(b"figure")
    page.write_bytes(b"page")

    assert collect_page_image_paths(artifacts_dir) == [page]


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


def test_docling_payload_disables_ocr_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docling payload は OCR を既定で無効にし、必要時だけ言語を送る。"""

    monkeypatch.delenv("DOCLING_DO_OCR", raising=False)
    monkeypatch.delenv("DOCLING_FORCE_OCR", raising=False)

    payload = docling_form_payload(120)

    assert payload["do_ocr"] == "false"
    assert payload["force_ocr"] == "false"
    assert "ocr_lang" not in payload


def test_docling_payload_adds_ocr_languages_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OCR 有効時は DOCLING_OCR_LANGS の言語を Docling payload に入れる。"""

    monkeypatch.setenv("DOCLING_DO_OCR", "true")
    monkeypatch.setenv("DOCLING_OCR_LANGS", "eng,jpn")

    payload = docling_form_payload(120)

    assert payload["do_ocr"] == "true"
    assert payload["ocr_preset"] == "tesseract"
    assert payload["ocr_lang"] == ["eng", "jpn"]


def test_env_bool_parses_common_truthy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """env_bool は一般的な truthy 文字列を True として解釈する。"""

    monkeypatch.setenv("TRANSLATE_TEST_BOOL", "yes")

    assert env_bool("TRANSLATE_TEST_BOOL", default=False) is True
    assert env_bool("TRANSLATE_TEST_MISSING", default=True) is True


def test_main_returns_pipeline_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Typer CLI は pipeline の終了コードを main の戻り値へ伝播する。"""

    input_path = tmp_path / "source.pdf"
    input_path.write_bytes(b"%PDF-1.4")

    monkeypatch.setattr("translate.execute_pipeline", lambda _options: 7)

    assert main(["--input", str(input_path)]) == 7


def test_convert_markdown_to_docx_falls_back_without_pandoc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pandoc がない環境では標準ライブラリ fallback で docx を生成する。"""

    markdown_path = tmp_path / "source.md"
    docx_path = tmp_path / "source.docx"
    markdown_path.write_text("# Title\n\nBody & details\n", encoding="utf-8")
    monkeypatch.setattr("translate.shutil.which", lambda _name: None)

    convert_markdown_to_docx(markdown_path, docx_path, None)

    with zipfile.ZipFile(docx_path, "r") as archive:
        names = set(archive.namelist())
        document = archive.read("word/document.xml").decode("utf-8")
    assert "[Content_Types].xml" in names
    assert "_rels/.rels" in names
    assert "word/styles.xml" in names
    assert "Heading1" in document
    assert "Body &amp; details" in document


def test_run_pipeline_writes_json_markdown_and_docx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pipeline は fake 外部依存で JSON、Markdown、docx を一通り生成する。"""

    input_path = tmp_path / "source.pdf"
    input_path.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "out"

    def fake_docling(
        _input_path: Path,
        output_json: Path,
        artifacts_dir: Path,
        *,
        force_async: bool,
    ) -> None:
        """Docling Serve の代わりに最小 Docling JSON と page PNG を保存する。

        Args:
            _input_path: 入力ファイル。
            output_json: Docling JSON 保存先。
            artifacts_dir: artifacts 保存先。
            force_async: async 指定。

        Returns:
            なし。
        """

        assert force_async is False
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
                        "text": "The force moves.",
                    },
                ]
            },
        )

    def fake_translate(data: dict[str, object]) -> dict[str, object]:
        """OpenAI 翻訳の代わりに render 用 metadata を追加する。

        Args:
            data: 構造補正済み JSON。

        Returns:
            翻訳 metadata を追加した JSON。
        """

        copied = read_json(output_dir / "source.structured.json")
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

        assert markdown_path.exists()
        assert template_path is None
        docx_path.write_bytes(b"docx")

    monkeypatch.setattr("translate.convert_with_docling", fake_docling)
    monkeypatch.setattr("translate.translate_document", fake_translate)
    monkeypatch.setattr("translate.convert_markdown_to_docx", fake_docx)

    paths = run_pipeline(
        PipelineOptions(
            input=input_path,
            output_dir=output_dir,
            output=None,
            template=None,
            async_docling=False,
            skip_vlm=True,
            skip_docx=False,
            force=True,
        )
    )

    assert paths.docling_json.exists()
    assert paths.normalized_json.exists()
    assert paths.structured_json.exists()
    assert paths.translated_json.exists()
    assert paths.markdown.read_text(encoding="utf-8").startswith("## Strategy / 戦略")
    assert "部隊が移動する。" in paths.markdown.read_text(encoding="utf-8")
    assert paths.docx.read_bytes() == b"docx"
