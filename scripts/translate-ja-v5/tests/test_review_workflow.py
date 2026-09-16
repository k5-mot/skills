"""共有LangGraph Reviewの品質ゲートとRAG連携を検証する。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
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


def test_number_checker_ignores_source_punctuation() -> None:
    """数値直後の英文区切り記号を数値の一部として比較しない。

    Returns:
        なし。
    """

    findings = deterministic_findings(
        "CJCS Instruction 3110.01, issued in 2018.",
        "CJCS指示3110.01、2018年発行。",
        [],
    )
    assert not [item for item in findings if item.category == "number-unit"]


def test_accuracy_regression_is_no_worse_than_retained_v4_baseline(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """全critical種別を検出し、保持したv4基準集合を下回らないことを確認する。

    Args:
        monkeypatch: 意味判定を行うlocal CriticへLLMを差し替えるfixture。
        settings: 共通Settings fixture。

    Returns:
        なし。
    """

    fixture_path = Path(__file__).parent / "fixtures" / "accuracy-v4-baseline.json"
    baseline = json.loads(fixture_path.read_text(encoding="utf-8"))
    detected: set[str] = set()
    semantic_category = ""

    def chat(
        _settings: Settings,
        _model: str,
        _system: str,
        _user: str,
        schema_name: str,
        _schema: dict[str, Any],
        _image: object = None,
    ) -> dict[str, Any]:
        """意味・因果誤りを模擬Criticで検出して修正を承認する。

        Args:
            _settings: 未使用設定。
            _model: 未使用model。
            _system: 未使用system prompt。
            _user: 未使用user prompt。
            schema_name: Review node名。
            _schema: 未使用schema。
            _image: 未使用画像。

        Returns:
            node schemaに対応するlocal応答。
        """

        if schema_name == "fidelity_findings":
            return {"findings": [_finding(semantic_category)]}
        if schema_name == "japanese_findings":
            return {"findings": []}
        if schema_name == "revision":
            return {"text": "修正済み訳文"}
        return {"approved": True, "findings": []}

    monkeypatch.setattr(review, "structured_chat", chat)
    for case in baseline["cases"]:
        glossary = (
            [GlossaryEntry.model_validate(case["glossary"])]
            if "glossary" in case
            else []
        )
        findings = deterministic_findings(case["source"], case["target"], glossary)
        if any(item.category == case["finding"] for item in findings):
            detected.add(case["category"])
            continue
        semantic_category = case["finding"]
        outcome = review.run_review(
            case["source"], case["target"], "rules", settings, glossary
        )
        if any(item.category == case["finding"] for item in outcome.findings):
            detected.add(case["category"])

    required = {case["category"] for case in baseline["cases"]}
    assert required == set(baseline["v4_detected"])
    assert detected >= set(baseline["v4_detected"])


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
    revision_prompts: list[str] = []

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
            return {"findings": [_finding("critic-meaning")]}
        if schema_name == "japanese_findings":
            return {"findings": []}
        if schema_name == "revision":
            revision_prompts.append(_user)
            return {"text": f"修正版{calls.count('revision')}"}
        return {
            "approved": calls.count("verification") == 2,
            "findings": []
            if calls.count("verification") == 2
            else [_finding("verifier-omission")],
        }

    monkeypatch.setattr(review, "structured_chat", chat)
    outcome = review.run_review("Source", "訳文", "rules", settings)
    assert outcome.text == "修正版2"
    assert outcome.verifications == 2
    assert calls.count("revision") == 2
    assert "critic-meaning" in revision_prompts[0]
    assert "critic-meaning" not in revision_prompts[1]
    assert "verifier-omission" in revision_prompts[1]
    assert {item.category for item in outcome.findings} == {
        "critic-meaning",
        "verifier-omission",
    }


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


def test_review_limits_assessment_output_but_not_revision(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """CriticとVerifierだけ出力上限を絞りReviserの長文枠を保つ。

    Args:
        monkeypatch: LLM呼出しを差し替えるfixture。
        settings: 共通Settings fixture。

    Returns:
        なし。
    """

    limits: dict[str, int] = {}
    prompts: dict[str, str] = {}
    systems: dict[str, str] = {}

    def chat(
        call_settings: Settings,
        _model: str,
        system: str,
        user: str,
        schema_name: str,
        _schema: dict[str, Any],
        _image: object = None,
    ) -> dict[str, Any]:
        """各Review nodeの出力上限とpromptを記録する。

        Args:
            call_settings: nodeへ渡された設定。
            _model: 未使用model。
            system: 記録するsystem prompt。
            user: 記録するuser prompt。
            schema_name: node識別名。
            _schema: 未使用schema。
            _image: 未使用画像。

        Returns:
            修正後に合格する模擬応答。
        """

        limits[schema_name] = call_settings.output_tokens
        prompts[schema_name] = user
        systems[schema_name] = system
        if schema_name == "fidelity_findings":
            return {"findings": [_finding()]}
        if schema_name == "japanese_findings":
            return {"findings": []}
        if schema_name == "revision":
            return {"text": "修正版"}
        return {"approved": True, "findings": []}

    monkeypatch.setattr(review, "structured_chat", chat)
    review.run_review(
        "warning order community", "警告命令コミュニティ", "固有ルール", settings
    )
    assert limits == {
        "fidelity_findings": 2_048,
        "japanese_findings": 2_048,
        "revision": settings.output_tokens,
        "verification": 2_048,
    }
    assert "固有ルール" in prompts["revision"]
    assert "warning order community" in prompts["verification"]
    assert "原文末尾より後を推測して欠落と判定せず" in systems["japanese_findings"]
    for schema_name in ("japanese_findings", "revision", "verification"):
        assert "参照文書は訳語選択の参考に限り" in systems[schema_name]
        assert "短い図表ラベルや用語" in systems[schema_name]
        assert "定義、関係者、背景説明を補うことを要求しない" in systems[schema_name]
    for schema_name in ("fidelity_findings", "japanese_findings", "verification"):
        assert "日本語の語順上完結して見えるだけ" in systems[schema_name]
    for schema_name in (
        "fidelity_findings",
        "japanese_findings",
        "revision",
        "verification",
    ):
        assert "括弧の欠落や不整合" in systems[schema_name]
        assert "原文と同じ壊れた記号列へ戻すよう要求しない" in systems[schema_name]


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
