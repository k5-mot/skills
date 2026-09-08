#!/usr/bin/env python3
"""translate-ja-v3のLangGraphパイプラインをCLIから実行する。"""

from __future__ import annotations

import sys
from pathlib import Path
from time import perf_counter
from typing import Annotated

import typer

from translate_ja_v3 import PipelineOptions, TranslationBackend, run
from translate_ja_v3.io import LOGGER, configure_logging, load_environment

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
        LOGGER.error("translate-ja-v3 was interrupted")
        raise typer.Exit(code=130) from None
    except Exception as error:
        LOGGER.exception("translate-ja-v3 failed: %s", error)
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
