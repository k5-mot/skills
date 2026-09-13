#!/usr/bin/env python3
"""PDF翻訳、比較Review、参照登録の三つだけを公開するCLI。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from src.config import Backend, load_settings
from src.workflows.compare import run_compare_review
from src.workflows.register import register_documents
from src.workflows.translate import run_translation

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="英語PDFを日本語化するユーティリティ",
)


@app.command()
def translate(
    source: Annotated[
        Path, typer.Option("--source", exists=True, dir_okay=False, help="英語PDF")
    ],
    output_dir: Annotated[Path, typer.Option("--output-dir", help="成果物root")] = Path(
        "output"
    ),
    backend: Annotated[
        Backend, typer.Option("--backend", help="翻訳backend")
    ] = "openai",
    glossary: Annotated[
        Path | None,
        typer.Option(
            "--glossary",
            exists=True,
            dir_okay=False,
            help="source,target列を持つUTF-8 CSV",
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="APIやfile変更なしで再利用計画を表示")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="内部成果物を無視してParseから再実行")
    ] = False,
) -> None:
    """PDFの全翻訳工程を実行して日本語DOCXを生成する。

    Args:
        source: 英語PDF。
        output_dir: source stem directoryを作るroot。
        backend: OpenAI互換APIまたはLibreTranslate。
        glossary: 任意のv5用語集。
        dry_run: 実行計画だけを表示するか。
        force: 全内部成果物を再構築するか。

    Returns:
        なし。
    """

    settings = load_settings("translate", backend)
    result = run_translation(
        source, output_dir, settings, backend, glossary, dry_run, force
    )
    if result is not None:
        typer.echo(result)


@app.command()
def review(
    source: Annotated[
        Path, typer.Option("--source", exists=True, dir_okay=False, help="英語PDF")
    ],
    destination: Annotated[
        Path,
        typer.Option("--destination", exists=True, dir_okay=False, help="日本語PDF"),
    ],
    output: Annotated[Path, typer.Option("--output", help="Review Markdown")] = Path(
        "review.md"
    ),
    force: Annotated[
        bool, typer.Option("--force", help="既存Review成果物を無視して再実行")
    ] = False,
) -> None:
    """英語PDFと日本語PDFを比較してReview Markdownを生成する。

    Args:
        source: 英語PDF。
        destination: 日本語PDF。
        output: Review Markdown保存先。
        force: 既存内部成果物を再構築するか。

    Returns:
        なし。
    """

    settings = load_settings("review")
    typer.echo(run_compare_review(source, destination, output, settings, force))


@app.command()
def register(
    doc_path: Annotated[
        Path | None,
        typer.Option(
            "--doc-path", exists=True, dir_okay=False, help="登録する単一文書"
        ),
    ] = None,
    doc_dir: Annotated[
        Path | None,
        typer.Option(
            "--doc-dir", exists=True, file_okay=False, help="再帰登録するdirectory"
        ),
    ] = None,
) -> None:
    """Review参照文書を既存Qdrant collectionへ登録する。

    Args:
        doc_path: 単一文書。
        doc_dir: 再帰探索するdirectory。

    Returns:
        なし。
    """

    settings = load_settings("register")
    typer.echo(f"registered {register_documents(settings, doc_path, doc_dir)} points")


if __name__ == "__main__":
    app()
