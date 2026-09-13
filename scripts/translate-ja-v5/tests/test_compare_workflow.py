"""standalone PDF比較の対応付け、Resume、Markdownを検証する。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import Settings
from src.workflows import compare
from src.workflows.review import ReviewOutcome


def test_align_pages_detects_reorder_split_missing_and_extra() -> None:
    """共有anchorから再順序、分割、欠落、余分ページを明示することを確認する。

    Returns:
        なし。
    """

    values = compare.align_pages(
        ["SEC_A 100 SEC_B 200", "SEC_C 300", "SEC_D 400"],
        ["SEC_C 300", "SEC_A 100", "SEC_B 200", "EXTRA 999"],
    )
    assert values[0]["status"] == "split"
    assert values[0]["destination_pages"] == [2, 3]
    assert values[1]["destination_pages"] == [1]
    assert values[2]["status"] == "missing"
    assert values[-1]["status"] == "extra"


def test_compare_review_writes_report_and_reuses_units(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """比較Reviewの再実行が完了unitを再利用することを確認する。

    Args:
        monkeypatch: PDF抽出とReviewを差し替えるfixture。
        settings: 共通Settings fixture。
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    source = tmp_path / "source.pdf"
    destination = tmp_path / "destination.pdf"
    source.write_bytes(b"source")
    destination.write_bytes(b"destination")
    output = tmp_path / "review.md"
    extraction_calls: list[Path] = []
    alignment_calls = 0
    original_align = compare.align_pages

    def extract(path: Path) -> list[str]:
        """PDF抽出呼出しを記録してpage本文を返す。

        Args:
            path: sourceまたはdestination PDF。

        Returns:
            対応する一ページの本文。
        """

        extraction_calls.append(path)
        return ["SEC 100"] if path == source else ["SEC 100 訳"]

    def align(source_pages: list[str], destination_pages: list[str]) -> list[dict]:
        """対応付け呼出しを記録して本来の処理へ委譲する。

        Args:
            source_pages: source本文列。
            destination_pages: destination本文列。

        Returns:
            対応付け結果。
        """

        nonlocal alignment_calls
        alignment_calls += 1
        return original_align(source_pages, destination_pages)

    monkeypatch.setattr(compare, "pdf_pages_text", extract)
    monkeypatch.setattr(compare, "align_pages", align)
    calls = 0

    def review_text(
        _source: str,
        target: str,
        _rules: str,
        _settings: Settings,
    ) -> ReviewOutcome:
        """呼出し回数を数えて合格結果を返す。

        Args:
            _source: 未使用原文。
            target: 返す訳文。
            _rules: 未使用Rules。
            _settings: 未使用設定。

        Returns:
            合格ReviewOutcome。
        """

        nonlocal calls
        calls += 1
        return ReviewOutcome(approved=True, text=target)

    monkeypatch.setattr(compare, "run_review", review_text)
    assert compare.run_compare_review(source, destination, output, settings) == output
    assert "指摘なし" in output.read_text(encoding="utf-8")
    assert compare.run_compare_review(source, destination, output, settings) == output
    assert calls == 1
    assert extraction_calls == [source, destination]
    assert alignment_calls == 1
    assert (tmp_path / ".work-review" / "aligned.json").exists()


def test_compare_review_rebuilds_corrupt_cached_extraction(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """破損した抽出artifactだけを再生成してReviewを継続することを確認する。

    Args:
        monkeypatch: PDF抽出とReviewを差し替えるfixture。
        settings: 共通Settings fixture。
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    source = tmp_path / "source.pdf"
    destination = tmp_path / "destination.pdf"
    source.write_bytes(b"source")
    destination.write_bytes(b"destination")
    calls: list[Path] = []

    def extract(path: Path) -> list[str]:
        """抽出対象を記録して固定本文を返す。

        Args:
            path: 抽出するPDF。

        Returns:
            一ページの固定本文。
        """

        calls.append(path)
        return ["SEC 100"]

    monkeypatch.setattr(compare, "pdf_pages_text", extract)
    monkeypatch.setattr(
        compare,
        "run_review",
        lambda source, target, rules, settings: ReviewOutcome(
            approved=True, text=target
        ),
    )
    output = tmp_path / "review.md"
    compare.run_compare_review(source, destination, output, settings)
    (tmp_path / ".work-review" / "source" / "pages.json").write_text(
        "broken", encoding="utf-8"
    )
    compare.run_compare_review(source, destination, output, settings)
    assert calls == [source, destination, source]


def test_compare_review_rejects_changed_input_without_force(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """入力hash変更を通常Resumeで拒否することを確認する。

    Args:
        monkeypatch: PDF抽出とReviewを差し替えるfixture。
        settings: 共通Settings fixture。
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    source = tmp_path / "source.pdf"
    destination = tmp_path / "destination.pdf"
    source.write_bytes(b"source")
    destination.write_bytes(b"destination")
    monkeypatch.setattr(compare, "pdf_pages_text", lambda _path: ["SEC 100"])
    monkeypatch.setattr(
        compare,
        "run_review",
        lambda source, target, rules, settings: ReviewOutcome(
            approved=True, text=target
        ),
    )
    compare.run_compare_review(source, destination, tmp_path / "review.md", settings)
    source.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        compare.run_compare_review(
            source, destination, tmp_path / "review.md", settings
        )


def test_compare_review_resumes_from_failed_unit(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """途中失敗後に完了済み比較unitを再利用することを確認する。

    Args:
        monkeypatch: PDF抽出とReviewを差し替えるfixture。
        settings: 共通Settings fixture。
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    source = tmp_path / "source.pdf"
    destination = tmp_path / "destination.pdf"
    source.write_bytes(b"source")
    destination.write_bytes(b"destination")
    pages = {
        source: ["SECTION_A 100", "SECTION_B 200"],
        destination: ["SECTION_A 100 訳", "SECTION_B 200 訳"],
    }
    monkeypatch.setattr(compare, "pdf_pages_text", lambda path: pages[path])
    calls: list[str] = []
    fail_once = True

    def review_text(
        source_text: str,
        target: str,
        _rules: str,
        _settings: Settings,
    ) -> ReviewOutcome:
        """二つ目のunitだけ初回に失敗させる。

        Args:
            source_text: 原文unit。
            target: 対応訳文。
            _rules: 未使用Rules。
            _settings: 未使用設定。

        Returns:
            合格ReviewOutcome。
        """

        nonlocal fail_once
        calls.append(source_text)
        if "200" in source_text and fail_once:
            fail_once = False
            raise RuntimeError("interrupted")
        return ReviewOutcome(approved=True, text=target)

    monkeypatch.setattr(compare, "run_review", review_text)
    output = tmp_path / "review.md"
    with pytest.raises(RuntimeError, match="interrupted"):
        compare.run_compare_review(source, destination, output, settings)
    compare.run_compare_review(source, destination, output, settings)
    assert calls == ["SECTION_A 100", "SECTION_B 200", "SECTION_B 200"]
