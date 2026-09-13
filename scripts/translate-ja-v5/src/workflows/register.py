"""Review参照文書をQdrantへrevision単位で登録する。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from qdrant_client import models

from src.adapters.langfuse import observed, update_current
from src.adapters.llm import embeddings
from src.adapters.pandoc import docx_to_text, pdf_pages_text
from src.adapters.qdrant import replace_revision
from src.config import Settings

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".md", ".txt"}


@dataclass(frozen=True)
class Chunk:
    """登録前の一つの検索用chunkを表す。"""

    text: str
    unit: int
    index: int


def collect_files(
    doc_path: Path | None, doc_dir: Path | None
) -> tuple[Path, list[Path]]:
    """排他的なfileまたはdirectory指定から登録対象を集める。

    Args:
        doc_path: 単一file指定。
        doc_dir: 再帰探索するdirectory指定。

    Returns:
        source相対化rootとsort済みfile列。

    Raises:
        ValueError: 指定が排他的でないか対象fileがない場合。
        FileNotFoundError: 指定pathが存在しない場合。
    """

    if (doc_path is None) == (doc_dir is None):
        raise ValueError("specify exactly one of --doc-path or --doc-dir")
    selected = doc_path or doc_dir
    if selected is None or not selected.exists():
        raise FileNotFoundError(selected)
    if doc_path is not None:
        if (
            not doc_path.is_file()
            or doc_path.suffix.casefold() not in SUPPORTED_SUFFIXES
        ):
            raise ValueError(f"unsupported document: {doc_path}")
        if doc_path.stat().st_size == 0:
            raise ValueError(f"empty document: {doc_path}")
        return doc_path.parent.resolve(), [doc_path.resolve()]
    if doc_dir is None:
        raise ValueError("--doc-dir is required")
    root = doc_dir.resolve()
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.casefold() in SUPPORTED_SUFFIXES
        and path.stat().st_size > 0
        and not any(part.startswith(".") for part in path.relative_to(root).parts)
    )
    if not files:
        raise ValueError("no supported non-empty documents found")
    return root, files


def extract_units(path: Path) -> list[str]:
    """対応文書から順序付きtext unitを抽出する。

    Args:
        path: PDF、DOCX、Markdown、textのいずれか。

    Returns:
        空白だけの値を除いたunit列。

    Raises:
        ValueError: 形式やUTF-8内容が不正、または本文が空の場合。
    """

    suffix = path.suffix.casefold()
    if suffix == ".pdf":
        values = pdf_pages_text(path)
    elif suffix == ".docx":
        values = [docx_to_text(path)]
    elif suffix in {".md", ".txt"}:
        try:
            values = [path.read_text(encoding="utf-8-sig")]
        except UnicodeDecodeError as error:
            raise ValueError(f"document must be UTF-8: {path}") from error
    else:
        raise ValueError(f"unsupported document: {path}")
    result = [value.strip() for value in values if value.strip()]
    if not result:
        raise ValueError(f"document contains no extractable text: {path}")
    return result


def _semantic_parts(text: str) -> list[str]:
    """本文を空行または見出し境界の意味単位へ分ける。

    Args:
        text: 分割する本文。

    Returns:
        空でない意味単位列。
    """

    return [
        part.strip()
        for part in re.split(r"\n\s*\n|(?=^#{1,6}\s)", text, flags=re.MULTILINE)
        if part.strip()
    ]


def chunk_units(units: list[str], size: int = 1_000, overlap: int = 100) -> list[Chunk]:
    """見出し・段落境界を優先して約1,000文字へ分割する。

    Args:
        units: pageまたは文書単位の本文。
        size: 1 chunkの概算token上限。
        overlap: 隣接chunkへ重ねる概算token数。

    Returns:
        unit番号と順序を持つchunk列。
    """

    chunks: list[Chunk] = []
    for unit_index, unit in enumerate(units):
        current = ""
        for part in _semantic_parts(unit):
            segments = [
                part[index : index + size] for index in range(0, len(part), size)
            ] or [part]
            for segment in segments:
                candidate = f"{current}\n\n{segment}".strip() if current else segment
                if current and len(candidate) > size:
                    chunks.append(Chunk(current, unit_index, len(chunks)))
                    prefix = current[-overlap:] if overlap else ""
                    current = f"{prefix}\n\n{segment}".strip()
                else:
                    current = candidate
                while len(current) > size:
                    chunks.append(Chunk(current[:size], unit_index, len(chunks)))
                    current = current[max(0, size - overlap) :]
        if current:
            chunks.append(Chunk(current, unit_index, len(chunks)))
    return chunks


@observed("register-workflow", capture_input=False)
def register_documents(
    settings: Settings, doc_path: Path | None = None, doc_dir: Path | None = None
) -> int:
    """対象文書をEmbeddingしてQdrantへ安全に登録する。

    Args:
        settings: OpenAI EmbeddingとQdrant設定。
        doc_path: 任意の単一file。
        doc_dir: 任意のdirectory。

    Returns:
        登録したpoint総数。
    """

    root, files = collect_files(doc_path, doc_dir)
    total = 0
    update_current(input={"files": [str(path) for path in files]})
    for path in files:
        payload = path.read_bytes()
        revision = hashlib.sha256(payload).hexdigest()
        source = path.relative_to(root).as_posix()
        chunks = chunk_units(extract_units(path))
        vectors = embeddings(settings, [chunk.text for chunk in chunks])
        points = [
            models.PointStruct(
                id=str(uuid5(NAMESPACE_URL, f"{source}:{revision}:{chunk.index}")),
                vector=vector,
                payload={
                    "text": chunk.text,
                    "source": source,
                    "source_type": path.suffix.casefold().removeprefix("."),
                    "revision": revision,
                    "unit": chunk.unit,
                    "chunk": chunk.index,
                },
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        replace_revision(settings, points, source, revision)
        total += len(points)
    update_current(output={"points": total})
    return total
