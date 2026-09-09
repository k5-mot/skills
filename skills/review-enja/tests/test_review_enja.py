"""review-enjaの決定論的な契約を検証する。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import review_enja.pipeline as module
from review_enja.pipeline import (
    AlignmentItem,
    AlignmentResponse,
    ReviewResponse,
    _adjudicate,
    _automatic_review,
    _align_batch,
    _materialize_alignments,
    _paired_batches,
    _render_report,
    _review_graph,
    _segments,
    _validate_alignments,
)


def _document() -> dict[str, Any]:
    """本文と各種表cell schemaを持つ最小Docling文書を返す。

    Returns:
        segment走査用Docling JSON。
    """

    return {
        "texts": [
            {"text": "Heading", "label": "section_header", "prov": [{"page_no": 1}]},
            {"text": "Body", "label": "paragraph", "prov": [{"page_no": 1}]},
            {"text": "code()", "label": "code", "prov": [{"page_no": 1}]},
        ],
        "tables": [
            {
                "caption": "Table caption",
                "prov": [{"page_no": 2}],
                "data": {"grid": [[{"text": "Grid cell"}]]},
            },
            {
                "prov": [{"page_no": 3}],
                "data": {
                    "table_cells": [
                        {
                            "text": "Table cell",
                            "start_row_offset_idx": 0,
                            "start_col_offset_idx": 0,
                        }
                    ]
                },
            },
            {
                "prov": [{"page_no": 4}],
                "data": {"cells": [{"text": "Cell", "row": 0, "col": 0}]},
            },
        ],
    }


def _alignment(item_id: str = "A000001", status: str = "aligned") -> dict[str, Any]:
    """test用Alignment要素を返す。

    Args:
        item_id: Alignment ID。
        status: aligned、source_only、translation_onlyのいずれか。

    Returns:
        ReviewStage入力形式のdict。
    """

    return {
        "id": item_id,
        "status": status,
        "source_ids": ["en:1"] if status != "translation_only" else [],
        "translation_ids": ["ja:1"] if status != "source_only" else [],
        "source_pages": [1] if status != "translation_only" else [],
        "translation_pages": [2] if status != "source_only" else [],
        "source_text": "Source" if status != "translation_only" else "",
        "translated_text": "訳文" if status != "source_only" else "",
        "alignment_reason": "",
    }


def test_segments_include_all_table_cell_schemas() -> None:
    """本文と3種の表cellを抽出しcodeを除外することを確認する。

    Returns:
        なし。
    """

    result = _segments(_document(), "en")
    assert [item["text"] for item in result] == [
        "Heading",
        "Body",
        "Table caption",
        "Grid cell",
        "Table cell",
        "Cell",
    ]
    assert [item["page"] for item in result] == [1, 1, 2, 2, 3, 4]


def test_paired_batches_preserve_both_inputs() -> None:
    """相対文字位置batchが英日両入力を順序通り保持することを確認する。

    Returns:
        なし。
    """

    sources = [{"id": f"en:{index}", "text": "source" * index} for index in range(1, 6)]
    translations = [
        {"id": f"ja:{index}", "text": "訳文" * index} for index in range(1, 8)
    ]
    batches = _paired_batches(sources, translations, 20, 4)
    assert [item["id"] for batch, _ in batches for item in batch] == [
        item["id"] for item in sources
    ]
    assert [item["id"] for _, batch in batches for item in batch] == [
        item["id"] for item in translations
    ]


def test_alignment_requires_exact_order_and_coverage() -> None:
    """Alignmentの欠落、重複、順序変更を拒否することを確認する。

    Returns:
        なし。
    """

    sources = [{"id": "en:1"}, {"id": "en:2"}]
    translations = [{"id": "ja:1"}, {"id": "ja:2"}]
    valid = [
        AlignmentItem(source_ids=["en:1"], translation_ids=["ja:1"]),
        AlignmentItem(source_ids=["en:2"], translation_ids=["ja:2"]),
    ]
    _validate_alignments(valid, sources, translations)
    with pytest.raises(ValueError, match="source IDs"):
        _validate_alignments(list(reversed(valid)), sources, translations)
    with pytest.raises(ValueError, match="translation IDs"):
        _validate_alignments(valid[:1], sources[:1], translations)


def test_structured_responses_accept_root_arrays() -> None:
    """AlignmentとReviewがroot配列形式のLLM応答を受理することを確認する。

    Returns:
        なし。
    """

    alignment = AlignmentResponse.model_validate(
        [{"source_ids": ["en:1"], "translation_ids": ["ja:1"]}]
    )
    review = ReviewResponse.model_validate(
        [
            {
                "id": "A000001",
                "status": "pass",
                "severity": "info",
                "suggested_translation": "訳文",
                "reason": "No issue",
            }
        ]
    )
    assert alignment.alignments[0].source_ids == ["en:1"]
    assert review.reviews[0].status == "pass"


def test_materialize_alignment_uses_local_text() -> None:
    """Alignment IDからローカル本文とページを復元することを確認する。

    Returns:
        なし。
    """

    result = _materialize_alignments(
        [AlignmentItem(source_ids=["en:1", "en:2"], translation_ids=["ja:1"])],
        [
            {"id": "en:1", "text": "First", "page": 1},
            {"id": "en:2", "text": "Second", "page": 2},
        ],
        [{"id": "ja:1", "text": "訳", "page": 3}],
    )
    assert result[0]["source_text"] == "First\nSecond"
    assert result[0]["translated_text"] == "訳"
    assert result[0]["source_pages"] == [1, 2]


def test_align_batch_uses_only_returned_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Alignment AgentのID応答を検証して採用することを確認する。

    Args:
        monkeypatch: pytest monkeypatch fixture。
        tmp_path: pytest一時directory fixture。

    Returns:
        なし。
    """

    class FakeChain:
        """外部LLMを呼ばないAlignment chain。"""

        def invoke(self, _payload: dict[str, Any]) -> AlignmentResponse:
            """固定の多対一Alignmentを返す。

            Args:
                _payload: 未使用のprompt変数。

            Returns:
                検証対象Alignment応答。
            """

            return AlignmentResponse(
                alignments=[
                    AlignmentItem(source_ids=["en:1", "en:2"], translation_ids=["ja:1"])
                ]
            )

    def fake_prompt(*_args: Any, **_kwargs: Any) -> FakeChain:
        """test用Alignment chainを返す。

        Args:
            *_args: 未使用の位置引数。
            **_kwargs: 未使用のkeyword引数。

        Returns:
            固定応答を返すFakeChain。
        """

        return FakeChain()

    monkeypatch.setattr(module, "prompt_runnable", fake_prompt)
    options = module.ReviewOptions(
        source=tmp_path / "source.pdf",
        translation=tmp_path / "translation.pdf",
        output_dir=tmp_path,
    )
    result = list(
        _align_batch(
            options,
            [{"id": "en:1", "text": "One"}, {"id": "en:2", "text": "Two"}],
            [{"id": "ja:1", "text": "一と二"}],
        )
    )
    assert result[0].source_ids == ["en:1", "en:2"]
    assert result[0].translation_ids == ["ja:1"]


def test_unpaired_alignment_is_reviewed_without_llm() -> None:
    """片言語要素を欠落または追加としてmajor判定することを確認する。

    Returns:
        なし。
    """

    omission = _automatic_review(_alignment(status="source_only"))["final"]
    addition = _automatic_review(_alignment(status="translation_only"))["final"]
    assert (omission["status"], omission["severity"], omission["categories"]) == (
        "needs_change",
        "major",
        ["omission"],
    )
    assert addition["categories"] == ["addition"]
    assert addition["suggested_translation"] == ""


def test_review_graph_runs_two_reviewers_before_adjudication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review subgraphが二Reviewerの結果をAdjudicatorへ渡すことを確認する。

    Args:
        monkeypatch: pytest monkeypatch fixture。

    Returns:
        なし。
    """

    def fake_fidelity(_state: dict[str, Any]) -> dict[str, Any]:
        """test用fidelity結果を返す。

        Args:
            _state: 未使用のgraph state。

        Returns:
            fidelity marker。
        """

        return {"fidelity": {"A": {"status": "pass"}}}

    def fake_terminology(_state: dict[str, Any]) -> dict[str, Any]:
        """test用terminology結果を返す。

        Args:
            _state: 未使用のgraph state。

        Returns:
            terminology marker。
        """

        return {"terminology": {"A": {"status": "pass"}}}

    def fake_adjudicate(state: dict[str, Any]) -> dict[str, Any]:
        """両Reviewer結果の到達を検証する。

        Args:
            state: Reviewer結果を含むgraph state。

        Returns:
            final marker。
        """

        assert "A" in state["fidelity"] and "A" in state["terminology"]
        return {"final": {"A": {"status": "pass"}}}

    monkeypatch.setattr(module, "_fidelity", fake_fidelity)
    monkeypatch.setattr(module, "_terminology", fake_terminology)
    monkeypatch.setattr(module, "_adjudicate", fake_adjudicate)
    result = _review_graph().invoke({})
    assert result["final"]["A"]["status"] == "pass"


def test_adjudicator_skips_llm_when_decisions_agree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """理由だけが異なる一致案ではAdjudicatorを呼ばないことを確認する。

    Args:
        monkeypatch: pytest monkeypatch fixture。

    Returns:
        なし。
    """

    def fail_prompt(*_args: Any, **_kwargs: Any) -> None:
        """呼び出された場合にtestを失敗させる。

        Args:
            *_args: 未使用の位置引数。
            **_kwargs: 未使用のkeyword引数。

        Returns:
            この関数は正常終了しない。

        Raises:
            AssertionError: 関数が呼ばれた場合。
        """

        raise AssertionError("Adjudicator must not call the LLM")

    base = {
        "id": "A000001",
        "status": "pass",
        "severity": "info",
        "categories": [],
        "suggested_translation": "訳文",
        "reason": "first",
    }
    monkeypatch.setattr(module, "prompt_runnable", fail_prompt)
    result = _adjudicate(
        {
            "items": [{"id": "A000001"}],
            "fidelity": {"A000001": base},
            "terminology": {"A000001": {**base, "reason": "second"}},
        }
    )
    assert result["final"]["A000001"] == base


def test_report_contains_only_issue_details() -> None:
    """Reportが集計と要修正項目の詳細を描画することを確認する。

    Returns:
        なし。
    """

    passed = {**_alignment("A000001"), **_automatic_review(_alignment("A000001"))}
    passed["final"] = {
        **passed["final"],
        "status": "pass",
        "severity": "info",
    }
    issue = {
        **_alignment("A000002", "source_only"),
        **_automatic_review(_alignment("A000002", "source_only")),
    }
    document = {
        "source": "/source.pdf",
        "translation": "/translation.pdf",
        "summary": {
            "total": 2,
            "pass": 1,
            "needs_change": 1,
            "severity": {"critical": 0, "major": 1, "minor": 0, "info": 1},
        },
        "reviews": [passed, issue],
    }
    result = _render_report(document)
    assert "A000002" in result
    assert "A000001 —" not in result
    assert "要修正: 1" in result
