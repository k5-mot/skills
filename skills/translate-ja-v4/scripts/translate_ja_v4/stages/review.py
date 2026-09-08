"""ReviewStageをLangGraph multi-agent subgraphで実装する。"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Iterator
from typing import Any, cast

from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from langchain_qdrant import QdrantVectorStore
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, SecretStr
from qdrant_client import QdrantClient
from typing_extensions import TypedDict

from ..config import PipelineOptions, PipelineState, state_options, state_paths
from ..document import resolve_target, translation_targets
from ..io import (
    LOGGER,
    glossary_matches,
    hash_file,
    hash_json,
    read_glossary,
    read_json,
    read_rules,
    record_stage,
    stage_cached,
    stage_partial,
    write_json,
)
from ..llm import llm_batches, prompt_runnable

DEFAULT_RULES = "- 日本語訳をレビューする。\n- 指定された外部Reviewルールに従う。"


class ReviewItem(BaseModel):
    """ReviewerまたはAdjudicatorの1要素分の回答を表す。"""

    id: str
    reviewed_text: str
    reason: str = ""


class ReviewResponse(BaseModel):
    """Review agentのstructured output schema。"""

    reviews: list[ReviewItem] = Field(default_factory=list)


class ReviewBatchState(TypedDict, total=False):
    """Review subgraph内で共有するbatch state。"""

    options: dict[str, Any]
    items: list[dict[str, Any]]
    rules: str
    fidelity: dict[str, dict[str, str]]
    terminology: dict[str, dict[str, str]]
    final: dict[str, dict[str, str]]


def _reviewer(
    state: ReviewBatchState, role: str
) -> dict[str, dict[str, dict[str, str]]]:
    """指定観点のReviewerをLangChainで実行する。

    Args:
        state: Review対象と設定を含むsubgraph state。
        role: fidelityまたはterminology。

    Returns:
        role名をkeyにしたReview案。
    """

    options = PipelineOptions.model_validate(state["options"])
    chain = prompt_runnable(
        options,
        ReviewResponse,
        (
            f"{role} Reviewerとしてreviews配列を持つobjectで回答してください。"
            "各入力IDを1回ずつ含め、reviewed_textに最終訳、reasonに理由を設定してください。"
        ),
        "Reviewルール:\n{rules}\n\n入力JSON:\n{items}",
        max_tokens=16_384,
    )
    response = chain.invoke(
        {
            "rules": state["rules"],
            "items": json.dumps(state["items"], ensure_ascii=False),
        }
    )
    values = {
        item.id: {"text": item.reviewed_text.strip(), "reason": item.reason}
        for item in response.reviews
    }
    expected = {str(item["id"]) for item in state["items"]}
    if set(values) != expected or any(not item["text"] for item in values.values()):
        raise ValueError(f"{role} Review IDs must exactly match the input")
    return {role: values}


def _fidelity(state: ReviewBatchState) -> dict[str, Any]:
    """忠実性Reviewer nodeを実行する。

    Args:
        state: Review batch state。

    Returns:
        fidelity Review案。
    """

    return _reviewer(state, "fidelity")


def _terminology(state: ReviewBatchState) -> dict[str, Any]:
    """用語Reviewer nodeを実行する。

    Args:
        state: Review batch state。

    Returns:
        terminology Review案。
    """

    return _reviewer(state, "terminology")


def _adjudicate(state: ReviewBatchState) -> dict[str, Any]:
    """二つのReview案を比較し、不一致だけをLLMで裁定する。

    Args:
        state: 両Reviewerの提案を含むbatch state。

    Returns:
        全IDの最終訳。
    """

    final: dict[str, dict[str, str]] = {}
    disputes: list[dict[str, Any]] = []
    source_by_id = {str(item["id"]): item for item in state["items"]}
    for item_id, fidelity in state["fidelity"].items():
        terminology = state["terminology"][item_id]
        if fidelity["text"] == terminology["text"]:
            final[item_id] = fidelity
        else:
            disputes.append(
                {
                    **source_by_id[item_id],
                    "fidelity": fidelity,
                    "terminology": terminology,
                }
            )
    if disputes:
        options = PipelineOptions.model_validate(state["options"])
        chain = prompt_runnable(
            options,
            ReviewResponse,
            (
                "翻訳ReviewのAdjudicatorとしてreviews配列を持つobjectで回答してください。"
                "各入力IDを1回ずつ含め、reviewed_textに最終訳、reasonに理由を設定してください。"
            ),
            "Reviewルール:\n{rules}\n\n競合JSON:\n{items}",
            max_tokens=16_384,
        )
        response = chain.invoke(
            {"rules": state["rules"], "items": json.dumps(disputes, ensure_ascii=False)}
        )
        decided = {
            item.id: {"text": item.reviewed_text.strip(), "reason": item.reason}
            for item in response.reviews
        }
        if set(decided) != {str(item["id"]) for item in disputes}:
            raise ValueError("Adjudicator IDs must exactly match disputes")
        final.update(decided)
    return {"final": final}


def _review_graph() -> Any:
    """FidelityとTerminologyを並列実行するReview subgraphを作る。

    Returns:
        compile済みLangGraph。
    """

    builder = StateGraph(cast(Any, ReviewBatchState))
    builder.add_node("fidelity", _fidelity)
    builder.add_node("terminology", _terminology)
    builder.add_node("adjudicate", _adjudicate)
    builder.add_edge(START, "fidelity")
    builder.add_edge(START, "terminology")
    builder.add_edge("fidelity", "adjudicate")
    builder.add_edge("terminology", "adjudicate")
    builder.add_edge("adjudicate", END)
    return builder.compile()


def _retriever() -> Any:
    """環境変数からLangChain Qdrant retrieverを作る。

    Returns:
        設定済みVectorStoreRetriever。

    Raises:
        RuntimeError: Qdrantまたはembedding設定が不足する場合。
    """

    url, key = (
        os.getenv("QDRANT_URI") or os.getenv("QDRANT_URL"),
        os.getenv("QDRANT_API_KEY"),
    )
    if not url or not key:
        raise RuntimeError("QDRANT_URI and QDRANT_API_KEY are required")
    client = QdrantClient(url=url, api_key=key)
    collection = os.getenv("QDRANT_COLLECTION")
    if not collection:
        names = [item.name for item in client.get_collections().collections]
        if len(names) != 1:
            raise RuntimeError(
                "QDRANT_COLLECTION is required unless exactly one collection exists"
            )
        collection = names[0]
    embeddings = OpenAIEmbeddings(
        model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
        base_url=os.getenv("OPENAI_BASE_URL"),
        api_key=SecretStr(os.getenv("OPENAI_API_KEY", "")),
    )
    store = QdrantVectorStore(
        client, collection, embeddings, vector_name=os.getenv("QDRANT_VECTOR_NAME", "")
    )
    return store.as_retriever(search_kwargs={"k": int(os.getenv("QDRANT_TOP_K", "3"))})


def _evidence(
    retriever: Any, targets: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Review対象をLangChain retrieverでbatch検索する。

    Args:
        retriever: Qdrant VectorStoreRetriever。
        targets: Review対象配列。

    Returns:
        IDごとの出典付き根拠。
    """

    documents: list[list[Document]] = retriever.batch(
        [str(item["source"]) for item in targets]
    )
    return {
        str(target["id"]): [
            {"text": doc.page_content[:800], "metadata": doc.metadata} for doc in docs
        ]
        for target, docs in zip(targets, documents, strict=True)
    }


def _reviewed(document: dict[str, Any], target: dict[str, Any]) -> bool:
    """対象がReview済みか判定する。

    Args:
        document: Review部分成果物。
        target: Review対象。

    Returns:
        review_ja_v4 metadataがあればTrue。
    """

    meta = resolve_target(document, target["path"]).get("translate_ja_v4", {})
    return "review_ja_v4" in meta


def _apply_review(
    document: dict[str, Any], target: dict[str, Any], result: dict[str, str]
) -> None:
    """Review結果を翻訳metadataへ反映する。

    Args:
        document: 更新対象JSON。
        target: Review対象。
        result: textとreasonを持つ最終案。

    Returns:
        なし。

    Side Effects:
        translate_ja_v4 metadataを更新する。
    """

    item = resolve_target(document, target["path"])
    meta = item["translate_ja_v4"]
    translated = result["text"] or str(meta["text_ja"])
    if len(translated) > max(200, len(str(meta["text_ja"])) * 2):
        translated = str(meta["text_ja"])
    meta["text_ja"] = translated
    meta["render_text"] = (
        f"{target['source']} / {translated}"
        if target["kind"] == "heading"
        else translated
    )
    meta["review_ja_v4"] = {"reason": result["reason"]}


def _run_with_fallback(
    graph: Any,
    options: PipelineOptions,
    rules: str,
    glossary: list[dict[str, str]],
    retriever: Any,
    document: dict[str, Any],
    batch: list[dict[str, Any]],
) -> Iterator[tuple[list[dict[str, Any]], dict[str, dict[str, str]]]]:
    """Review失敗時にbatchを半減して成功結果を順次返す。

    Args:
        graph: compile済みReview subgraph。
        options: LLMとcontext設定。
        rules: Review外部ルール。
        glossary: 全用語集。
        retriever: 任意のRAG retriever。
        document: 翻訳metadataを持つ文書。
        batch: Review対象batch。

    Yields:
        成功したsub-batchと最終Review結果。

    Raises:
        Exception: 1要素でもReview subgraphが失敗した場合。
    """

    try:
        evidence = _evidence(retriever, batch) if retriever else {}
        items = [
            {
                "id": target["id"],
                "source_text": target["source"],
                "translated_text": resolve_target(document, target["path"])[
                    "translate_ja_v4"
                ]["text_ja"],
                "glossary": glossary_matches(str(target["source"]), glossary),
                "rag_evidence": evidence.get(str(target["id"]), []),
            }
            for target in batch
        ]
        encoded = json.dumps(items, ensure_ascii=False)
        if len(rules) + len(encoded) + 2_000 > options.context_chars:
            raise ValueError("ReviewStage prompt exceeds context_chars")
        result = graph.invoke(
            {"options": options.model_dump(mode="json"), "items": items, "rules": rules}
        )["final"]
        yield batch, result
    except Exception:
        if len(batch) == 1:
            raise
        middle = len(batch) // 2
        LOGGER.warning(
            "ReviewStage batch failed; retrying with smaller batches elements=%s->%s",
            len(batch),
            middle,
        )
        yield from _run_with_fallback(
            graph, options, rules, glossary, retriever, document, batch[:middle]
        )
        yield from _run_with_fallback(
            graph, options, rules, glossary, retriever, document, batch[middle:]
        )


def review_stage(state: PipelineState) -> PipelineState:
    """multi-agent subgraphで翻訳をレビューするLangGraph node。

    Args:
        state: Translate成果物を含むgraph state。

    Returns:
        Review成果物パスを設定した部分state。
    """

    options, paths = state_options(state), state_paths(state)
    input_hash = hash_file(paths.translated_json)
    rules, glossary = (
        read_rules(options.review_rules, DEFAULT_RULES),
        read_glossary(options.glossary),
    )
    config_hash = hash_json(
        {
            "version": 2,
            "skip": options.skip_review,
            "rules": rules,
            "glossary": glossary,
            "rag": options.review_rag,
            "context_chars": options.context_chars,
            "batch_chars": options.batch_chars,
            "max_elements": options.max_batch_elements,
            "max_output_tokens": options.max_output_tokens,
            "model": os.getenv("OPENAI_MODEL"),
            "collection": os.getenv("QDRANT_COLLECTION")
            if options.review_rag
            else None,
        }
    )
    if options.skip_review:
        document = read_json(paths.translated_json)
        write_json(paths.reviewed_json, document)
        record_stage(
            paths.manifest,
            "review",
            "skipped",
            input_hash,
            config_hash,
            paths.reviewed_json,
        )
        return {"current_path": str(paths.reviewed_json), "completed_stage": "review"}
    if stage_cached(
        paths.manifest, "review", input_hash, config_hash, paths.reviewed_json
    ):
        LOGGER.info("Resumed ReviewStage output=%s", paths.reviewed_json)
        return {"current_path": str(paths.reviewed_json), "completed_stage": "review"}
    source = read_json(paths.translated_json)
    document = (
        read_json(paths.reviewed_json)
        if stage_partial(
            paths.manifest, "review", input_hash, config_hash, paths.reviewed_json
        )
        else copy.deepcopy(source)
    )
    targets = translation_targets(source)
    pending = [target for target in targets if not _reviewed(document, target)]
    record_stage(
        paths.manifest,
        "review",
        "running",
        input_hash,
        config_hash,
        paths.reviewed_json,
        {"total": len(targets), "completed": len(targets) - len(pending)},
    )
    retriever = _retriever() if options.review_rag else None
    graph = _review_graph()
    prompt_budget = max(1, (options.context_chars - len(rules) - 2_000) // 2)
    estimated = [
        {
            **target,
            "_output_text": resolve_target(document, target["path"])["translate_ja_v4"][
                "text_ja"
            ],
        }
        for target in pending
    ]
    for batch in llm_batches(
        estimated,
        min(options.batch_chars, prompt_budget),
        options.max_batch_elements,
        options.max_output_tokens,
        text_key="_output_text",
        expansion=1.1,
        per_element_tokens=120,
    ):
        for completed_batch, result in _run_with_fallback(
            graph, options, rules, glossary, retriever, document, batch
        ):
            for target in completed_batch:
                _apply_review(document, target, result[str(target["id"])])
            write_json(paths.reviewed_json, document)
            record_stage(
                paths.manifest,
                "review",
                "running",
                input_hash,
                config_hash,
                paths.reviewed_json,
                {
                    "total": len(targets),
                    "completed": sum(_reviewed(document, target) for target in targets),
                },
            )
    write_json(paths.reviewed_json, document)
    record_stage(
        paths.manifest,
        "review",
        "completed",
        input_hash,
        config_hash,
        paths.reviewed_json,
        {"total": len(targets), "completed": len(targets)},
    )
    return {"current_path": str(paths.reviewed_json), "completed_stage": "review"}
