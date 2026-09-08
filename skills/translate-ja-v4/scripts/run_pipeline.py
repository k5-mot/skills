#!/usr/bin/env python3
"""translate-ja-v4のLangGraphパイプラインをCLIから実行する。"""

from __future__ import annotations

import sys
from pathlib import Path
from time import perf_counter
from typing import Annotated

import typer

from translate_ja_v4 import PipelineOptions, TranslationBackend, run
from translate_ja_v4.io import LOGGER, configure_logging, load_environment

app = typer.Typer(
    add_completion=False,
    help="LangChain/LangGraph document translation pipeline",
)


@app.command()
def cli(
    input: Annotated[Path, typer.Option(help="PDF/Word input path")],
    output_dir: Annotated[
        Path | None, typer.Option(help="artifact output directory")
    ] = None,
    output: Annotated[Path | None, typer.Option(help="final docx output path")] = None,
    template: Annotated[Path | None, typer.Option(help="pandoc reference docx")] = None,
    env: Annotated[Path, typer.Option(help="dotenv path")] = Path(".env"),
    glossary: Annotated[
        Path | None, typer.Option(help="translation glossary CSV")
    ] = None,
    structure_rules: Annotated[
        Path | None, typer.Option(help="StructureStage rules file")
    ] = None,
    translation_rules: Annotated[
        Path | None, typer.Option(help="TranslateStage rules file")
    ] = None,
    review_rules: Annotated[
        Path | None, typer.Option(help="ReviewStage rules file")
    ] = None,
    translator: Annotated[
        TranslationBackend,
        typer.Option(help="Translate backend: default=LibreTranslate, llm=OpenAI"),
    ] = TranslationBackend.DEFAULT,
    context_chars: Annotated[
        int, typer.Option(min=1, help="maximum prompt characters")
    ] = 50_000,
    batch_chars: Annotated[
        int, typer.Option(min=1, help="maximum source characters per batch")
    ] = 20_000,
    max_batch_elements: Annotated[
        int, typer.Option(min=0, help="maximum elements; 0 means no element limit")
    ] = 0,
    request_timeout_seconds: Annotated[
        float,
        typer.Option(
            min=1, help="Docling, LLM, and translation request timeout seconds"
        ),
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
    skip_vlm: Annotated[
        bool, typer.Option(help="skip VLM structure correction")
    ] = False,
    skip_review: Annotated[bool, typer.Option(help="skip translation review")] = False,
    skip_docx: Annotated[
        bool, typer.Option(help="write Markdown and JSON only")
    ] = False,
    review_rag: Annotated[
        bool, typer.Option(help="use Qdrant RAG in ReviewStage")
    ] = False,
    force: Annotated[
        bool, typer.Option(help="rerun ParseStage even when cached")
    ] = False,
) -> None:
    """CLI引数を検証し、翻訳パイプラインを実行する。

    Args:
        input: PDFまたはWord入力。
        output_dir: 中間成果物の出力先。
        output: 最終docxの明示的な保存先。
        template: pandoc reference docx。
        env: dotenvファイル。
        glossary: 翻訳・Review用CSV用語集。
        structure_rules: StructureStage外部ルール。
        translation_rules: TranslateStage外部ルール。
        review_rules: ReviewStage外部ルール。
        translator: LibreTranslateまたはLLM backend。
        context_chars: prompt全体の最大文字数。
        batch_chars: batch内原文の最大文字数。
        max_batch_elements: batch内要素数上限。0は無制限。
        request_timeout_seconds: Docling、LLM、翻訳APIのtimeout秒数。
        max_retries: 初回失敗後のAPI最大再試行回数。
        retry_initial_seconds: API再試行の初期待機秒数。
        retry_max_seconds: API再試行の最大待機秒数。
        stage_max_attempts: 各LangGraph Stageの最大試行回数。
        stage_retry_initial_seconds: Stage再試行の初期待機秒数。
        stage_retry_max_seconds: Stage再試行の最大待機秒数。
        pdf_chunk_pages: Doclingへ1回に送るPDFページ数。
        skip_vlm: StructureStageのVLMを省略するか。
        skip_review: Review LLMを省略するか。
        skip_docx: docx生成を省略するか。
        review_rag: Qdrant RAGをReviewへ追加するか。
        force: ParseStage cacheを無視するか。

    Returns:
        なし。

    Side Effects:
        dotenvと外部サービスを利用し、出力ファイルを生成する。
    """

    options = PipelineOptions(
        input=input,
        output_dir=output_dir,
        output=output,
        template=template,
        env=env,
        glossary=glossary,
        structure_rules=structure_rules,
        translation_rules=translation_rules,
        review_rules=review_rules,
        translator=translator,
        context_chars=context_chars,
        batch_chars=batch_chars,
        max_batch_elements=max_batch_elements,
        request_timeout_seconds=request_timeout_seconds,
        max_retries=max_retries,
        retry_initial_seconds=retry_initial_seconds,
        retry_max_seconds=retry_max_seconds,
        stage_max_attempts=stage_max_attempts,
        stage_retry_initial_seconds=stage_retry_initial_seconds,
        stage_retry_max_seconds=stage_retry_max_seconds,
        pdf_chunk_pages=pdf_chunk_pages,
        skip_vlm=skip_vlm,
        skip_review=skip_review,
        skip_docx=skip_docx,
        review_rag=review_rag,
        force=force,
    )
    load_environment(options.env)
    configure_logging()
    try:
        paths = run(options)
    except KeyboardInterrupt:
        LOGGER.error("translate-ja-v4 was interrupted")
        raise typer.Exit(code=130) from None
    except Exception as error:
        LOGGER.exception("translate-ja-v4 failed: %s", error)
        raise typer.Exit(code=1) from None
    LOGGER.info("Outputs markdown=%s docx=%s", paths.markdown, paths.docx)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypointを実行して終了codeを返す。

    Args:
        argv: 引数列。Noneならsys.argvを使う。

    Returns:
        process終了code。

    Side Effects:
        CLIで指定されたパイプラインを実行する。
    """

    command = typer.main.get_command(app)
    try:
        result = command.main(args=argv, standalone_mode=False)
    except typer.Exit as error:
        return int(error.exit_code or 0)
    return int(result or 0)


if __name__ == "__main__":
    started_at = perf_counter()
    code = main()
    LOGGER.debug("Elapsed time %.3f seconds", perf_counter() - started_at)
    sys.exit(code)
