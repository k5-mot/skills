"""翻訳内外で共有する直列LangGraph Reviewを提供する。"""

from __future__ import annotations

import json
from typing import Any, Literal, cast

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from src.adapters.langfuse import observed, update_current
from src.adapters.llm import structured_chat
from src.adapters.qdrant import search
from src.config import Settings
from src.processing.quality import Finding, GlossaryEntry, deterministic_findings


class ReviewOutcome(BaseModel):
    """共有Review graphの最終結果を表す。"""

    approved: bool
    text: str
    findings: list[Finding] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    verifications: int = 0


class ReviewState(TypedDict, total=False):
    """Review graph node間で共有する状態を表す。"""

    source: str
    original: str
    candidate: str
    rules: str
    glossary: list[dict[str, str]]
    findings: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    approved: bool
    verifications: int


FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "major", "minor"],
                    },
                    "message": {"type": "string"},
                    "source": {"type": "string"},
                    "evidence": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
                "required": [
                    "category",
                    "severity",
                    "message",
                    "source",
                    "evidence",
                    "suggestion",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}
REVISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}
VERIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "findings": FINDINGS_SCHEMA["properties"]["findings"],
    },
    "required": ["approved", "findings"],
    "additionalProperties": False,
}


def _parse_findings(value: Any) -> list[dict[str, Any]]:
    """LLM応答のfinding列を検証済みdictへ変換する。

    Args:
        value: JSONから得た任意値。

    Returns:
        Finding schemaに従うdict列。
    """

    if not isinstance(value, list):
        raise ValueError("review findings must be an array")
    return [Finding.model_validate(item).model_dump() for item in value]


def build_review_graph(settings: Settings) -> Any:
    """設定を閉じ込めた直列Review graphを構築する。

    Args:
        settings: Review modelと任意Qdrant設定。

    Returns:
        compile済みLangGraph。
    """

    if not settings.review_model:
        raise ValueError("OPENAI_REVIEW_MODEL is required")

    def checker(state: ReviewState) -> dict[str, Any]:
        """決定的invariant違反をfindingへ変換する。

        Args:
            state: Review対象状態。

        Returns:
            初期finding列。
        """

        glossary = [
            GlossaryEntry.model_validate(item) for item in state.get("glossary", [])
        ]
        findings = deterministic_findings(state["source"], state["candidate"], glossary)
        return {
            "findings": [item.model_dump() for item in findings],
            "evidence": [],
            "verifications": 0,
        }

    def fidelity(state: ReviewState) -> dict[str, Any]:
        """意味の忠実性だけをLLMで批評する。

        Args:
            state: 原文、訳文、既存findingを持つ状態。

        Returns:
            忠実性findingを追記した状態差分。
        """

        response = structured_chat(
            settings,
            settings.review_model or "",
            "忠実性Criticです。訳文は変更せず、意味、欠落、追加、否定、条件、比較、因果関係の問題だけを指摘してください。",
            f"Reviewルール:\n{state['rules']}\n\n原文:\n{state['source']}\n\n訳文:\n{state['candidate']}",
            "fidelity_findings",
            FINDINGS_SCHEMA,
        )
        return {
            "findings": state.get("findings", [])
            + _parse_findings(response.get("findings"))
        }

    def japanese(state: ReviewState) -> dict[str, Any]:
        """用語、一貫性、自然さを外部根拠付きで批評する。

        Args:
            state: 忠実性批評後の状態。

        Returns:
            日本語findingとRAG根拠を追記した状態差分。
        """

        evidence = search(settings, state["source"]) if settings.qdrant_enabled else []
        response = structured_chat(
            settings,
            settings.review_model or "",
            "Japanese Criticです。訳文は変更せず、用語、一貫性、自然さを指摘してください。参照がある場合はsourceを含む根拠をevidenceへ必ず示してください。",
            f"Reviewルール:\n{state['rules']}\n\n原文:\n{state['source']}\n\n訳文:\n{state['candidate']}\n\n参照:\n{json.dumps(evidence, ensure_ascii=False)}",
            "japanese_findings",
            FINDINGS_SCHEMA,
        )
        findings = _parse_findings(response.get("findings"))
        if evidence and any(not item.get("evidence") for item in findings):
            raise ValueError("Japanese Critic finding must cite RAG evidence")
        return {"findings": state.get("findings", []) + findings, "evidence": evidence}

    def revise(state: ReviewState) -> dict[str, Any]:
        """全findingに必要な最小修正だけを候補訳へ適用する。

        Args:
            state: findingと候補訳を持つ状態。

        Returns:
            更新した候補訳。
        """

        response = structured_chat(
            settings,
            settings.review_model or "",
            "Reviserです。指摘箇所だけを必要最小限に修正し、原文にない情報を追加しないでください。",
            f"原文:\n{state['source']}\n\n元訳:\n{state['original']}\n\n候補訳:\n{state['candidate']}\n\n指摘:\n{json.dumps(state.get('findings', []), ensure_ascii=False)}\n\n根拠:\n{json.dumps(state.get('evidence', []), ensure_ascii=False)}",
            "revision",
            REVISION_SCHEMA,
        )
        text = str(response.get("text", "")).strip()
        if not text:
            raise ValueError("Reviser returned empty text")
        return {"candidate": text}

    def verifier(state: ReviewState) -> dict[str, Any]:
        """原文、元訳、候補訳と根拠を比較して合否を決める。

        Args:
            state: 検証対象の全情報。

        Returns:
            合否、検証回数、追加finding。
        """

        response = structured_chat(
            settings,
            settings.review_model or "",
            "Verifierです。重大な誤訳、欠落、追加、未解決指摘が一つでもあればapproved=falseにしてください。",
            f"原文:\n{state['source']}\n\n元訳:\n{state['original']}\n\n候補訳:\n{state['candidate']}\n\n既存指摘:\n{json.dumps(state.get('findings', []), ensure_ascii=False)}\n\n参照根拠:\n{json.dumps(state.get('evidence', []), ensure_ascii=False)}",
            "verification",
            VERIFICATION_SCHEMA,
        )
        new_findings = _parse_findings(response.get("findings"))
        return {
            "approved": bool(response.get("approved")),
            "verifications": state.get("verifications", 0) + 1,
            "findings": state.get("findings", []) + new_findings,
        }

    def after_critics(state: ReviewState) -> Literal["revise", "verify"]:
        """findingの有無から最初の修正要否を選ぶ。

        Args:
            state: Critic完了状態。

        Returns:
            次node名。
        """

        return "revise" if state.get("findings") else "verify"

    def after_verifier(state: ReviewState) -> Literal["revise", "end"]:
        """Verifier合否と検証回数から一度だけの差戻しを選ぶ。

        Args:
            state: Verifier完了状態。

        Returns:
            再修正する場合は`revise`、終了する場合は`end`。
        """

        if state.get("approved") or state.get("verifications", 0) >= 2:
            return "end"
        return "revise"

    builder = StateGraph(cast(Any, ReviewState))
    builder.add_node(
        "checker", observed("review-checker", capture_input=False)(checker)
    )
    builder.add_node(
        "fidelity", observed("review-fidelity", capture_input=False)(fidelity)
    )
    builder.add_node(
        "japanese", observed("review-japanese", capture_input=False)(japanese)
    )
    builder.add_node("revise", observed("review-reviser", capture_input=False)(revise))
    builder.add_node(
        "verify", observed("review-verifier", capture_input=False)(verifier)
    )
    builder.add_edge(START, "checker")
    builder.add_edge("checker", "fidelity")
    builder.add_edge("fidelity", "japanese")
    builder.add_conditional_edges(
        "japanese", after_critics, {"revise": "revise", "verify": "verify"}
    )
    builder.add_edge("revise", "verify")
    builder.add_conditional_edges(
        "verify", after_verifier, {"revise": "revise", "end": END}
    )
    return builder.compile()


@observed("review", capture_input=False)
def run_review(
    source: str,
    target: str,
    rules: str,
    settings: Settings,
    glossary: list[GlossaryEntry] | None = None,
) -> ReviewOutcome:
    """共有Review graphを同時実行数1で実行する。

    Args:
        source: 英語原文。
        target: 日本語訳文。
        rules: Review専用Rules。
        settings: LLM、Langfuse、Qdrant設定。
        glossary: 任意の用語集。

    Returns:
        最終訳、finding、根拠を持つReview結果。

    Raises:
        RuntimeError: 2回目のVerifierも不合格の場合。
    """

    update_current(input={"source": source, "target": target, "rules": rules})
    state: ReviewState = {
        "source": source,
        "original": target,
        "candidate": target,
        "rules": rules,
        "glossary": [item.model_dump() for item in glossary or []],
        "findings": [],
        "evidence": [],
        "approved": False,
        "verifications": 0,
    }
    result = build_review_graph(settings).invoke(state, config={"max_concurrency": 1})
    outcome = ReviewOutcome(
        approved=bool(result.get("approved")),
        text=str(result.get("candidate", target)),
        findings=[Finding.model_validate(item) for item in result.get("findings", [])],
        evidence=list(result.get("evidence", [])),
        verifications=int(result.get("verifications", 0)),
    )
    update_current(output=outcome.model_dump())
    if not outcome.approved:
        raise RuntimeError("Verifier rejected the translation twice")
    return outcome
