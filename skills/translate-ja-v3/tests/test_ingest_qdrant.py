"""Qdrant ingest CLIの決定論的な契約を検証する。"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.documents import Document
from langchain_qdrant import QdrantVectorStore
from qdrant_client import models

import ingest_qdrant
from ingest_qdrant import (
    _delete_old_revisions,
    collect_files,
    ingest,
    load_documents,
    main,
    split_text,
)


def test_collect_files_recurses_and_deduplicates(tmp_path: Path) -> None:
    """directoryを再帰走査し対応fileだけを重複なく返すことを確認する。

    Args:
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    nested = tmp_path / "nested"
    nested.mkdir()
    document = nested / "guide.md"
    document.write_text("Guide", encoding="utf-8")
    (nested / "ignored.bin").write_bytes(b"ignored")

    assert collect_files([tmp_path, document]) == [document.resolve()]


def test_split_text_applies_character_overlap() -> None:
    """chunk境界に指定文字数のoverlapが含まれることを確認する。

    Returns:
        なし。
    """

    assert split_text("abcdefghij", 6, 2) == ["abcdef", "efghij", "ij"]
    with pytest.raises(ValueError, match="greater than overlap_chars"):
        split_text("text", 4, 4)


def test_load_documents_builds_stable_ids_and_metadata(tmp_path: Path) -> None:
    """text入力から安定IDと追跡metadataを生成することを確認する。

    Args:
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    source = tmp_path / "domain.txt"
    source.write_text("abcdefghij", encoding="utf-8")

    first = load_documents([source], 6, 2)
    second = load_documents([source], 6, 2)

    assert [item.id for item in first] == [item.id for item in second]
    assert [item.page_content for item in first] == ["abcdef", "efghij", "ij"]
    assert first[0].metadata == {
        "source": source.resolve().as_posix(),
        "source_type": "txt",
        "document_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "unit": 0,
        "chunk": 0,
    }


def test_load_documents_extracts_word_paragraphs(tmp_path: Path) -> None:
    """Word OOXMLの段落と表セル相当textを抽出することを確認する。

    Args:
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    source = tmp_path / "domain.docx"
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body>'
        "<w:p><w:r><w:t>First</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cell</w:t></w:r>"
        "</w:p></w:tc></w:tr></w:tbl>"
        "</w:body></w:document>"
    )
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("word/document.xml", xml)

    documents = load_documents([source], 100, 0)

    assert documents[0].page_content == "First\nCell"
    assert documents[0].metadata["source_type"] == "docx"


def test_main_dry_run_avoids_external_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dry-runがembeddingとQdrantを呼ばず成功することを確認する。

    Args:
        tmp_path: pytest一時directory。
        monkeypatch: pytest monkeypatch fixture。

    Returns:
        なし。
    """

    source = tmp_path / "domain.md"
    source.write_text("Domain knowledge", encoding="utf-8")

    def fail_ingest(*args: Any, **kwargs: Any) -> int:
        """外部登録が呼ばれた場合にtestを失敗させる。

        Args:
            args: 位置引数。
            kwargs: keyword引数。

        Returns:
            到達しない終了code。

        Raises:
            AssertionError: 常に送出する。
        """

        raise AssertionError("ingest must not run")

    monkeypatch.setattr(ingest_qdrant, "ingest", fail_ingest)

    assert main(["--input", str(source), "--dry-run"]) == 0


def test_ingest_upserts_with_review_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review互換設定でDocumentをupsertすることを確認する。

    Args:
        monkeypatch: pytest monkeypatch fixture。

    Returns:
        なし。
    """

    captured: dict[str, Any] = {}
    deletions: list[tuple[object, str, list[Document]]] = []
    store = object()
    documents = [Document(id="point", page_content="text", metadata={})]

    def fake_from_documents(*args: Any, **kwargs: Any) -> object:
        """Qdrant呼び出しを記録してfake storeを返す。

        Args:
            args: Documentとembedding。
            kwargs: Qdrant接続設定。

        Returns:
            fake store。
        """

        captured["args"] = args
        captured["kwargs"] = kwargs
        return store

    def fake_delete_old_revisions(
        selected_store: object,
        selected_collection: str,
        selected_documents: list[Document],
    ) -> None:
        """旧revision削除要求を記録する。

        Args:
            selected_store: upsert後のstore。
            selected_collection: 削除対象collection。
            selected_documents: 今回登録したDocument。

        Returns:
            なし。
        """

        deletions.append((selected_store, selected_collection, selected_documents))

    monkeypatch.setattr(
        ingest_qdrant, "_settings", lambda collection: ("url", "key", "chosen")
    )
    monkeypatch.setattr(ingest_qdrant, "_embeddings", lambda: "embedding")
    monkeypatch.setattr(
        ingest_qdrant.QdrantVectorStore,
        "from_documents",
        fake_from_documents,
    )
    monkeypatch.setattr(
        ingest_qdrant, "_delete_old_revisions", fake_delete_old_revisions
    )
    monkeypatch.setenv("QDRANT_VECTOR_NAME", "content")

    assert ingest(documents, "requested", 32, False) == 1
    assert deletions == []
    assert ingest(documents, "requested", 32, True) == 1
    assert deletions == [(store, "chosen", documents)]
    assert captured["args"] == (documents, "embedding")
    assert captured["kwargs"] == {
        "url": "url",
        "api_key": "key",
        "collection_name": "chosen",
        "vector_name": "content",
        "batch_size": 32,
    }


def test_delete_old_revisions_filters_source_and_current_hash() -> None:
    """旧revision削除がsourceを限定し現在hashを除外することを確認する。

    Returns:
        なし。
    """

    calls: list[dict[str, Any]] = []

    class FakeClient:
        """delete条件を記録するQdrantClient代替。"""

        def delete(self, **kwargs: Any) -> None:
            """delete引数を記録する。

            Args:
                kwargs: Qdrant delete引数。

            Returns:
                なし。
            """

            calls.append(kwargs)

    class FakeStore:
        """fake clientを公開するVectorStore代替。"""

        def __init__(self) -> None:
            """fake clientを初期化する。

            Returns:
                なし。
            """

            self.client = FakeClient()

    documents = [
        Document(
            page_content="text",
            metadata={"source": "/docs/source.md", "document_sha256": "current"},
        )
    ]

    _delete_old_revisions(cast(QdrantVectorStore, FakeStore()), "domain", documents)

    selector = calls[0]["points_selector"]
    assert calls[0]["collection_name"] == "domain"
    assert calls[0]["wait"] is True
    assert isinstance(selector, models.FilterSelector)
    assert selector.filter.must[0].key == "metadata.source"
    assert selector.filter.must[0].match.value == "/docs/source.md"
    assert selector.filter.must_not[0].key == "metadata.document_sha256"
    assert selector.filter.must_not[0].match.value == "current"
