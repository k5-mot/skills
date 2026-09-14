"""英語PDFと日本語PDFのstandalone比較Reviewを実装する。"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, cast

from src.adapters.langfuse import flush_safely, observed, update_current
from src.adapters.pandoc import pdf_pages_text
from src.config import Settings, read_rules
from src.processing.quality import Finding
from src.state import (
    OutputLock,
    atomic_write_json,
    atomic_write_text,
    load_json,
    sha256_file,
)
from src.workflows.review import ReviewOutcome, run_review

ANCHOR_RE = re.compile(r"https?://\S+|\b\d[\d.,]*\b|\b[A-Z][A-Z0-9_-]{2,}\b")


def _load_string_list(path: Path) -> list[str] | None:
    """再利用可能な文字列配列artifactを安全に読む。

    Args:
        path: JSON artifact。

    Returns:
        検証済み文字列配列。欠損または破損時はNone。
    """

    try:
        value = load_json(path)
    except (OSError, ValueError):
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return value


def _load_dict_list(path: Path) -> list[dict[str, Any]] | None:
    """再利用可能なobject配列artifactを安全に読む。

    Args:
        path: JSON artifact。

    Returns:
        検証済みobject配列。欠損または破損時はNone。
    """

    try:
        value = load_json(path)
    except (OSError, ValueError):
        return None
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        return None
    return cast(list[dict[str, Any]], value)


def _load_dict(path: Path) -> dict[str, Any] | None:
    """再利用可能なobject artifactを安全に読む。

    Args:
        path: JSON artifact。

    Returns:
        object。欠損または破損時はNone。
    """

    try:
        value = load_json(path)
    except (OSError, ValueError):
        return None
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def _anchors(text: str) -> set[str]:
    """言語をまたいで残りやすい対応付けanchorを抽出する。

    Args:
        text: PDF page本文。

    Returns:
        数値、URL、大文字識別子の集合。
    """

    return {value.casefold() for value in ANCHOR_RE.findall(text)}


def _score(source: str, destination: str) -> float:
    """二つのpageの共有anchorによる類似度を計算する。

    Args:
        source: 英語page本文。
        destination: 日本語page本文。

    Returns:
        Jaccard係数。anchorがなければ0。
    """

    left, right = _anchors(source), _anchors(destination)
    return len(left & right) / len(left | right) if left or right else 0.0


def align_pages(
    source_pages: list[str], destination_pages: list[str]
) -> list[dict[str, Any]]:
    """共有anchorとpage順fallbackでReview unitを対応付ける。

    Args:
        source_pages: 英語PDFのページ本文。
        destination_pages: 日本語PDFのページ本文。

    Returns:
        matched、split、missing、extraを明示するunit列。
    """

    alignments: list[dict[str, Any]] = []
    used: set[int] = set()
    for source_index, source in enumerate(source_pages):
        # 翻訳で改ページがずれるケースを吸収するため、1対1と1対2を同時に比較する。
        candidates: list[tuple[float, list[int]]] = []
        for destination_index, destination in enumerate(destination_pages):
            if destination_index not in used:
                candidates.append((_score(source, destination), [destination_index]))
            if (
                destination_index + 1 < len(destination_pages)
                and not {destination_index, destination_index + 1} & used
            ):
                candidates.append(
                    (
                        _score(
                            source,
                            f"{destination}\n{destination_pages[destination_index + 1]}",
                        ),
                        [destination_index, destination_index + 1],
                    )
                )
        score, selected = max(candidates, default=(0.0, []), key=lambda value: value[0])
        if score == 0:
            # 共通anchorが皆無の文書だけ、順序対応へ限定的にfallbackする。
            selected = (
                [source_index]
                if source_index < len(destination_pages) and source_index not in used
                else []
            )
        if not selected:
            status = "missing"
            destination = ""
        else:
            used.update(selected)
            status = "split" if len(selected) > 1 else "matched"
            destination = "\n\n".join(destination_pages[index] for index in selected)
        alignments.append(
            {
                "id": f"source-page-{source_index + 1}",
                "source_pages": [source_index + 1],
                "destination_pages": [index + 1 for index in selected],
                "status": status,
                "source_text": source,
                "destination_text": destination,
            }
        )
    # sourceと結び付かなかった訳文ページも、余剰としてreportから落とさない。
    for index, text in enumerate(destination_pages):
        if index not in used:
            alignments.append(
                {
                    "id": f"destination-extra-{index + 1}",
                    "source_pages": [],
                    "destination_pages": [index + 1],
                    "status": "extra",
                    "source_text": "",
                    "destination_text": text,
                }
            )
    return alignments


def _review_markdown(results: list[dict[str, Any]]) -> str:
    """対応単位ごとのReview結果をMarkdown reportへ変換する。

    Args:
        results: alignmentとReviewOutcomeを持つ結果列。

    Returns:
        `review.md`本文。
    """

    lines = ["# 翻訳比較レビュー", ""]
    for item in results:
        lines.extend([f"## {item['id']}", "", f"- 対応状態: `{item['status']}`"])
        for finding in item.get("findings", []):
            lines.extend(
                [
                    f"- **{finding['severity']} / {finding['category']}**: {finding['message']}",
                    f"  - 原文位置: {finding.get('source') or '-'}",
                    f"  - 根拠: {finding.get('evidence') or '-'}",
                    f"  - 修正案: {finding.get('suggestion') or '-'}",
                ]
            )
        if not item.get("findings"):
            lines.append("- 指摘なし")
        lines.append("")
    return "\n".join(lines)


@observed("compare-review-workflow", capture_input=False)
def run_compare_review(
    source: Path,
    destination: Path,
    output: Path,
    settings: Settings,
    force: bool = False,
) -> Path:
    """二つのPDFを対応付けて再開可能なReview reportを生成する。

    Args:
        source: 英語PDF。
        destination: 日本語PDF。
        output: `review.md`保存先。
        settings: Review modelと任意RAG設定。
        force: 既存内部成果物を再利用しないか。

    Returns:
        生成したMarkdown path。

    Raises:
        ValueError: PDF以外、入力変更、未修正Review失敗の場合。
    """

    if source.suffix.casefold() != ".pdf" or destination.suffix.casefold() != ".pdf":
        raise ValueError("review inputs must be PDFs")
    source_hash, destination_hash = sha256_file(source), sha256_file(destination)
    work = output.parent / ".work-review"
    state_path = work / "state.json"
    with OutputLock(work):
        existing = load_json(state_path)
        if isinstance(existing, dict) and not force:
            # hash不一致時のResumeを拒み、別文書のReview結果混入を防ぐ。
            if (
                existing.get("source_hash") != source_hash
                or existing.get("destination_hash") != destination_hash
            ):
                raise ValueError("review input PDF changed; use --force to rebuild")
            state = existing
            if state.get("review_model") != settings.review_model:
                state["review_model"] = settings.review_model
                state["units"] = {}
        else:
            if force and work.exists():
                for name in ("state.json", "aligned.json"):
                    (work / name).unlink(missing_ok=True)
                for name in ("source", "destination", "reviewed"):
                    target = work / name
                    if target.exists():
                        shutil.rmtree(target)
            state = {
                "schema_version": 1,
                "source_hash": source_hash,
                "destination_hash": destination_hash,
                "review_model": settings.review_model,
                "units": {},
                "last_error": None,
            }
        work.mkdir(parents=True, exist_ok=True)
        source_path = work / "source" / "pages.json"
        destination_path = work / "destination" / "pages.json"
        # stateがdoneでもartifactが破損・欠損していれば、その層だけ安全に作り直す。
        source_pages = None if force else _load_string_list(source_path)
        destination_pages = None if force else _load_string_list(destination_path)
        if source_pages is None:
            source_pages = pdf_pages_text(source)
            atomic_write_json(source_path, source_pages)
        if destination_pages is None:
            destination_pages = pdf_pages_text(destination)
            atomic_write_json(destination_path, destination_pages)
        aligned_path = work / "aligned.json"
        alignments = None if force else _load_dict_list(aligned_path)
        if alignments is None:
            alignments = align_pages(source_pages, destination_pages)
            atomic_write_json(aligned_path, alignments)
        units_state = cast(dict[str, str], state["units"])
        for item in alignments:
            units_state.setdefault(item["id"], "pending")
        atomic_write_json(state_path, state)
        rules = read_rules(settings, "review")
        results: list[dict[str, Any]] = []
        update_current(
            input={
                "source": str(source),
                "destination": str(destination),
                "rules": rules,
            }
        )
        try:
            for item in alignments:
                result_path = work / "reviewed" / f"{item['id']}.json"
                result = (
                    _load_dict(result_path)
                    if units_state[item["id"]] == "done"
                    else None
                )
                if result is not None:
                    pass
                elif item["status"] in {"missing", "extra"}:
                    category = "omission" if item["status"] == "missing" else "addition"
                    finding = Finding(
                        category=category,
                        message="対応する訳文がない"
                        if category == "omission"
                        else "対応する原文がない",
                        source=",".join(map(str, item["source_pages"])),
                    )
                    result = {
                        **item,
                        "approved": False,
                        "findings": [finding.model_dump()],
                        "evidence": [],
                    }
                else:
                    outcome: ReviewOutcome = run_review(
                        item["source_text"], item["destination_text"], rules, settings
                    )
                    result = {**item, **outcome.model_dump()}
                atomic_write_json(result_path, result)
                units_state[item["id"]] = "done"
                atomic_write_json(state_path, state)
                results.append(result)
            atomic_write_text(output, _review_markdown(results))
            state["last_error"] = None
            atomic_write_json(state_path, state)
            update_current(output={"review": str(output)})
            return output
        except BaseException as error:
            state["last_error"] = str(error)
            atomic_write_json(state_path, state)
            raise
        finally:
            flush_safely()
