#!/usr/bin/env python3
"""review-enjaのLangGraph PipelineをCLIから実行する。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from time import perf_counter
from typing import Annotated

import typer

from review_enja import ReviewOptions, run

app = typer.Typer(
    add_completion=False,
    help="Review a Japanese PDF translation against its English source PDF",
)


@app.command()
def cli(
    source: Annotated[Path, typer.Option(help="English source PDF path")],
    translation: Annotated[Path, typer.Option(help="Japanese translation PDF path")],
    output_dir: Annotated[Path, typer.Option(help="review output directory")],
    env: Annotated[Path, typer.Option(help="dotenv path")] = Path(".env"),
    glossary: Annotated[Path | None, typer.Option(help="review glossary CSV")] = None,
    review_rules: Annotated[
        Path | None, typer.Option(help="external review rules file")
    ] = None,
    context_chars: Annotated[
        int, typer.Option(min=1, help="maximum prompt characters")
    ] = 50_000,
    batch_chars: Annotated[
        int,
        typer.Option(min=1, help="maximum source and translation characters per batch"),
    ] = 20_000,
    max_batch_elements: Annotated[
        int,
        typer.Option(min=0, help="maximum combined elements; 0 means no element limit"),
    ] = 0,
    max_output_tokens: Annotated[
        int, typer.Option(min=256, help="maximum estimated LLM output tokens")
    ] = 16_384,
    request_timeout_seconds: Annotated[
        float,
        typer.Option(min=1, help="Docling, LLM, and RAG request timeout seconds"),
    ] = 1_800,
    max_retries: Annotated[
        int, typer.Option(min=0, help="maximum API retries after the first attempt")
    ] = 5,
    retry_initial_seconds: Annotated[
        float, typer.Option(min=0, help="initial API retry delay seconds")
    ] = 1,
    retry_max_seconds: Annotated[
        float, typer.Option(min=0, help="maximum API retry delay seconds")
    ] = 30,
    stage_max_attempts: Annotated[
        int, typer.Option(min=1, help="maximum LangGraph attempts per stage")
    ] = 2,
    stage_retry_initial_seconds: Annotated[
        float, typer.Option(min=0.001, help="initial stage retry delay seconds")
    ] = 1,
    stage_retry_max_seconds: Annotated[
        float, typer.Option(min=0.001, help="maximum stage retry delay seconds")
    ] = 8,
    pdf_chunk_pages: Annotated[
        int, typer.Option(min=1, help="PDF pages sent per Docling request")
    ] = 10,
    review_rag: Annotated[
        bool, typer.Option(help="use Qdrant RAG during review")
    ] = False,
    force: Annotated[
        bool, typer.Option(help="rerun PDF parsing even when cached")
    ] = False,
) -> None:
    """CLI引数を検証し、英日翻訳Review Pipelineを実行する。

    Args:
        source: 英語原文PDF。
        translation: 日本語翻訳PDF。
        output_dir: 全成果物の出力directory。
        env: dotenvファイル。
        glossary: Review用CSV用語集。
        review_rules: ReviewStageの外部ルール。
        context_chars: prompt全体の最大文字数。
        batch_chars: batch内の英日合計最大文字数。
        max_batch_elements: batch内の英日合計要素数上限。0は無制限。
        max_output_tokens: LLM応答の見積りおよびAPI上限token数。
        request_timeout_seconds: Docling、LLM、RAG APIのtimeout秒数。
        max_retries: 初回失敗後のAPI最大再試行回数。
        retry_initial_seconds: API再試行の初期待機秒数。
        retry_max_seconds: API再試行の最大待機秒数。
        stage_max_attempts: 各LangGraph Stageの最大試行回数。
        stage_retry_initial_seconds: Stage再試行の初期待機秒数。
        stage_retry_max_seconds: Stage再試行の最大待機秒数。
        pdf_chunk_pages: Doclingへ1回に送るPDFページ数。
        review_rag: Qdrant RAGをReviewへ追加するか。
        force: ParseStage cacheを無視するか。

    Returns:
        なし。

    Side Effects:
        外部サービスを利用し、Review成果物を生成する。
    """

    try:
        paths = run(
            ReviewOptions(
                source=source,
                translation=translation,
                output_dir=output_dir,
                env=env,
                glossary=glossary,
                review_rules=review_rules,
                context_chars=context_chars,
                batch_chars=batch_chars,
                max_batch_elements=max_batch_elements,
                max_output_tokens=max_output_tokens,
                request_timeout_seconds=request_timeout_seconds,
                max_retries=max_retries,
                retry_initial_seconds=retry_initial_seconds,
                retry_max_seconds=retry_max_seconds,
                stage_max_attempts=stage_max_attempts,
                stage_retry_initial_seconds=stage_retry_initial_seconds,
                stage_retry_max_seconds=stage_retry_max_seconds,
                pdf_chunk_pages=pdf_chunk_pages,
                review_rag=review_rag,
                force=force,
            )
        )
    except KeyboardInterrupt:
        logging.getLogger("review-enja").error("review-enja was interrupted")
        raise typer.Exit(code=130) from None
    except Exception as error:
        logging.getLogger("review-enja").exception("review-enja failed: %s", error)
        raise typer.Exit(code=1) from None
    typer.echo(f"JSON: {paths.reviewed_json}")
    typer.echo(f"Markdown: {paths.report}")


def main(argv: list[str] | None = None) -> int:
    """CLI entrypointを実行して終了codeを返す。

    Args:
        argv: 引数列。Noneならsys.argvを使う。

    Returns:
        process終了code。
    """

    command = typer.main.get_command(app)
    try:
        result = command.main(args=argv, standalone_mode=False)
    except typer.Exit as error:
        return int(error.exit_code or 0)
    return int(result or 0)


if __name__ == "__main__":
    started_at = perf_counter()
    exit_code = main()
    logging.getLogger("review-enja").debug(
        "Elapsed time %.3f seconds", perf_counter() - started_at
    )
    sys.exit(exit_code)
