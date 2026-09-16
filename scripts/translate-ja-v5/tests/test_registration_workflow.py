"""参照文書抽出、chunk、Qdrant revision置換を検証する。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.adapters import qdrant
from src.config import Settings
from src.model import Block, Document, Inline, Page
from src.workflows import register


class TemporaryQdrantError(RuntimeError):
    """再試行対象statusを持つQdrant test用例外。"""

    def __init__(self) -> None:
        """503 statusの一時例外を作る。

        Returns:
            なし。
        """

        super().__init__("temporary Qdrant failure")
        self.status_code = 503


def test_collect_files_filters_hidden_empty_and_unsupported(tmp_path: Path) -> None:
    """directory探索が対応する非表示でない非空fileだけを返すことを確認する。

    Args:
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    (tmp_path / "ok.md").write_text("text", encoding="utf-8")
    (tmp_path / "slides.pptx").write_bytes(b"presentation")
    (tmp_path / "empty.txt").touch()
    (tmp_path / "skip.bin").write_bytes(b"x")
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "secret.md").write_text("secret", encoding="utf-8")
    root, files = register.collect_files(None, tmp_path)
    assert root == tmp_path.resolve()
    assert files == [
        (tmp_path / "ok.md").resolve(),
        (tmp_path / "slides.pptx").resolve(),
    ]


def test_extract_units_routes_binary_formats_through_docling_normalize(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """PDF、DOCX、PPTXを共通のDocling Parse、Normalizeへ通す。

    Args:
        monkeypatch: Docling ParseとNormalizeを差し替えるfixture。
        settings: 共通Settings fixture。
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    configured = replace(settings, docling_url="http://docling")
    pdf, docx, pptx, markdown, text = (
        tmp_path / name for name in ("a.pdf", "a.docx", "a.pptx", "a.md", "a.txt")
    )
    for path in (pdf, docx, pptx):
        path.write_bytes(b"binary")
    markdown.write_text("# Heading", encoding="utf-8")
    text.write_text("Plain", encoding="utf-8")
    parsed: list[str] = []

    def convert(source: Path, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        """Parse対象の拡張子を記録する。

        Args:
            source: Parse対象文書。
            _args: 未使用の位置引数。
            _kwargs: 未使用のkeyword引数。

        Returns:
            入力形式だけを持つ模擬Docling文書。
        """

        parsed.append(source.suffix)
        return {"source": source.suffix}

    document = Document(
        pages=[
            Page(
                number=1,
                blocks=[
                    Block(
                        id="heading",
                        order=0,
                        kind="heading",
                        level=1,
                        source=[Inline(id="heading:0", text="Heading")],
                    ),
                    Block(
                        id="body",
                        order=1,
                        kind="paragraph",
                        source=[Inline(id="body:0", text="Body")],
                    ),
                ],
            )
        ]
    )
    monkeypatch.setattr(register, "convert_document", convert)
    monkeypatch.setattr(register, "normalize_docling", lambda _parsed: document)
    for path in (pdf, docx, pptx):
        assert register.extract_units(path, configured) == ["# Heading\n\nBody"]
    assert parsed == [".pdf", ".docx", ".pptx"]
    assert register.extract_units(markdown, configured) == ["# Heading"]
    assert register.extract_units(text, configured) == ["Plain"]


def test_chunk_units_joins_whole_blocks_up_to_limit() -> None:
    """意味blockを分割せず上限まで連結することを確認する。

    Returns:
        なし。
    """

    chunks = register.chunk_units(["A" * 70 + "\n\n" + "B" * 70], size=100, overlap=0)
    assert len(chunks) == 2
    assert chunks[0].text == "A" * 70
    assert chunks[1].text == "B" * 70


def test_chunk_units_uses_1000_character_default() -> None:
    """既定値では区切り込み1,000文字までを一chunkへ連結する。

    Returns:
        なし。
    """

    joined = register.chunk_units(["A" * 499 + "\n\n" + "B" * 499])
    split = register.chunk_units(["A" * 500 + "\n\n" + "B" * 500])
    assert [len(chunk.text) for chunk in joined] == [1_000]
    assert len(split) == 2
    assert all(len(chunk.text) <= 1_000 for chunk in split)


def test_chunk_units_keeps_markdown_heading_with_its_body() -> None:
    """Markdown見出しを対応本文と同じblockとしてchunkへ入れる。

    Returns:
        なし。
    """

    chunks = register.chunk_units(
        ["# Alpha\n\nAlpha body\n\n# Beta\n\nBeta body"], size=30, overlap=0
    )
    assert [chunk.text for chunk in chunks] == [
        "# Alpha\n\nAlpha body",
        "# Beta\n\nBeta body",
    ]


def test_chunk_units_splits_oversized_heading_block_with_context() -> None:
    """巨大な見出しblockを見出しと重複文脈付きで上限内へ分割する。

    Returns:
        なし。
    """

    source = "# Alpha\n\n" + "0123456789 " * 8
    chunks = register.chunk_units([source], size=30, overlap=5)
    assert len(chunks) > 1
    assert all(chunk.text.startswith("# Alpha\n\n") for chunk in chunks)
    assert all(len(chunk.text) <= 30 for chunk in chunks)


def test_chunk_units_splits_indivisible_paragraph_with_overlap() -> None:
    """長い単一段落を上限内へ分割して隣接文脈を重複させる。

    Returns:
        なし。
    """

    chunks = register.chunk_units(["0123456789ABCDEFGHIJ"], size=10, overlap=3)
    assert [chunk.text for chunk in chunks] == [
        "0123456789",
        "789ABCDEFG",
        "EFGHIJ",
    ]


def test_chunk_units_keeps_consecutive_headings_with_following_body() -> None:
    """連続見出しを後続本文と一つの意味blockにする。

    Returns:
        なし。
    """

    source = "# Chapter\n\n## Section\n\nBody\n\n# Next\n\nNext body"
    chunks = register.chunk_units([source], size=40, overlap=0)
    assert [chunk.text for chunk in chunks] == [
        "# Chapter\n\n## Section\n\nBody",
        "# Next\n\nNext body",
    ]


def test_register_uses_relative_source_and_stable_point_ids(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """登録metadataとpoint IDが同じ内容で決定的になることを確認する。

    Args:
        monkeypatch: EmbeddingとQdrantを差し替えるfixture。
        settings: 共通Settings fixture。
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    configured = replace(
        settings, qdrant_url="http://qdrant", qdrant_collection="review"
    )
    docs = tmp_path / "docs"
    nested = docs / "guide"
    docs.mkdir()
    nested.mkdir()
    document = nested / "terms.md"
    document.write_text("# Heading\n\nBody", encoding="utf-8")
    captured: list[Any] = []
    monkeypatch.setattr(
        register, "embeddings", lambda _settings, texts: [[1.0, 0.0] for _text in texts]
    )
    monkeypatch.setattr(
        register,
        "replace_revision",
        lambda _settings, points, source, revision: captured.append(
            (points, source, revision)
        ),
    )
    assert register.register_documents(configured, doc_dir=docs) == 1
    first_id = captured[0][0][0].id
    assert captured[0][1] == "guide/terms.md"
    captured.clear()
    register.register_documents(configured, doc_dir=docs)
    assert captured[0][0][0].id == first_id


def test_qdrant_verifies_new_revision_before_delete(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """新point検証失敗時に旧revisionを削除しないことを確認する。

    Args:
        monkeypatch: Qdrant clientを差し替えるfixture。
        settings: 共通Settings fixture。

    Returns:
        なし。
    """

    configured = replace(
        settings, qdrant_url="http://qdrant", qdrant_collection="review"
    )
    calls: list[str] = []

    class Client:
        """Qdrant置換順序を記録するtest double。"""

        def collection_exists(self, **_kwargs: Any) -> bool:
            """既存collectionを返す。

            Args:
                _kwargs: 未使用collection指定。

            Returns:
                True。
            """

            return True

        def upsert(self, **_kwargs: Any) -> None:
            """upsert呼出しを記録する。

            Args:
                _kwargs: 未使用Qdrant引数。

            Returns:
                なし。
            """

            calls.append("upsert")

        def retrieve(self, **_kwargs: Any) -> list[Any]:
            """新pointが見つからない状態を返す。

            Args:
                _kwargs: 未使用Qdrant引数。

            Returns:
                空のpoint列。
            """

            calls.append("retrieve")
            return []

        def delete(self, **_kwargs: Any) -> None:
            """呼ばれてはならない削除を記録する。

            Args:
                _kwargs: 未使用Qdrant引数。

            Returns:
                なし。
            """

            calls.append("delete")

    monkeypatch.setattr(qdrant, "_client", lambda _settings: Client())
    point = qdrant.models.PointStruct(
        id="00000000-0000-0000-0000-000000000001", vector=[1.0], payload={}
    )
    with pytest.raises(RuntimeError, match="every"):
        qdrant.replace_revision(configured, [point], "source.md", "new")
    assert calls == ["upsert", "retrieve"]


def test_qdrant_deletes_only_after_successful_verification(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """新revision全point確認後に削除へ進む順序を確認する。

    Args:
        monkeypatch: Qdrant clientを差し替えるfixture。
        settings: 共通Settings fixture。

    Returns:
        なし。
    """

    configured = replace(
        settings, qdrant_url="http://qdrant", qdrant_collection="review"
    )
    calls: list[str] = []
    point = qdrant.models.PointStruct(
        id="00000000-0000-0000-0000-000000000001", vector=[1.0], payload={}
    )

    class SimplePoint:
        """retrieve結果の最小pointを表す。"""

        def __init__(self, point_id: object) -> None:
            """point IDを保持する。

            Args:
                point_id: Qdrant point ID。

            Returns:
                なし。
            """

            self.id = point_id

    class Client:
        """成功するQdrant置換のtest double。"""

        def collection_exists(self, **_kwargs: Any) -> bool:
            """既存collectionを返す。

            Args:
                _kwargs: 未使用collection指定。

            Returns:
                True。
            """

            return True

        def upsert(self, **_kwargs: Any) -> None:
            """upsert呼出しを記録する。

            Args:
                _kwargs: 未使用Qdrant引数。

            Returns:
                なし。
            """

            calls.append("upsert")

        def retrieve(self, **_kwargs: Any) -> list[Any]:
            """登録済みpointを返す。

            Args:
                _kwargs: 未使用Qdrant引数。

            Returns:
                point IDを持つ結果。
            """

            calls.append("retrieve")
            return [SimplePoint(point.id)]

        def delete(self, **kwargs: Any) -> None:
            """削除filterと呼出し順序を確認する。

            Args:
                kwargs: Qdrant削除引数。

            Returns:
                なし。
            """

            assert kwargs["collection_name"] == "review"
            calls.append("delete")

    monkeypatch.setattr(qdrant, "_client", lambda _settings: Client())
    qdrant.replace_revision(configured, [point], "source.md", "new")
    assert calls == ["upsert", "retrieve", "delete", "delete"]


def test_qdrant_deletes_stale_tail_from_same_revision(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """chunk数減少時に同一revisionの余剰pointを削除する。

    Args:
        monkeypatch: Qdrant clientを差し替えるfixture。
        settings: 共通Settings fixture。

    Returns:
        なし。
    """

    configured = replace(
        settings, qdrant_url="http://qdrant", qdrant_collection="review"
    )
    point = qdrant.models.PointStruct(
        id="00000000-0000-0000-0000-000000000001", vector=[1.0], payload={}
    )
    selectors: list[dict[str, Any]] = []

    class Client:
        """同一revisionの削除条件を記録するtest double。"""

        def collection_exists(self, **_kwargs: Any) -> bool:
            """既存collectionを返す。

            Args:
                _kwargs: 未使用collection指定。

            Returns:
                True。
            """

            return True

        def upsert(self, **_kwargs: Any) -> None:
            """point登録を成功させる。

            Args:
                _kwargs: 未使用登録引数。

            Returns:
                なし。
            """

        def retrieve(self, **_kwargs: Any) -> list[SimpleNamespace]:
            """登録済みpointを返す。

            Args:
                _kwargs: 未使用取得引数。

            Returns:
                登録済みpoint。
            """

            return [SimpleNamespace(id=point.id)]

        def delete(self, **kwargs: Any) -> None:
            """削除条件を記録する。

            Args:
                kwargs: point selectorを含む削除引数。

            Returns:
                なし。
            """

            selectors.append(kwargs["points_selector"].model_dump())

    monkeypatch.setattr(qdrant, "_client", lambda _settings: Client())
    qdrant.replace_revision(configured, [point], "source.md", "new")
    conditions = selectors[1]["must"]
    assert conditions[0]["key"] == "source"
    assert conditions[0]["match"]["value"] == "source.md"
    assert conditions[1]["key"] == "revision"
    assert conditions[1]["match"]["value"] == "new"
    assert conditions[2]["key"] == "chunk"
    assert conditions[2]["range"]["gte"] == 1.0


def test_qdrant_sends_large_revisions_in_fixed_batches(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """Qdrant登録と確認を64件ずつ送り、全確認後だけ旧版を削除する。

    Args:
        monkeypatch: Qdrant clientを差し替えるfixture。
        settings: 共通Settings fixture。

    Returns:
        なし。
    """

    configured = replace(
        settings, qdrant_url="http://qdrant", qdrant_collection="review"
    )
    points = [
        qdrant.models.PointStruct(id=index, vector=[1.0], payload={})
        for index in range(129)
    ]
    calls: list[tuple[str, int]] = []

    class Client:
        """Qdrant batch sizeと呼出し順序を記録するtest double。"""

        def collection_exists(self, **_kwargs: Any) -> bool:
            """既存collectionを返す。

            Args:
                _kwargs: 未使用collection指定。

            Returns:
                True。
            """

            return True

        def upsert(self, **kwargs: Any) -> None:
            """upsert batch件数を記録する。

            Args:
                kwargs: pointsを含むupsert引数。

            Returns:
                なし。
            """

            calls.append(("upsert", len(kwargs["points"])))

        def retrieve(self, **kwargs: Any) -> list[SimpleNamespace]:
            """retrieve batch件数を記録して全IDを返す。

            Args:
                kwargs: idsを含むretrieve引数。

            Returns:
                指定IDを持つpoint列。
            """

            calls.append(("retrieve", len(kwargs["ids"])))
            return [SimpleNamespace(id=value) for value in kwargs["ids"]]

        def delete(self, **_kwargs: Any) -> None:
            """旧revision削除を記録する。

            Args:
                _kwargs: 未使用削除引数。

            Returns:
                なし。
            """

            calls.append(("delete", 0))

    monkeypatch.setattr(qdrant, "_client", lambda _settings: Client())
    qdrant.replace_revision(configured, points, "source.md", "new")
    assert calls == [
        ("upsert", 64),
        ("upsert", 64),
        ("upsert", 1),
        ("retrieve", 64),
        ("retrieve", 64),
        ("retrieve", 1),
        ("delete", 0),
        ("delete", 0),
    ]


def test_qdrant_creates_missing_collection_before_upsert(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """未作成collectionをvector次元に合わせて作ってから登録する。

    Args:
        monkeypatch: Qdrant clientを差し替えるfixture。
        settings: 共通Settings fixture。

    Returns:
        なし。
    """

    configured = replace(
        settings, qdrant_url="http://qdrant", qdrant_collection="review"
    )
    calls: list[tuple[str, dict[str, Any]]] = []
    point = qdrant.models.PointStruct(
        id="00000000-0000-0000-0000-000000000001",
        vector=[1.0, 0.0],
        payload={},
    )

    class Client:
        """collection作成と登録順を記録するtest double。"""

        def collection_exists(self, **kwargs: Any) -> bool:
            """collection未作成を返す。

            Args:
                kwargs: collection指定。

            Returns:
                False。
            """

            calls.append(("exists", kwargs))
            return False

        def create_collection(self, **kwargs: Any) -> None:
            """collection作成引数を記録する。

            Args:
                kwargs: collectionとvector設定。

            Returns:
                なし。
            """

            calls.append(("create", kwargs))

        def upsert(self, **kwargs: Any) -> None:
            """point登録を記録する。

            Args:
                kwargs: upsert引数。

            Returns:
                なし。
            """

            calls.append(("upsert", kwargs))

        def retrieve(self, **kwargs: Any) -> list[SimpleNamespace]:
            """登録済みpointを返す。

            Args:
                kwargs: retrieve引数。

            Returns:
                登録済みpoint。
            """

            calls.append(("retrieve", kwargs))
            return [SimpleNamespace(id=point.id)]

        def delete(self, **kwargs: Any) -> None:
            """旧revision削除を記録する。

            Args:
                kwargs: delete引数。

            Returns:
                なし。
            """

            calls.append(("delete", kwargs))

    monkeypatch.setattr(qdrant, "_client", lambda _settings: Client())
    qdrant.replace_revision(configured, [point], "source.md", "new")
    assert [name for name, _kwargs in calls] == [
        "exists",
        "create",
        "upsert",
        "retrieve",
        "delete",
        "delete",
    ]
    create = calls[1][1]
    assert create["collection_name"] == "review"
    assert create["vectors_config"].size == 2
    assert create["vectors_config"].distance == qdrant.models.Distance.COSINE


def test_every_qdrant_operation_uses_shared_retry(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """検索、登録、検証、削除の全呼出しが一時障害を再試行することを確認する。

    Args:
        monkeypatch: Qdrant clientと待機を差し替えるfixture。
        settings: 共通Settings fixture。

    Returns:
        なし。
    """

    configured = replace(
        settings, qdrant_url="http://qdrant", qdrant_collection="review"
    )
    attempts = {name: 0 for name in ("query", "upsert", "retrieve", "delete")}
    point = qdrant.models.PointStruct(
        id="00000000-0000-0000-0000-000000000001", vector=[1.0], payload={}
    )

    class Client:
        """各operationを一度だけ失敗させるQdrant test double。"""

        def collection_exists(self, **_kwargs: Any) -> bool:
            """既存collectionを返す。

            Args:
                _kwargs: 未使用collection指定。

            Returns:
                True。
            """

            return True

        def _temporary_once(self, name: str) -> None:
            """指定operationの初回だけ一時例外を送出する。

            Args:
                name: operation名。

            Returns:
                なし。

            Raises:
                TemporaryQdrantError: 初回呼出しの場合。
            """

            attempts[name] += 1
            if attempts[name] == 1:
                raise TemporaryQdrantError

        def query_points(self, **_kwargs: Any) -> SimpleNamespace:
            """検索結果を二回目に返す。

            Args:
                _kwargs: 未使用Qdrant引数。

            Returns:
                一点を持つ検索結果。
            """

            self._temporary_once("query")
            return SimpleNamespace(
                points=[SimpleNamespace(payload={"text": "evidence"}, score=1.0)]
            )

        def upsert(self, **_kwargs: Any) -> None:
            """二回目にupsertを成功させる。

            Args:
                _kwargs: 未使用Qdrant引数。

            Returns:
                なし。
            """

            self._temporary_once("upsert")

        def retrieve(self, **_kwargs: Any) -> list[SimpleNamespace]:
            """二回目に登録済みpointを返す。

            Args:
                _kwargs: 未使用Qdrant引数。

            Returns:
                登録済みpoint列。
            """

            self._temporary_once("retrieve")
            return [SimpleNamespace(id=point.id)]

        def delete(self, **_kwargs: Any) -> None:
            """二回目に旧revision削除を成功させる。

            Args:
                _kwargs: 未使用Qdrant引数。

            Returns:
                なし。
            """

            self._temporary_once("delete")

    client = Client()
    actual_retry = qdrant.retry_call
    monkeypatch.setattr(qdrant, "_client", lambda _settings: client)
    monkeypatch.setattr(qdrant, "embeddings", lambda _settings, _texts: [[1.0]])
    monkeypatch.setattr(
        qdrant,
        "retry_call",
        lambda call: actual_retry(call, lambda _delay: None),
    )
    assert qdrant.search(configured, "query")[0]["text"] == "evidence"
    qdrant.replace_revision(configured, [point], "source.md", "new")
    assert attempts == {"query": 2, "upsert": 2, "retrieve": 2, "delete": 3}


def test_qdrant_delete_failure_is_recoverable_and_rerun_converges(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """旧revision削除失敗後も再実行で同一sourceだけ新revisionへ収束する。

    Args:
        monkeypatch: 状態を持つQdrant clientを差し替えるfixture。
        settings: 共通Settings fixture。

    Returns:
        なし。
    """

    configured = replace(
        settings, qdrant_url="http://qdrant", qdrant_collection="review"
    )
    point = qdrant.models.PointStruct(
        id="00000000-0000-0000-0000-000000000003",
        vector=[1.0],
        payload={"source": "source.md", "revision": "new", "text": "new"},
    )
    stored = {
        "old": {"source": "source.md", "revision": "old"},
        "other": {"source": "other.md", "revision": "old"},
    }
    delete_attempts = 0

    class SimplePoint:
        """retrieve結果の最小pointを表す。"""

        def __init__(self, point_id: object) -> None:
            """point IDを保持する。

            Args:
                point_id: Qdrant point ID。

            Returns:
                なし。
            """

            self.id = point_id

    class Client:
        """置換失敗と再実行を模擬する共有状態client。"""

        def collection_exists(self, **_kwargs: Any) -> bool:
            """既存collectionを返す。

            Args:
                _kwargs: 未使用collection指定。

            Returns:
                True。
            """

            return True

        def upsert(self, **kwargs: Any) -> None:
            """新revisionを冪等に保存する。

            Args:
                kwargs: Qdrant upsert引数。

            Returns:
                なし。
            """

            for value in kwargs["points"]:
                stored[str(value.id)] = dict(value.payload or {})

        def retrieve(self, **kwargs: Any) -> list[SimplePoint]:
            """保存済みIDだけを返す。

            Args:
                kwargs: Qdrant retrieve引数。

            Returns:
                存在するpoint列。
            """

            return [
                SimplePoint(value) for value in kwargs["ids"] if str(value) in stored
            ]

        def delete(self, **kwargs: Any) -> None:
            """初回だけ失敗し、再実行では旧revisionだけを削除する。

            Args:
                kwargs: sourceとrevisionを含む削除filter。

            Returns:
                なし。
            """

            nonlocal delete_attempts
            delete_attempts += 1
            if delete_attempts <= 3:
                raise TemporaryQdrantError
            selector = kwargs["points_selector"].model_dump()
            source = selector["must"][0]["match"]["value"]
            revision_match = selector["must"][1]["match"]
            if "except_" in revision_match:
                kept_revision = revision_match["except_"][0]
                for point_id, payload in list(stored.items()):
                    if (
                        payload.get("source") == source
                        and payload.get("revision") != kept_revision
                    ):
                        del stored[point_id]

    client = Client()
    actual_retry = qdrant.retry_call
    monkeypatch.setattr(qdrant, "_client", lambda _settings: client)
    monkeypatch.setattr(
        qdrant,
        "retry_call",
        lambda call: actual_retry(call, lambda _delay: None),
    )
    with pytest.raises(RuntimeError, match="temporary Qdrant failure"):
        qdrant.replace_revision(configured, [point], "source.md", "new")
    assert {
        value["revision"] for value in stored.values() if value["source"] == "source.md"
    } == {"old", "new"}
    qdrant.replace_revision(configured, [point], "source.md", "new")
    assert {
        value["revision"] for value in stored.values() if value["source"] == "source.md"
    } == {"new"}
    assert stored["other"]["source"] == "other.md"
