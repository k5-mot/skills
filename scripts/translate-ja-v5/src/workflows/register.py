"""Review参照文書をQdrantへrevision単位で登録する。"""

from __future__ import annotations

import hashlib
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from qdrant_client import models

from src.adapters.docling import convert_document
from src.adapters.langfuse import observed, update_current
from src.adapters.llm import embeddings
from src.adapters.qdrant import replace_revision
from src.config import Settings
from src.model import Document, block_text, inline_text
from src.processing.normalize import normalize_docling

DOCLING_SUFFIXES = {".pdf", ".docx", ".pptx"}
SUPPORTED_SUFFIXES = DOCLING_SUFFIXES | {".md", ".txt"}


@dataclass(frozen=True)
class Chunk:
    """登録前の一つの検索用chunkを表す。"""

    text: str
    unit: int
    index: int


def _normalized_text(document: Document) -> str:
    """正規化済み文書を検索登録用の簡潔なMarkdownへ変換する。

    Args:
        document: Doclingから正規化した内部文書。

    Returns:
        見出し記法と可視本文だけを持つ文字列。
    """

    parts: list[str] = []
    for page in document.pages:
        for block in sorted(page.blocks, key=lambda item: item.order):
            text = block_text(block, reviewed=False)
            if block.kind == "table":
                text = "\n".join(inline_text(cell.source) for cell in block.cells)
            elif block.kind == "figure":
                text = inline_text(block.caption) or block.alt_text or ""
            if block.kind == "heading" and text:
                text = f"{'#' * max(1, min(6, block.level or 1))} {text}"
            if text.strip():
                parts.append(text.strip())
    return "\n\n".join(parts)


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


def extract_units(path: Path, settings: Settings) -> list[str]:
    """対応文書から順序付きtext unitを抽出する。

    Args:
        path: PDF、DOCX、PPTX、Markdown、textのいずれか。
        settings: Docling接続設定。

    Returns:
        空白だけの値を除いたunit列。

    Raises:
        ValueError: 形式やUTF-8内容が不正、または本文が空の場合。
    """

    suffix = path.suffix.casefold()
    if suffix in DOCLING_SUFFIXES:
        if not settings.docling_url:
            raise ValueError("DOCLING_URL is required for PDF/DOCX/PPTX registration")
        with tempfile.TemporaryDirectory(prefix="translate-ja-register-") as temporary:
            root = Path(temporary)
            parsed = convert_document(
                path,
                root / "parsed.json",
                root / "artifacts",
                settings.docling_url,
                settings.docling_api_key,
            )
            values = [_normalized_text(normalize_docling(parsed))]
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
    """Markdown見出しと対応本文を一つの意味単位へまとめる。

    Args:
        text: 分割する本文。

    Returns:
        空でない意味単位列。
    """

    headings = list(re.finditer(r"^#{1,6}\s", text, flags=re.MULTILINE))
    if not headings:
        return [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    parts = [
        part.strip()
        for part in re.split(r"\n\s*\n", text[: headings[0].start()])
        if part.strip()
    ]
    raw_parts = [*parts]
    raw_parts.extend(
        text[heading.start() : next_start].strip()
        for heading, next_start in zip(
            headings,
            [item.start() for item in headings[1:]] + [len(text)],
            strict=True,
        )
    )
    # 本文を持たない連続見出しは、最初の本文を持つ見出しblockへ連結する。
    result: list[str] = []
    pending: list[str] = []
    for part in raw_parts:
        if not part:
            continue
        if re.match(r"^#{1,6}\s", part):
            lines = part.splitlines()
            if not any(line.strip() for line in lines[1:]):
                pending.append(part)
                continue
        result.append("\n\n".join([*pending, part]))
        pending.clear()
    if pending:
        result.append("\n\n".join(pending))
    return result


def chunk_units(units: list[str], size: int = 1_500) -> list[Chunk]:
    """意味blockを壊さず最大1,500文字を目安に連結する。

    Args:
        units: pageまたは文書単位の本文。
        size: 1 chunkの文字数上限。単一blockが超える場合はblockを優先する。

    Returns:
        unit番号と順序を持つchunk列。
    """

    chunks: list[Chunk] = []
    for unit_index, unit in enumerate(units):
        current = ""
        for part in _semantic_parts(unit):
            candidate = f"{current}\n\n{part}".strip() if current else part
            if current and len(candidate) > size:
                chunks.append(Chunk(current, unit_index, len(chunks)))
                current = part
            else:
                current = candidate
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
        chunks = chunk_units(extract_units(path, settings))
        vectors = embeddings(settings, [chunk.text for chunk in chunks])
        # revisionをUUID材料へ含め、旧版と新版を同時保持できるID空間にする。
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
