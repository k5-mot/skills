#!/usr/bin/env python3
"""ReviewStageのRAG文書をQdrantへ登録する。"""

from __future__ import annotations

import hashlib
import os
import sys
import zipfile
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any
from uuid import NAMESPACE_URL, uuid5
from xml.etree import ElementTree as ET

import pypdfium2 as pdfium
import typer
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from langchain_qdrant import QdrantVectorStore
from pydantic import SecretStr
from qdrant_client import models

from translate_ja_v3.io import LOGGER, configure_logging, load_environment

TEXT_SUFFIXES = {
    ".csv",
    ".html",
    ".json",
    ".jsonl",
    ".md",
    ".rst",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | {".docx", ".dotx", ".pdf"}
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

app = typer.Typer(add_completion=False, help="Ingest RAG documents into Qdrant")


def collect_files(inputs: list[Path]) -> list[Path]:
    """入力fileとdirectoryから対応fileを重複なく収集する。

    Args:
        inputs: fileまたはdirectoryの一覧。

    Returns:
        resolve済みfileのsort済み一覧。

    Raises:
        FileNotFoundError: 入力が存在しない場合。
        ValueError: 対応fileが見つからない場合。
    """

    files: set[Path] = set()
    for value in inputs:
        path = value.resolve()
        if not path.exists():
            raise FileNotFoundError(f"Input does not exist: {path}")
        candidates = [path] if path.is_file() else path.rglob("*")
        files.update(
            candidate
            for candidate in candidates
            if candidate.is_file() and candidate.suffix.lower() in SUPPORTED_SUFFIXES
        )
    if not files:
        raise ValueError("No supported documents found")
    return sorted(files)


def split_text(text: str, chunk_chars: int, overlap_chars: int) -> list[str]:
    """文字列を固定文字数とoverlapで空でないchunkへ分割する。

    Args:
        text: 分割対象文字列。
        chunk_chars: 1 chunkの最大文字数。
        overlap_chars: 隣接chunkへ重ねる文字数。

    Returns:
        前後空白を除いたchunk一覧。

    Raises:
        ValueError: chunkとoverlapの関係が不正な場合。
    """

    if chunk_chars < 1 or overlap_chars < 0 or overlap_chars >= chunk_chars:
        raise ValueError("chunk_chars must be positive and greater than overlap_chars")
    normalized = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    if not normalized:
        return []
    step = chunk_chars - overlap_chars
    return [
        normalized[start : start + chunk_chars].strip()
        for start in range(0, len(normalized), step)
        if normalized[start : start + chunk_chars].strip()
    ]


def _pdf_units(path: Path) -> list[tuple[str, dict[str, Any]]]:
    """PDFからpage単位のtextとmetadataを抽出する。

    Args:
        path: PDF file。

    Returns:
        page textと1始まりpage番号の組。
    """

    units: list[tuple[str, dict[str, Any]]] = []
    with pdfium.PdfDocument(path) as pdf:
        for index in range(len(pdf)):
            page = pdf[index]
            try:
                text_page = page.get_textpage()
                try:
                    text = text_page.get_text_range()
                finally:
                    text_page.close()
            finally:
                page.close()
            units.append((text, {"page": index + 1}))
    return units


def _word_text(path: Path) -> str:
    """DOCX/DOTXの段落と表セルからtextを抽出する。

    Args:
        path: Word OOXML file。

    Returns:
        段落改行を保ったplain text。

    Raises:
        KeyError: document.xmlが存在しない場合。
        BadZipFile: OOXML packageが破損している場合。
    """

    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    paragraphs = []
    for paragraph in root.findall(f".//{{{WORD_NS}}}p"):
        text = "".join(
            node.text or "" for node in paragraph.findall(f".//{{{WORD_NS}}}t")
        ).strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def load_documents(
    files: list[Path], chunk_chars: int, overlap_chars: int
) -> list[Document]:
    """対応fileを読み、安定IDとmetadata付きLangChain Documentへ変換する。

    Args:
        files: 読み込むfile一覧。
        chunk_chars: 1 chunkの最大文字数。
        overlap_chars: 隣接chunkの重複文字数。

    Returns:
        Qdrantへ登録できるLangChain Document一覧。
    """

    documents: list[Document] = []
    for path in files:
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        source = path.resolve().as_posix()
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            units = _pdf_units(path)
        elif suffix in {".docx", ".dotx"}:
            units = [(_word_text(path), {})]
        else:
            units = [(payload.decode("utf-8-sig"), {})]
        for unit_index, (text, unit_metadata) in enumerate(units):
            for chunk_index, chunk in enumerate(
                split_text(text, chunk_chars, overlap_chars)
            ):
                point_id = str(
                    uuid5(NAMESPACE_URL, f"{source}:{unit_index}:{chunk_index}")
                )
                documents.append(
                    Document(
                        id=point_id,
                        page_content=chunk,
                        metadata={
                            "source": source,
                            "source_type": suffix.removeprefix("."),
                            "document_sha256": digest,
                            "unit": unit_index,
                            "chunk": chunk_index,
                            **unit_metadata,
                        },
                    )
                )
    if not documents:
        raise ValueError("Documents contain no extractable text")
    return documents


def _settings(collection: str | None) -> tuple[str, str, str]:
    """環境変数とCLIからQdrant設定を解決する。

    Args:
        collection: CLIで明示されたcollection名。

    Returns:
        Qdrant URI、API key、collection名。

    Raises:
        RuntimeError: 必須設定が不足する場合。
    """

    uri = os.getenv("QDRANT_URI") or os.getenv("QDRANT_URL")
    key = os.getenv("QDRANT_API_KEY")
    name = collection or os.getenv("QDRANT_COLLECTION")
    missing = [
        label
        for label, value in (
            ("QDRANT_URI", uri),
            ("QDRANT_API_KEY", key),
            ("QDRANT_COLLECTION", name),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing Qdrant settings: {', '.join(missing)}")
    return str(uri), str(key), str(name)


def _embeddings() -> OpenAIEmbeddings:
    """環境変数からReviewStage互換のLangChain embedding modelを作る。

    Returns:
        設定済みOpenAIEmbeddings。

    Raises:
        RuntimeError: OpenAI API keyが未設定の場合。
    """

    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is required")
    return OpenAIEmbeddings(
        model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
        base_url=os.getenv("OPENAI_BASE_URL"),
        api_key=SecretStr(key),
    )


def _delete_old_revisions(
    store: QdrantVectorStore, collection: str, documents: list[Document]
) -> None:
    """同じsourceで現在hash以外の古いpointを削除する。

    Args:
        store: 登録済みQdrantVectorStore。
        collection: 削除対象collection。
        documents: 今回登録したDocument一覧。

    Returns:
        なし。

    Side Effects:
        Qdrantから明示対象sourceの旧revisionを削除する。
    """

    revisions = {
        (str(document.metadata["source"]), str(document.metadata["document_sha256"]))
        for document in documents
    }
    for source, digest in revisions:
        selector = models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="metadata.source", match=models.MatchValue(value=source)
                    )
                ],
                must_not=[
                    models.FieldCondition(
                        key="metadata.document_sha256",
                        match=models.MatchValue(value=digest),
                    )
                ],
            )
        )
        store.client.delete(
            collection_name=collection, points_selector=selector, wait=True
        )


def ingest(
    documents: list[Document],
    collection: str | None,
    batch_size: int,
    replace_source: bool,
) -> int:
    """LangChainでembeddingを生成しQdrantへDocumentをupsertする。

    Args:
        documents: 登録対象LangChain Document。
        collection: 任意のcollection名。
        batch_size: embedding/upsert batch件数。
        replace_source: 同じsourceの旧revisionを削除するか。

    Returns:
        登録したpoint数。

    Side Effects:
        collectionを必要時に作成し、Qdrant pointをupsertする。
    """

    uri, key, name = _settings(collection)
    store = QdrantVectorStore.from_documents(
        documents,
        _embeddings(),
        url=uri,
        api_key=key,
        collection_name=name,
        vector_name=os.getenv("QDRANT_VECTOR_NAME", ""),
        batch_size=batch_size,
    )
    if replace_source:
        _delete_old_revisions(store, name, documents)
    return len(documents)


@app.command()
def cli(
    input: Annotated[
        list[Path], typer.Option(help="input file or directory; repeatable")
    ],
    env: Annotated[Path, typer.Option(help="dotenv path")] = Path(".env"),
    collection: Annotated[
        str | None, typer.Option(help="Qdrant collection name")
    ] = None,
    chunk_chars: Annotated[
        int, typer.Option(min=1, help="maximum characters per chunk")
    ] = 1_500,
    overlap_chars: Annotated[
        int, typer.Option(min=0, help="overlap characters between chunks")
    ] = 200,
    batch_size: Annotated[
        int, typer.Option(min=1, help="embedding and upsert batch size")
    ] = 64,
    replace_source: Annotated[
        bool, typer.Option(help="delete older revisions of ingested sources")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option(help="parse and chunk without embedding or upsert")
    ] = False,
) -> None:
    """入力文書をchunk化し、Review RAG用Qdrant collectionへ登録する。

    Args:
        input: fileまたはdirectory。optionを繰り返し指定できる。
        env: dotenv file。
        collection: Qdrant collection名。未指定時は環境変数を使う。
        chunk_chars: 1 chunkの最大文字数。
        overlap_chars: 隣接chunkへ重ねる文字数。
        batch_size: embeddingとupsertのbatch件数。
        replace_source: 同じsourceの旧revisionを削除するか。
        dry_run: 外部APIを呼ばず読み込みだけ検証するか。

    Returns:
        なし。

    Side Effects:
        dotenvを読み、dry-runでなければOpenAI互換APIとQdrantを更新する。
    """

    load_environment(env)
    configure_logging()
    try:
        files = collect_files(input)
        documents = load_documents(files, chunk_chars, overlap_chars)
        characters = sum(len(document.page_content) for document in documents)
        if dry_run:
            LOGGER.info(
                "Qdrant ingest dry run completed files=%s chunks=%s characters=%s",
                len(files),
                len(documents),
                characters,
            )
            return
        count = ingest(documents, collection, batch_size, replace_source)
    except KeyboardInterrupt:
        LOGGER.error("Qdrant ingest was interrupted")
        raise typer.Exit(code=130) from None
    except Exception as error:
        LOGGER.exception("Qdrant ingest failed: %s", error)
        raise typer.Exit(code=1) from None
    LOGGER.info(
        "Qdrant ingest completed files=%s chunks=%s characters=%s",
        len(files),
        count,
        characters,
    )


def main(argv: list[str] | None = None) -> int:
    """Qdrant ingest CLIを実行して終了codeを返す。

    Args:
        argv: 引数列。Noneならsys.argvを使う。

    Returns:
        process終了code。

    Side Effects:
        CLI引数に従い文書を読み、任意でQdrantを更新する。
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
    LOGGER.debug("Elapsed time %.3f seconds", perf_counter() - started_at)
    sys.exit(exit_code)
