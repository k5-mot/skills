"""ページ翻訳、context、Resumeを含む翻訳workflowを検証する。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.adapters.llm import ContextLengthError
from src.config import Settings
from src.model import Block, Inline, Page
from src.processing import translation as page_translation
from src.state import atomic_write_json, load_json
from src.workflows import review as review_workflow
from src.workflows import translate as workflow
from src.workflows.review import ReviewOutcome


def _page(number: int, texts: list[str]) -> Page:
    """指定本文を持つ翻訳test用ページを作る。

    Args:
        number: ページ番号。
        texts: Blockごとの本文。

    Returns:
        paragraphだけを持つPage。
    """

    return Page(
        number=number,
        blocks=[
            Block(
                id=f"p{number}-{index}",
                order=index,
                kind="paragraph",
                source=[Inline(id=f"i{number}-{index}", text=text)],
            )
            for index, text in enumerate(texts)
        ],
    )


def test_translate_page_uses_neighbor_context_and_rejects_foreign_ids(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """翻訳promptが前後文脈を含み対象IDだけを適用することを確認する。

    Args:
        monkeypatch: LLMを差し替えるfixture。
        settings: 共通Settings fixture。

    Returns:
        なし。
    """

    prompts: list[str] = []

    def chat(
        _settings: Settings,
        _model: str,
        _system: str,
        user: str,
        _name: str,
        _schema: dict[str, Any],
        _image: object = None,
    ) -> dict[str, Any]:
        """入力IDを保った翻訳応答を返す。

        Args:
            _settings: 未使用設定。
            _model: 未使用model。
            _system: 未使用prompt。
            user: 記録するuser prompt。
            _name: 未使用schema名。
            _schema: 未使用schema。
            _image: 未使用画像。

        Returns:
            対象IDの翻訳応答。
        """

        prompts.append(user)
        return {"translations": [{"id": "i2-0", "text": "訳文"}]}

    monkeypatch.setattr(page_translation, "structured_chat", chat)
    page = workflow.translate_page(
        _page(2, ["Text"]), "Previous", "Following", "rules", [], settings, "openai"
    )
    assert page.blocks[0].translated is not None
    assert page.blocks[0].translated[0].text == "訳文"
    assert "Previous" in prompts[0] and "Following" in prompts[0]
    monkeypatch.setattr(
        page_translation,
        "structured_chat",
        lambda *_args, **_kwargs: {"translations": [{"id": "foreign", "text": "訳"}]},
    )
    with pytest.raises(ValueError, match="exactly match"):
        workflow.translate_page(
            _page(2, ["Text"]), "", "", "rules", [], settings, "openai"
        )


def test_context_overflow_bisects_only_once(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """API context超過時に複数要素を一度だけ二分することを確認する。

    Args:
        monkeypatch: LLMを差し替えるfixture。
        settings: 共通Settings fixture。

    Returns:
        なし。
    """

    calls = 0

    def chat(
        _settings: Settings,
        _model: str,
        _system: str,
        user: str,
        _name: str,
        _schema: dict[str, Any],
        _image: object = None,
    ) -> dict[str, Any]:
        """複数ID要求だけcontext errorにする。

        Args:
            _settings: 未使用設定。
            _model: 未使用model。
            _system: 未使用prompt。
            user: IDを判定するprompt。
            _name: 未使用schema名。
            _schema: 未使用schema。
            _image: 未使用画像。

        Returns:
            単一IDの翻訳応答。
        """

        nonlocal calls
        calls += 1
        ids = [item_id for item_id in ("i2-0", "i2-1") if item_id in user]
        if len(ids) > 1:
            raise ContextLengthError("context")
        return {"translations": [{"id": ids[0], "text": f"訳{ids[0]}"}]}

    monkeypatch.setattr(page_translation, "structured_chat", chat)
    page = workflow.translate_page(
        _page(2, ["A", "B"]), "", "", "rules", [], settings, "openai"
    )
    assert calls == 3
    assert [item.text for block in page.blocks for item in block.translated or []] == [
        "訳i2-0",
        "訳i2-1",
    ]


def test_libretranslate_backend_changes_only_translation(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """LibreTranslate選択時も同じPage構造へ訳文だけを反映する。

    Args:
        monkeypatch: 翻訳adapterを差し替えるfixture。
        settings: 共通Settings fixture。

    Returns:
        なし。
    """

    from dataclasses import replace

    configured = replace(settings, libretranslate_url="http://libre")
    page = _page(2, ["First", "Second"])
    monkeypatch.setattr(
        page_translation,
        "structured_chat",
        lambda *_args, **_kwargs: pytest.fail("OpenAI translation must not be called"),
    )
    monkeypatch.setattr(
        page_translation,
        "translate_texts",
        lambda _url, _key, texts: [f"訳:{text}" for text in texts],
    )
    result = workflow.translate_page(
        page, "Previous", "Following", "rules", [], configured, "libretranslate"
    )
    assert [block.id for block in result.blocks] == ["p2-0", "p2-1"]
    assert [
        item.text for block in result.blocks for item in block.translated or []
    ] == ["訳:First", "訳:Second"]


def _raw_document() -> dict[str, Any]:
    """表紙と翻訳2ページを持つ最小Docling documentを返す。

    Returns:
        三ページのDocling JSON。
    """

    return {
        "schema_name": "DoclingDocument",
        "version": "1",
        "pages": {"1": {}, "2": {}, "3": {}},
        "body": {"children": [{"$ref": f"#/texts/{index}"} for index in range(3)]},
        "furniture": {"children": []},
        "groups": [],
        "texts": [
            {
                "self_ref": "#/texts/0",
                "label": "title",
                "text": "Cover",
                "prov": [{"page_no": 1}],
            },
            {
                "self_ref": "#/texts/1",
                "label": "paragraph",
                "text": "Page two",
                "prov": [{"page_no": 2}],
            },
            {
                "self_ref": "#/texts/2",
                "label": "paragraph",
                "text": "Page three",
                "prov": [{"page_no": 3}],
            },
        ],
        "tables": [],
        "pictures": [],
        "key_value_items": [],
        "form_items": [],
    }


def test_translation_resumes_after_failed_page(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """途中Review失敗後に完了ページを再利用し未完了ページから再開する。

    Args:
        monkeypatch: 外部adapterをlocal fakeへ差し替えるfixture。
        settings: 共通Settings fixture。
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf")
    raw = _raw_document()
    structure_calls: list[int] = []
    translation_calls: list[str] = []
    review_calls: list[str] = []
    fail_once = True
    monkeypatch.setattr(workflow, "check_pandoc", lambda: None)
    monkeypatch.setattr(
        workflow, "pdf_pages_text", lambda _path: ["cover", "two", "three"]
    )

    def convert(
        _source: Path,
        output: Path,
        _assets: Path,
        _url: str,
        _key: str | None,
    ) -> dict[str, Any]:
        """Docling JSONをlocal保存して返す。

        Args:
            _source: 未使用入力。
            output: raw JSON保存先。
            _assets: 未使用asset先。
            _url: 未使用URL。
            _key: 未使用key。

        Returns:
            固定Docling JSON。
        """

        atomic_write_json(output, raw)
        return raw

    def structure(page: Page, _image: Path, _rules: str, _settings: Settings) -> Page:
        """Structure呼出しページを記録してそのまま返す。

        Args:
            page: 対象ページ。
            _image: 未使用画像。
            _rules: 未使用Rules。
            _settings: 未使用設定。

        Returns:
            入力ページ。
        """

        structure_calls.append(page.number)
        return page

    def chat(
        _settings: Settings,
        _model: str,
        _system: str,
        user: str,
        _name: str,
        _schema: dict[str, Any],
        _image: object = None,
    ) -> dict[str, Any]:
        """対象IDを抽出して固定翻訳を返す。

        Args:
            _settings: 未使用設定。
            _model: 未使用model。
            _system: 未使用prompt。
            user: IDを含むprompt。
            _name: 未使用schema名。
            _schema: 未使用schema。
            _image: 未使用画像。

        Returns:
            対象ページの翻訳。
        """

        item_id = (
            "#/texts/1/inline/0"
            if "#/texts/1/inline/0" in user
            else "#/texts/2/inline/0"
        )
        translation_calls.append(item_id)
        return {
            "translations": [
                {"id": item_id, "text": "二ページ" if "/1/" in item_id else "三ページ"}
            ]
        }

    def review(
        source_text: str,
        target: str,
        _rules: str,
        _settings: Settings,
        _glossary: object = None,
    ) -> ReviewOutcome:
        """三ページ目の初回だけVerifier失敗を模擬する。

        Args:
            source_text: 原文。
            target: 訳文。
            _rules: 未使用Rules。
            _settings: 未使用設定。
            _glossary: 未使用用語集。

        Returns:
            合格時のReviewOutcome。
        """

        nonlocal fail_once
        review_calls.append(source_text)
        if source_text == "Page three" and fail_once:
            fail_once = False
            raise RuntimeError("Verifier rejected")
        return ReviewOutcome(approved=True, text=target)

    def image(_source: Path, _number: int, output: Path, _dpi: int = 120) -> Path:
        """Structure用dummy画像を保存する。

        Args:
            _source: 未使用PDF。
            _number: 未使用ページ番号。
            output: 保存先。
            _dpi: 未使用DPI。

        Returns:
            保存したpath。
        """

        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"png")
        return output

    def cover(_source: Path, output: Path, _dpi: int = 150) -> Path:
        """dummy表紙画像を保存する。

        Args:
            _source: 未使用PDF。
            output: 保存先。
            _dpi: 未使用DPI。

        Returns:
            保存したpath。
        """

        output.write_bytes(b"png")
        return output

    def docx(_markdown: Path, output: Path, _template: Path) -> None:
        """dummy DOCX成果物を保存する。

        Args:
            _markdown: 未使用Markdown。
            output: 保存先。
            _template: 未使用template。

        Returns:
            なし。
        """

        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"docx")

    monkeypatch.setattr(workflow, "convert_pdf", convert)
    monkeypatch.setattr(workflow, "render_page", image)
    monkeypatch.setattr(workflow, "structure_page", structure)
    monkeypatch.setattr(page_translation, "structured_chat", chat)
    monkeypatch.setattr(review_workflow, "run_review", review)
    monkeypatch.setattr(workflow, "render_cover", cover)
    monkeypatch.setattr(workflow, "create_docx", docx)
    with pytest.raises(RuntimeError, match="Verifier"):
        workflow.run_translation(source, tmp_path / "output", settings)
    state_path = tmp_path / "output" / "source" / ".work" / "state.json"
    state = load_json(state_path)
    assert state["pages"]["2"]["review"] == "done"
    assert state["pages"]["3"]["review"] == "failed"
    result = workflow.run_translation(source, tmp_path / "output", settings)
    assert result == tmp_path / "output" / "source" / "document.ja.docx"
    assert structure_calls == [2, 3]
    assert len(translation_calls) == 2
    assert review_calls == ["Page two", "Page three", "Page three"]
    reviewed_page_two = (
        tmp_path / "output" / "source" / ".work" / "reviewed" / "page_000002.json"
    )
    reviewed_page_two.unlink()
    workflow.run_translation(source, tmp_path / "output", settings)
    assert review_calls[-1] == "Page two"
    work_entries = {
        path.name for path in (tmp_path / "output" / "source" / ".work").iterdir()
    }
    assert work_entries == {
        "state.json",
        "parsed.json",
        "normalized.json",
        "structured",
        "translated",
        "reviewed",
        "document.ja.md",
    }


def test_dry_run_performs_no_write_or_api_call(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """dry-runが計画表示だけを行い出力先を作らないことを確認する。

    Args:
        monkeypatch: Pandoc検査とPDF page数を差し替えるfixture。
        settings: 共通Settings fixture。
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf")
    monkeypatch.setattr(workflow, "check_pandoc", lambda: None)
    monkeypatch.setattr(workflow, "pdf_pages_text", lambda _path: ["cover", "body"])
    monkeypatch.setattr(
        workflow,
        "convert_pdf",
        lambda *_args, **_kwargs: pytest.fail("API must not be called"),
    )
    assert (
        workflow.run_translation(source, tmp_path / "output", settings, dry_run=True)
        is None
    )
    assert not (tmp_path / "output").exists()


def test_fifty_thousand_token_budget_splits_long_page(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """50,000 token想定の長いページを予算内requestへ分割する。

    Args:
        monkeypatch: LLMを予算検査fakeへ差し替えるfixture。
        settings: 50,000 token既定の共通Settings。

    Returns:
        なし。
    """

    page = _page(2, [f"block-{index}-" + "x" * 100 for index in range(500)])
    request_sizes: list[int] = []

    def chat(
        _settings: Settings,
        _model: str,
        _system: str,
        user: str,
        _name: str,
        _schema: dict[str, Any],
        _image: object = None,
    ) -> dict[str, Any]:
        """request sizeを記録してprompt内IDへ翻訳を返す。

        Args:
            _settings: 未使用設定。
            _model: 未使用model。
            _system: 未使用prompt。
            user: 翻訳対象JSONを含むprompt。
            _name: 未使用schema名。
            _schema: 未使用schema。
            _image: 未使用画像。

        Returns:
            promptに含まれる全IDの翻訳応答。
        """

        import re

        request_sizes.append(len(user))
        ids = re.findall(r'"id": "(i2-\d+)"', user)
        return {
            "translations": [
                {"id": item_id, "text": f"訳-{item_id}"} for item_id in ids
            ]
        }

    monkeypatch.setattr(page_translation, "structured_chat", chat)
    result = workflow.translate_page(
        page, "p" * 10_000, "n" * 10_000, "rules", [], settings, "openai"
    )
    assert len(request_sizes) >= 2
    assert all(size < settings.context_tokens for size in request_sizes)
    assert sum(len(block.translated or []) for block in result.blocks) == 500
