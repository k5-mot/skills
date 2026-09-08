"""全Stageを直列接続するLangGraphパイプラインを提供する。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from .config import (
    PipelineOptions,
    PipelineState,
    StagePaths,
    build_paths,
    initial_state,
    thread_id,
)
from .io import LOGGER
from .stages import (
    clean_stage,
    docx_stage,
    markdown_stage,
    normalize_stage,
    parse_stage,
    review_stage,
    structure_stage,
    translate_stage,
)

STAGES = (
    ("parse", parse_stage),
    ("normalize", normalize_stage),
    ("structure", structure_stage),
    ("clean", clean_stage),
    ("translate", translate_stage),
    ("review", review_stage),
    ("markdown", markdown_stage),
    ("docx", docx_stage),
)


def build_graph(
    checkpointer: Any | None = None, options: PipelineOptions | None = None
) -> Any:
    """Stageを順番に接続したLangGraphをcompileする。

    Args:
        checkpointer: 任意のLangGraph checkpointer。
        options: Stage retryを含むPipelineOptions。Noneなら既定値。

    Returns:
        compile済みLangGraph。
    """

    builder = StateGraph(cast(Any, PipelineState))
    values = options or PipelineOptions(input=Path("input.pdf"))
    retry = RetryPolicy(
        max_attempts=values.stage_max_attempts,
        initial_interval=values.stage_retry_initial_seconds,
        max_interval=values.stage_retry_max_seconds,
    )
    for name, action in STAGES:
        builder.add_node(name, action, retry_policy=retry)
    builder.add_edge(START, STAGES[0][0])
    for current, following in zip(STAGES, STAGES[1:]):
        builder.add_edge(current[0], following[0])
    builder.add_edge(STAGES[-1][0], END)
    return builder.compile(checkpointer=checkpointer)


def run(options: PipelineOptions) -> StagePaths:
    """永続checkpoint付きLangGraphで翻訳パイプラインを実行する。

    Args:
        options: 検証済みCLI設定。

    Returns:
        生成物の固定パス一覧。

    Side Effects:
        外部サービスを呼び出し、出力先へ成果物とcheckpointを保存する。
    """

    paths = build_paths(options)
    paths.output_dir.mkdir(parents=True, exist_ok=True)
    configuration = {
        "configurable": {"thread_id": thread_id(options, paths)},
        "recursion_limit": len(STAGES) + 4,
    }
    with SqliteSaver.from_conn_string(str(paths.checkpoints)) as checkpointer:
        graph = build_graph(checkpointer, options)
        graph.invoke(initial_state(options, paths), configuration)
    LOGGER.info("Pipeline completed output=%s", paths.output_dir)
    return paths


def graph_image(output: Path) -> None:
    """pipeline graphをMermaid PNGとして保存する。

    Args:
        output: PNG保存先。

    Returns:
        なし。

    Side Effects:
        LangGraphの描画APIを呼び、PNGファイルを保存する。
    """

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(build_graph().get_graph().draw_mermaid_png())
