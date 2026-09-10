"""英日PDF指摘票CLIの決定論的処理を検証する。"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import translate_ja_v4.review_docx as module


def _alignment(
    item_id: str,
    status: str = "aligned",
    source: str = "Source text",
    translation: str = "現在訳",
) -> dict[str, Any]:
    """test用Alignmentを生成する。

    Args:
        item_id: Alignment ID。
        status: aligned、source_only、translation_onlyのいずれか。
        source: 英語原文。
        translation: 日本語訳。

    Returns:
        review-enja互換Alignment。
    """
    return {
        "id": item_id,
        "status": status,
        "source_pages": [1] if source else [],
        "translation_pages": [2] if translation else [],
        "source_text": source,
        "translated_text": translation,
    }


def test_translated_document_preserves_alignment_without_translation_only() -> None:
    """ReviewStage入力は原文のある対応だけをmetadata付きで保持する。

    Args:
        なし。

    Returns:
        なし。
    """
    alignments = [
        _alignment("A1"),
        _alignment("A2", "translation_only", "", "追加訳"),
    ]

    document = module._translated_document(alignments)

    assert len(document["texts"]) == 1
    assert document["texts"][0]["review_docx"]["id"] == "A1"
    assert document["texts"][0]["translate_ja_v4"]["text_ja"] == "現在訳"


def test_findings_include_revision_omission_and_addition(tmp_path: Path) -> None:
    """変更案、欠落、追加だけを指摘事項へ列挙する。

    Args:
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """
    alignments = [
        _alignment("A1"),
        _alignment("A2", "aligned", "Unchanged", "同じ訳"),
        _alignment("A3", "source_only", "Missing", ""),
        _alignment("A4", "translation_only", "", "追加訳"),
    ]
    reviewed = module._translated_document(alignments)
    reviewed["texts"][0]["translate_ja_v4"].update(
        {"text_ja": "修正訳", "review_ja_v4": {"reason": "Meaning differs."}}
    )
    reviewed["texts"][1]["translate_ja_v4"].update(
        {"review_ja_v4": {"reason": "No change."}}
    )
    options = module.ReviewDocxOptions(
        source=tmp_path / "source.pdf",
        translation=tmp_path / "translation.pdf",
        output_dir=tmp_path / "out",
    )

    result = module._findings_document(reviewed, alignments, options)

    assert [item["category"] for item in result["findings"]] == [
        "revision",
        "omission",
        "addition",
    ]
    assert result["summary"] == {"alignments": 4, "findings": 3}


def test_run_calls_v4_review_stage_without_modifying_pdfs(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """runはv4 ReviewStageを呼び、入力PDFを変更せず指摘票を保存する。

    Args:
        monkeypatch: pytest monkeypatch fixture。
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """
    source = tmp_path / "source.pdf"
    translation = tmp_path / "translation.pdf"
    source.write_bytes(b"source-pdf")
    translation.write_bytes(b"translation-pdf")
    alignments = [_alignment("A1")]
    called = False

    def fake_align(options: module.ReviewDocxOptions) -> list[dict[str, Any]]:
        """外部APIを使わずtest用Alignmentを返す。

        Args:
            options: 実行設定。

        Returns:
            固定Alignment配列。
        """
        return alignments

    def fake_review(state: dict[str, Any]) -> dict[str, Any]:
        """ReviewStage呼出しを記録し修正版JSONを保存する。

        Args:
            state: v4 PipelineState。

        Returns:
            ReviewStage互換の部分state。

        Side Effects:
            test用document.reviewed.jsonを保存する。
        """
        nonlocal called
        called = True
        paths = module.build_paths(
            module.PipelineOptions.model_validate(state["options"])
        )
        document = copy.deepcopy(module.read_json(paths.translated_json))
        document["texts"][0]["translate_ja_v4"].update(
            {"text_ja": "修正訳", "review_ja_v4": {"reason": "Incorrect."}}
        )
        module.write_json(paths.reviewed_json, document)
        return {"current_path": str(paths.reviewed_json), "completed_stage": "review"}

    monkeypatch.setattr(module, "_align_pdfs", fake_align)
    monkeypatch.setattr(module, "v4_review_stage", fake_review)
    monkeypatch.setattr(module, "flush_langfuse", lambda: None)
    options = module.ReviewDocxOptions(
        source=source,
        translation=translation,
        output_dir=tmp_path / "output",
    )

    paths = module.run(options)

    assert called
    assert source.read_bytes() == b"source-pdf"
    assert translation.read_bytes() == b"translation-pdf"
    assert paths.findings_json.is_file()
    assert "修正訳" in paths.report.read_text(encoding="utf-8")
