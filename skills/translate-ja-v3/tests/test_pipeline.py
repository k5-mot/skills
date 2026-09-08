"""translate-ja-v3の決定論的な契約を検証する。"""

from __future__ import annotations

import copy
import logging
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pytest

from translate_ja_v3.config import PipelineOptions, build_paths, initial_state
from translate_ja_v3.document import batches, resolve_target, translation_targets
from translate_ja_v3.graph import build_graph
from translate_ja_v3.io import (
    ColorFormatter,
    glossary_matches,
    hash_file,
    hash_json,
    read_glossary,
    record_stage,
    configure_logging,
    stage_cached,
    write_json,
)
from translate_ja_v3.stages.clean import clean
from translate_ja_v3.stages.docx import _fix_heading_spacing
from translate_ja_v3.stages.markdown import markdown
from translate_ja_v3.stages.normalize import normalize
from translate_ja_v3.stages.parse import _merge, _remap
from translate_ja_v3.stages.review import (
    ReviewResponse,
    _review_graph,
    _run_with_fallback,
)
from translate_ja_v3.stages.structure import (
    StructurePatch,
    _apply,
    _page_image,
    _request_with_fallback,
)
from translate_ja_v3.stages.translate import Translator, _translate_with_fallback


def _document() -> dict[str, Any]:
    """複数Stageで共有する最小Docling文書を返す。

    Returns:
        textとtableを含むtest文書。
    """

    return {
        "texts": [
            {
                "self_ref": "#/texts/0",
                "label": "section_header",
                "level": 2,
                "text": "Overview",
                "prov": [{"page_no": 1, "bbox": {"t": 90, "b": 80, "l": 10}}],
            },
            {
                "self_ref": "#/texts/1",
                "label": "paragraph",
                "text": "Use the API......",
                "prov": [{"page_no": 1, "bbox": {"t": 70, "b": 60, "l": 10}}],
            },
            {"self_ref": "#/texts/2", "label": "code", "text": "print('x')......"},
        ],
        "tables": [
            {
                "self_ref": "#/tables/0",
                "caption": "API table",
                "prov": [{"page_no": 1}],
                "data": {
                    "grid": [
                        [
                            {
                                "text": "Call api()・・・・・・",
                                "structure_ja_v3": {
                                    "inline_code_spans": ["api()・・・・・・"]
                                },
                            }
                        ]
                    ]
                },
            }
        ],
        "pictures": [],
        "pages": {"1": {}},
    }


def test_build_paths_uses_input_stem(tmp_path: Path) -> None:
    """Parse成果物だけ入力stem名になり、後続名が固定されることを確認する。

    Args:
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    paths = build_paths(PipelineOptions(input=tmp_path / "source.pdf"))
    assert paths.document_json.name == "source.json"
    assert paths.normalized_json.name == "document.normalized.json"
    assert paths.cleaned_json.name == "document.cleaned.json"
    assert paths.checkpoints.name == ".langgraph.sqlite3"


def test_glossary_schema_and_prompt_filter(tmp_path: Path) -> None:
    """8列用語集を英語列で検索し内部列を除外することを確認する。

    Args:
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    path = tmp_path / "glossary.csv"
    path.write_text(
        "english-short,english-long,japanse-short,japanese-long,kind,description,note,reference\n"
        "API,Application Programming Interface,API,API,term,interface,secret note,source URL\n",
        encoding="utf-8",
    )
    rows = read_glossary(path)
    match = glossary_matches("Application Programming Interface", rows)
    assert len(match) == 1
    assert "note" not in match[0]
    assert "reference" not in match[0]


def test_glossary_requires_reference_column(tmp_path: Path) -> None:
    """旧7列schemaを拒否することを確認する。

    Args:
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    path = tmp_path / "old.csv"
    path.write_text(
        "english-short,english-long,japanse-short,japanese-long,kind,description,note\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="reference"):
        read_glossary(path)


def test_color_formatter_colors_only_level_name() -> None:
    """ANSI色がlevel名だけに付くことを確認する。

    Returns:
        なし。
    """

    formatter = ColorFormatter("%(levelname)s %(message)s")
    record = logging.LogRecord("test", logging.INFO, "", 1, "plain body", (), None)
    rendered = formatter.format(record)
    assert rendered.startswith("\x1b[32mINFO\x1b[0m ")
    assert rendered.endswith("plain body")
    assert "\x1b" not in rendered.split(" ", 1)[1]


def test_logging_suppresses_transport_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    """既定DEBUGでもHTTP transportの内部logを抑えることを確認する。

    Args:
        monkeypatch: pytest monkeypatch fixture。

    Returns:
        なし。
    """

    monkeypatch.delenv("LOG_LEVEL", raising=False)
    configure_logging()
    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("httpcore").level == logging.WARNING
    assert logging.getLogger("httpx").level == logging.WARNING


def test_batches_obey_both_limits() -> None:
    """文字数と要素数の小さい側でbatchが分かれることを確認する。

    Returns:
        なし。
    """

    items = [{"source": value} for value in ("1234", "5678", "90")]
    assert [len(batch) for batch in batches(items, 8, 0)] == [2, 1]
    assert [len(batch) for batch in batches(items, 100, 1)] == [1, 1, 1]


def test_translation_targets_include_text_caption_and_cell() -> None:
    """共通走査がcodeを除外し3種類の翻訳対象を返すことを確認する。

    Returns:
        なし。
    """

    targets = translation_targets(_document())
    assert [item["id"] for item in targets] == [
        "#/texts/0",
        "#/texts/1",
        "#/tables/0/caption",
        "#/tables/0/data/grid/0/0",
    ]
    assert resolve_target(_document(), targets[-1]["path"])["text"].startswith("Call")


def test_normalize_sorts_located_texts_and_rewrites_refs() -> None:
    """座標順の並べ替えと参照更新を確認する。

    Returns:
        なし。
    """

    document = _document()
    document["texts"][0]["prov"][0]["bbox"]["t"] = 10
    document["texts"][1]["prov"][0]["bbox"]["t"] = 90
    document["body"] = {"children": [{"$ref": "#/texts/1"}]}
    result = normalize(document)
    assert result["texts"][0]["text"].startswith("Use")
    assert result["texts"][0]["self_ref"] == "#/texts/0"
    assert result["body"]["children"][0]["$ref"] == "#/texts/0"


def test_clean_preserves_code_and_inline_code() -> None:
    """記号校正が本文だけに作用し保護spanを維持することを確認する。

    Returns:
        なし。
    """

    result = clean(_document())
    assert result["texts"][1]["text"].endswith("...")
    assert result["texts"][2]["text"].endswith("......")
    assert result["tables"][0]["data"]["grid"][0][0]["text"].endswith("・・・・・・")


def test_parse_remap_updates_ref_page_and_uri() -> None:
    """chunk内参照、ページ、artifact URIのoffset適用を確認する。

    Returns:
        なし。
    """

    value = {
        "self_ref": "#/texts/1",
        "link": {"$ref": "#/tables/2"},
        "page_no": 1,
        "image": {"uri": "artifacts/image.png"},
    }
    result = _remap(value, {"texts": 5, "tables": 7}, 10, "chunk_000002")
    assert result["self_ref"] == "#/texts/6"
    assert result["link"]["$ref"] == "#/tables/9"
    assert result["page_no"] == 11
    assert result["image"]["uri"] == "artifacts/chunk_000002/image.png"


def test_parse_merge_combines_collections_and_pages(tmp_path: Path) -> None:
    """複数chunkのcollectionとpageを一つの文書へ連結することを確認する。

    Args:
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    base = {
        "texts": [{"self_ref": "#/texts/0", "prov": [{"page_no": 1}]}],
        "pages": {"1": {}},
        "body": {"children": [{"$ref": "#/texts/0"}]},
        "furniture": {"children": []},
    }
    result = _merge([base, copy.deepcopy(base)], tmp_path / "source.pdf")
    assert [item["self_ref"] for item in result["texts"]] == ["#/texts/0", "#/texts/1"]
    assert set(result["pages"]) == {"1", "2"}
    assert result["texts"][1]["prov"][0]["page_no"] == 2


def test_structure_patches_keep_stable_refs() -> None:
    """構造patchとcode結合が配列refを削除しないことを確認する。

    Returns:
        なし。
    """

    document = _document()
    patches = [
        StructurePatch(op="set_label", ref="#/texts/1", label="code"),
        StructurePatch(op="merge_texts", refs=["#/texts/1", "#/texts/2"]),
        StructurePatch(
            op="set_table_cell_inline_code",
            ref="#/tables/0/data/grid/0/0",
            code_spans=["api()"],
        ),
    ]
    assert _apply(document, patches) == 3
    assert len(document["texts"]) == 3
    assert document["texts"][2]["self_ref"] == "#/texts/2"
    assert document["texts"][2]["structure_ja_v3"]["merged_into"] == "#/texts/1"


def test_page_image_rejects_parent_traversal(tmp_path: Path) -> None:
    """Structure画像pathが出力先外へ脱出できないことを確認する。

    Args:
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    document = {"pages": {"1": {"image": {"uri": "../outside.png"}}}}
    assert _page_image(document, tmp_path, 1) is None


def test_structure_fallback_halves_failed_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structure request失敗時に要素単位で二分することを確認する。

    Args:
        monkeypatch: pytest monkeypatch fixture。

    Returns:
        なし。
    """

    import translate_ja_v3.stages.structure as module

    calls: list[int] = []

    def fake_request(*_args: Any) -> list[StructurePatch]:
        """複数要素だけ失敗するfake VLMを返す。

        Args:
            _args: `_request` と同じ位置引数。

        Returns:
            空patch。

        Raises:
            RuntimeError: payloadが複数要素の場合。
        """

        payload = _args[1]
        size = sum(len(payload[group]) for group in ("texts", "cells"))
        calls.append(size)
        if size > 1:
            raise RuntimeError("too large")
        return []

    monkeypatch.setattr(module, "_request", fake_request)
    payload = {"texts": [{"ref": "a"}, {"ref": "b"}], "cells": []}
    options = PipelineOptions(input=Path("input.pdf"))
    assert len(list(_request_with_fallback(options, payload, "", None))) == 2
    assert calls == [2, 1, 1]


class _FailingTranslator(Translator):
    """複数要素だけ失敗するtest翻訳backend。"""

    def __init__(self) -> None:
        """呼び出し履歴を初期化する。

        Returns:
            なし。
        """

        self.calls: list[int] = []

    def translate(self, batch: list[dict[str, Any]]) -> dict[str, str]:
        """単一要素だけ成功させる。

        Args:
            batch: test対象batch。

        Returns:
            IDから固定訳への対応。

        Raises:
            RuntimeError: 複数要素の場合。
        """

        self.calls.append(len(batch))
        if len(batch) > 1:
            raise RuntimeError("too large")
        return {str(batch[0]["id"]): "訳"}


def test_translate_fallback_halves_failed_batch() -> None:
    """LLM翻訳失敗時に1要素まで二分することを確認する。

    Returns:
        なし。
    """

    backend = _FailingTranslator()
    batch = [{"id": str(index)} for index in range(4)]
    results = list(_translate_with_fallback(backend, batch, split_on_error=True))
    assert len(results) == 4
    assert backend.calls == [4, 2, 1, 1, 2, 1, 1]


def test_review_graph_adjudicates_only_disputes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """二Reviewerの不一致だけAdjudicatorへ渡ることを確認する。

    Args:
        monkeypatch: pytest monkeypatch fixture。

    Returns:
        なし。
    """

    import translate_ja_v3.stages.review as module

    seen: list[tuple[str, int]] = []

    class FakeChain:
        """role別のstructured responseを返すfake chain。"""

        def __init__(self, role: str) -> None:
            """応答roleを保存する。

            Args:
                role: reviewerまたはadjudicator。

            Returns:
                なし。
            """

            self.role = role

        def invoke(self, values: dict[str, Any]) -> ReviewResponse:
            """入力IDを保ったReviewResponseを返す。

            Args:
                values: prompt変数。

            Returns:
                fake ReviewResponse。
            """

            import json

            items = json.loads(values["items"])
            seen.append((self.role, len(items)))
            translations = []
            for item in items:
                text = "同じ" if item["id"] == "same" else self.role
                translations.append(
                    {"id": item["id"], "reviewed_text": text, "reason": self.role}
                )
            return ReviewResponse.model_validate({"reviews": translations})

    def fake_prompt(
        _options: PipelineOptions,
        _schema: Any,
        system: str,
        _human: str,
        *,
        max_tokens: int,
    ) -> FakeChain:
        """system promptからroleを選ぶfake factoryを返す。

        Args:
            _options: 未使用の設定。
            _schema: 未使用のschema。
            system: roleを含むsystem prompt。
            _human: 未使用のhuman prompt。
            max_tokens: 未使用の出力上限。

        Returns:
            FakeChain。
        """

        del max_tokens
        role = "adjudicator" if "Adjudicator" in system else system.split()[0]
        return FakeChain(role)

    monkeypatch.setattr(module, "prompt_runnable", fake_prompt)
    options = PipelineOptions(input=Path("input.pdf"))
    items = [
        {"id": "same", "source_text": "a", "translated_text": "同じ"},
        {"id": "different", "source_text": "b", "translated_text": "旧"},
    ]
    result = _review_graph().invoke(
        {"options": options.model_dump(mode="json"), "items": items, "rules": ""}
    )
    assert result["final"]["same"]["text"] == "同じ"
    assert ("adjudicator", 1) in seen


def test_review_fallback_halves_failed_graph() -> None:
    """Review subgraph失敗時に1要素まで二分することを確認する。

    Returns:
        なし。
    """

    class FakeGraph:
        """複数要素だけ失敗するfake Review graph。"""

        def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
            """単一要素へ固定Reviewを返す。

            Args:
                state: Review batch state。

            Returns:
                final Review結果。

            Raises:
                RuntimeError: 複数要素の場合。
            """

            if len(state["items"]) > 1:
                raise RuntimeError("too large")
            item_id = state["items"][0]["id"]
            return {"final": {item_id: {"text": "訳", "reason": "ok"}}}

    document = _document()
    targets = translation_targets(document)[:2]
    for target in targets:
        resolve_target(document, target["path"])["translate_ja_v3"] = {"text_ja": "旧"}
    options = PipelineOptions(input=Path("input.pdf"))
    results = list(
        _run_with_fallback(FakeGraph(), options, "", [], None, document, targets)
    )
    assert [len(batch) for batch, _result in results] == [1, 1]


def test_markdown_renders_translations_code_and_inline_code() -> None:
    """日本語metadataと構造metadataがMarkdownへ反映されることを確認する。

    Returns:
        なし。
    """

    document = _document()
    document["texts"][0]["translate_ja_v3"] = {"render_text": "Overview / 概要"}
    document["texts"][1]["translate_ja_v3"] = {"render_text": "APIを使う。"}
    cell = document["tables"][0]["data"]["grid"][0][0]
    cell["translate_ja_v3"] = {"render_text": "`api()`を呼ぶ"}
    result = markdown(document)
    assert "## Overview / 概要" in result
    assert "APIを使う。" in result
    assert "```\nprint('x')" in result
    assert "API table" in result


def test_manifest_cache_requires_output_hash(tmp_path: Path) -> None:
    """manifestだけでなく成果物hashも一致した場合だけResumeすることを確認する。

    Args:
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    source, output, manifest = (
        tmp_path / "source.json",
        tmp_path / "output.json",
        tmp_path / "manifest.json",
    )
    write_json(source, {"source": 1})
    write_json(output, {"output": 1})
    input_hash, config_hash = hash_file(source), hash_json({"config": 1})
    record_stage(manifest, "test", "completed", input_hash, config_hash, output)
    assert stage_cached(manifest, "test", input_hash, config_hash, output)
    write_json(output, {"output": 2})
    assert not stage_cached(manifest, "test", input_hash, config_hash, output)


def test_docx_fixes_only_consecutive_heading_spacing(tmp_path: Path) -> None:
    """連続見出しのafter/beforeだけを0へ補正することを確認する。

    Args:
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    document = b"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr></w:p>
    <w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr></w:p>
    <w:p><w:pPr><w:pStyle w:val="Normal"/></w:pPr></w:p>
  </w:body>
</w:document>"""
    path = tmp_path / "output.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)
        archive.writestr("[Content_Types].xml", "<Types/>")
    assert _fix_heading_spacing(path) == 1
    with zipfile.ZipFile(path) as archive:
        updated = archive.read("word/document.xml")
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    spacings = ET.fromstring(updated).findall(f".//{{{namespace}}}spacing")
    assert spacings[0].get(f"{{{namespace}}}after") == "0"
    assert spacings[1].get(f"{{{namespace}}}before") == "0"


def test_top_graph_executes_stages_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """top graphが登録順で全nodeを実行することを確認する。

    Args:
        monkeypatch: pytest monkeypatch fixture。
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    import translate_ja_v3.graph as module

    events: list[str] = []

    def node(name: str) -> Any:
        """実行順を記録するLangGraph nodeを作る。

        Args:
            name: 記録するStage名。

        Returns:
            stateを更新するnode関数。
        """

        def execute(_state: dict[str, Any]) -> dict[str, str]:
            """Stage名を記録して部分stateを返す。

            Args:
                _state: 未使用のgraph state。

            Returns:
                完了Stage名。
            """

            events.append(name)
            return {"completed_stage": name}

        return execute

    names = ["one", "two", "three"]
    monkeypatch.setattr(module, "STAGES", tuple((name, node(name)) for name in names))
    options = PipelineOptions(input=tmp_path / "input.pdf", output_dir=tmp_path / "out")
    paths = build_paths(options)
    build_graph().invoke(initial_state(options, paths))
    assert events == names


def test_examples_use_eight_column_glossary() -> None:
    """配布用glossary sampleが現行8列schemaであることを確認する。

    Returns:
        なし。
    """

    root = Path(__file__).parents[1]
    rows = read_glossary(root / "examples" / "glossary.csv")
    assert rows
    assert set(rows[0]) == {
        "english-short",
        "english-long",
        "japanse-short",
        "japanese-long",
        "kind",
        "description",
        "note",
        "reference",
    }
