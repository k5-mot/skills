"""共有LangGraph Reviewの品質ゲートとRAG連携を検証する。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from src.config import Settings
from src.processing.quality import GlossaryEntry, deterministic_findings
from src.workflows import review


def _finding(category: str = "meaning", evidence: str = "") -> dict[str, str]:
    """LLM schemaに従うfindingを作る。

    Args:
        category: finding分類。
        evidence: 任意のRAG根拠。

    Returns:
        全必須fieldを持つfinding。
    """

    return {
        "category": category,
        "severity": "major",
        "message": "問題",
        "source": "source",
        "evidence": evidence,
        "suggestion": "修正",
    }


def test_deterministic_checker_finds_seeded_critical_errors() -> None:
    """数値、単位、否定、保護文字列、用語集違反をLLMなしで検出する。

    Returns:
        なし。
    """

    findings = deterministic_findings(
        "API must not call https://example.com after 50 ms.",
        "処理を呼び出します。",
        [GlossaryEntry(source="API", target="APIインターフェース")],
    )
    assert {item.category for item in findings} >= {
        "number-unit",
        "negation",
        "protected",
        "glossary",
    }


@pytest.mark.parametrize(
    ("source", "target", "category"),
    [
        ("If the value is valid, continue.", "値は有効です。", "condition"),
        ("The value must be greater than 10.", "値は10です。", "comparison"),
        ("A" * 100, "短い", "omission"),
        ("Short", "追加" * 100, "addition"),
    ],
)
def test_deterministic_checker_covers_accuracy_baseline(
    source: str, target: str, category: str
) -> None:
    """v4回帰対象の条件、比較、欠落、追加を決定的に検出する。

    Args:
        source: seed原文。
        target: seed誤訳。
        category: 期待finding分類。

    Returns:
        なし。
    """

    assert category in {
        item.category for item in deterministic_findings(source, target, [])
    }


def test_review_no_findings_goes_directly_to_verifier(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """二Criticに指摘がない場合Reviserを呼ばないことを確認する。

    Args:
        monkeypatch: LLM呼出しを差し替えるfixture。
        settings: 共通Settings fixture。

    Returns:
        なし。
    """

    calls: list[str] = []

    def chat(
        _settings: Settings,
        _model: str,
        _system: str,
        _user: str,
        schema_name: str,
        _schema: dict[str, Any],
        _image: object = None,
    ) -> dict[str, Any]:
        """node名に応じた合格応答を返す。

        Args:
            _settings: 未使用設定。
            _model: 未使用model。
            _system: 未使用system prompt。
            _user: 未使用user prompt。
            schema_name: node識別名。
            _schema: 未使用schema。
            _image: 未使用画像。

        Returns:
            CriticまたはVerifier応答。
        """

        calls.append(schema_name)
        return (
            {"approved": True, "findings": []}
            if schema_name == "verification"
            else {"findings": []}
        )

    monkeypatch.setattr(review, "structured_chat", chat)
    outcome = review.run_review("Source", "訳文", "rules", settings)
    assert outcome.approved
    assert calls == ["fidelity_findings", "japanese_findings", "verification"]


def test_review_revises_and_retries_verifier_once(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """finding後の初回不合格で一度だけ再修正して合格することを確認する。

    Args:
        monkeypatch: LLM呼出しを差し替えるfixture。
        settings: 共通Settings fixture。

    Returns:
        なし。
    """

    calls: list[str] = []

    def chat(
        _settings: Settings,
        _model: str,
        _system: str,
        _user: str,
        schema_name: str,
        _schema: dict[str, Any],
        _image: object = None,
    ) -> dict[str, Any]:
        """一回目Verifierだけ不合格にする。

        Args:
            _settings: 未使用設定。
            _model: 未使用model。
            _system: 未使用prompt。
            _user: 未使用prompt。
            schema_name: node識別名。
            _schema: 未使用schema。
            _image: 未使用画像。

        Returns:
            node別の模擬応答。
        """

        calls.append(schema_name)
        if schema_name == "fidelity_findings":
            return {"findings": [_finding()]}
        if schema_name == "japanese_findings":
            return {"findings": []}
        if schema_name == "revision":
            return {"text": f"修正版{calls.count('revision')}"}
        return {"approved": calls.count("verification") == 2, "findings": []}

    monkeypatch.setattr(review, "structured_chat", chat)
    outcome = review.run_review("Source", "訳文", "rules", settings)
    assert outcome.text == "修正版2"
    assert outcome.verifications == 2
    assert calls.count("revision") == 2


def test_review_rejects_after_second_verifier(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """二回目Verifier不合格で未検証訳を拒否することを確認する。

    Args:
        monkeypatch: LLM呼出しを差し替えるfixture。
        settings: 共通Settings fixture。

    Returns:
        なし。
    """

    def chat(
        _settings: Settings,
        _model: str,
        _system: str,
        _user: str,
        schema_name: str,
        _schema: dict[str, Any],
        _image: object = None,
    ) -> dict[str, Any]:
        """Verifierを常に不合格にする。

        Args:
            _settings: 未使用設定。
            _model: 未使用model。
            _system: 未使用prompt。
            _user: 未使用prompt。
            schema_name: node識別名。
            _schema: 未使用schema。
            _image: 未使用画像。

        Returns:
            node別の模擬応答。
        """

        if schema_name.endswith("findings"):
            return {"findings": []}
        if schema_name == "revision":
            return {"text": "修正版"}
        return {"approved": False, "findings": [_finding("omission")]}

    monkeypatch.setattr(review, "structured_chat", chat)
    with pytest.raises(RuntimeError, match="rejected"):
        review.run_review("Source", "訳文", "rules", settings)


def test_japanese_critic_propagates_rag_evidence(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """Qdrant本文とsource metadataがCriticからVerifierまで渡ることを確認する。

    Args:
        monkeypatch: 検索とLLMを差し替えるfixture。
        settings: 共通Settings fixture。

    Returns:
        なし。
    """

    configured = replace(
        settings, qdrant_url="http://qdrant", qdrant_collection="review"
    )
    evidence = [
        {"text": "用語根拠", "source": "guide.md", "revision": "abc", "score": 1.0}
    ]
    prompts: list[str] = []
    monkeypatch.setattr(review, "search", lambda *_args, **_kwargs: evidence)

    def chat(
        _settings: Settings,
        _model: str,
        _system: str,
        user: str,
        schema_name: str,
        _schema: dict[str, Any],
        _image: object = None,
    ) -> dict[str, Any]:
        """RAG引用付きfindingと合格を返す。

        Args:
            _settings: 未使用設定。
            _model: 未使用model。
            _system: 未使用prompt。
            user: 記録するuser prompt。
            schema_name: node識別名。
            _schema: 未使用schema。
            _image: 未使用画像。

        Returns:
            node別応答。
        """

        prompts.append(user)
        if schema_name == "japanese_findings":
            return {"findings": [_finding("terminology", "guide.md: 用語根拠")]}
        if schema_name == "revision":
            return {"text": "修正版"}
        if schema_name == "verification":
            return {"approved": True, "findings": []}
        return {"findings": []}

    monkeypatch.setattr(review, "structured_chat", chat)
    outcome = review.run_review("Source", "訳文", "rules", configured)
    assert outcome.evidence == evidence
    assert sum("guide.md" in prompt for prompt in prompts) >= 3
