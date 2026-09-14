"""用語集と翻訳の決定的品質検査を提供する。"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from pydantic import BaseModel

PROTECTED_RE = re.compile(
    r"`[^`\n]+`"
    r"|https?://[^\s<>()]+"
    r"|www\.[^\s<>()]+"
    r"|(?<!\w)[A-Za-z]:\\[^\s]+"
    r"|(?<!\w)(?:\.\.?/|~/|/)[A-Za-z0-9_.~+@%/-]+"
    r"|(?<!\w)--?[A-Za-z][A-Za-z0-9-]*"
    r"|\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b"
    r"|\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b"
    r"|\b[a-z]+(?:[A-Z][A-Za-z0-9]*)+\b"
)
NUMBER_UNIT_RE = re.compile(
    r"(?<!\w)[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    r"(?:\s?(?:%|ms|s|MB|GB|KB|V|A|Hz|°C))?"
)
EN_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|without|mustn['’]t|cannot|can't)\b", re.IGNORECASE
)
JA_NEGATION_RE = re.compile(r"(?:ない|ません|不可|禁止|ず|なし|ないで|できない)")
EN_CONDITION_RE = re.compile(
    r"\b(?:if|unless|when|provided that|in case)\b", re.IGNORECASE
)
JA_CONDITION_RE = re.compile(r"(?:場合|とき|なら|限り|条件|際)")
EN_COMPARISON_RE = re.compile(
    r"\b(?:more|less|than|at least|at most|greater|smaller|higher|lower)\b",
    re.IGNORECASE,
)
JA_COMPARISON_RE = re.compile(r"(?:以上|以下|より|超|未満|多|少|高|低)")


class GlossaryEntry(BaseModel):
    """原語と指定訳の一組を表す。"""

    source: str
    target: str
    notes: str = ""


class Finding(BaseModel):
    """Reviewで検出した一つの問題を表す。"""

    category: str
    severity: str = "critical"
    message: str
    source: str = ""
    evidence: str = ""
    suggestion: str = ""


def read_glossary(path: Path | None) -> list[GlossaryEntry]:
    """v5のUTF-8 CSV用語集を検証して読む。

    Args:
        path: 用語集path。Noneなら用語集なし。

    Returns:
        原語が重複しない用語entry列。

    Raises:
        ValueError: 必須列、値、原語の一意性が不正な場合。
    """

    if path is None:
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = set(reader.fieldnames or [])
        if not {"source", "target"} <= fields:
            raise ValueError("glossary requires source and target columns")
        values = [
            GlossaryEntry(
                source=(row.get("source") or "").strip(),
                target=(row.get("target") or "").strip(),
                notes=(row.get("notes") or "").strip(),
            )
            for row in reader
        ]
    if any(not item.source or not item.target for item in values):
        raise ValueError("glossary source and target must not be empty")
    sources = [item.source.casefold() for item in values]
    if len(sources) != len(set(sources)):
        raise ValueError("glossary source values must be unique")
    return values


def protected_fragments(text: str) -> list[str]:
    """翻訳で変更してはならない文字列を抽出する。

    Args:
        text: 原文。

    Returns:
        出現順の保護文字列列。
    """

    return [match.group(0) for match in PROTECTED_RE.finditer(text)]


def deterministic_findings(
    source: str, target: str, glossary: list[GlossaryEntry]
) -> list[Finding]:
    """原文と訳文の機械的invariant違反を検出する。

    Args:
        source: 英語原文。
        target: 日本語訳文。
        glossary: 適用する用語集。

    Returns:
        検出したcritical finding列。
    """

    # heuristicは自動修正せずfindingだけを返し、最終判断をReview graphへ委ねる。
    findings: list[Finding] = []
    for value in NUMBER_UNIT_RE.findall(source):
        if value not in target:
            findings.append(
                Finding(
                    category="number-unit",
                    message=f"数値または単位が欠落: {value}",
                    source=value,
                )
            )
    for value in protected_fragments(source):
        if value not in target:
            findings.append(
                Finding(
                    category="protected",
                    message=f"保護文字列が欠落または変更: {value}",
                    source=value,
                )
            )
    if EN_NEGATION_RE.search(source) and not JA_NEGATION_RE.search(target):
        findings.append(
            Finding(category="negation", message="原文の否定表現を訳文で確認できない")
        )
    if EN_CONDITION_RE.search(source) and not JA_CONDITION_RE.search(target):
        findings.append(
            Finding(category="condition", message="原文の条件表現を訳文で確認できない")
        )
    if EN_COMPARISON_RE.search(source) and not JA_COMPARISON_RE.search(target):
        findings.append(
            Finding(category="comparison", message="原文の比較表現を訳文で確認できない")
        )
    if source.strip() and not target.strip():
        findings.append(Finding(category="omission", message="訳文が空である"))
    elif (
        source.strip() == target.strip()
        and len(re.findall(r"\b[A-Za-z][A-Za-z'-]*\b", source)) >= 3
    ):
        findings.append(
            Finding(
                category="untranslated",
                severity="major",
                message="複数語の英語原文が翻訳されていない",
            )
        )
    # 短い断片で誤検知しないよう、長さ比検査には最低文字数を設ける。
    elif len(source.strip()) >= 80 and len(target.strip()) < len(source.strip()) * 0.15:
        findings.append(
            Finding(
                category="omission",
                severity="major",
                message="訳文が原文に比べて極端に短い",
            )
        )
    if (
        len(target.strip()) >= 100
        and len(target.strip()) > max(1, len(source.strip())) * 5
    ):
        findings.append(
            Finding(
                category="addition",
                severity="major",
                message="訳文が原文に比べて極端に長い",
            )
        )
    for entry in glossary:
        if entry.source.casefold() in source.casefold() and entry.target not in target:
            findings.append(
                Finding(
                    category="glossary",
                    message=f"指定訳が使われていない: {entry.source} → {entry.target}",
                    source=entry.source,
                    suggestion=entry.target,
                )
            )
    return findings
