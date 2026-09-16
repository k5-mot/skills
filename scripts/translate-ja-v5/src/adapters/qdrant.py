"""Qdrantの検索とrevision単位の安全な置換を提供する。"""

from __future__ import annotations

from typing import Any

from qdrant_client import QdrantClient, models

from src.adapters.llm import embeddings, retry_call
from src.config import Settings

QDRANT_BATCH_SIZE = 64


def _client(settings: Settings) -> QdrantClient:
    """設定からQdrant clientを作る。

    Args:
        settings: Qdrant接続設定。

    Returns:
        Qdrant client。
    """

    if not settings.qdrant_url:
        raise ValueError("QDRANT_URL is required")
    return QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)


def search(settings: Settings, query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Review根拠を既存collectionから検索する。

    Args:
        settings: QdrantとEmbedding設定。
        query: 検索文字列。
        limit: 最大結果件数。

    Returns:
        text、source metadata、scoreを持つ結果列。
    """

    collection = settings.qdrant_collection
    if not collection:
        return []
    vector = embeddings(settings, [query])[0]
    client = _client(settings)
    response = retry_call(
        lambda: client.query_points(
            collection_name=collection,
            query=vector,
            limit=limit,
            with_payload=True,
        )
    )
    return [
        {
            "text": str((point.payload or {}).get("text", "")),
            "source": (point.payload or {}).get("source"),
            "revision": (point.payload or {}).get("revision"),
            "score": point.score,
        }
        for point in response.points
    ]


def replace_revision(
    settings: Settings, points: list[models.PointStruct], source: str, revision: str
) -> None:
    """新point検証後に旧revisionと同一revisionの余剰pointを削除する。

    Args:
        settings: Qdrant接続とcollection設定。
        points: 新revisionの全point。
        source: 同一文書を識別する相対path。
        revision: 新しいfile SHA-256。

    Returns:
        なし。

    Raises:
        RuntimeError: upsert後に全pointを取得できない場合。
    """

    collection = settings.qdrant_collection
    if not collection:
        raise ValueError("QDRANT_COLLECTION is required")
    if not points:
        raise ValueError("at least one Qdrant point is required")
    client = _client(settings)
    if not retry_call(lambda: client.collection_exists(collection_name=collection)):
        vector = points[0].vector
        if not isinstance(vector, list) or not vector:
            raise ValueError("a non-empty dense vector is required")
        retry_call(
            lambda: client.create_collection(
                collection_name=collection,
                vectors_config=models.VectorParams(
                    size=len(vector), distance=models.Distance.COSINE
                ),
            )
        )
    # 大きな文書もrequest上限へ達しないよう、固定件数ずつ直列登録する。
    for start in range(0, len(points), QDRANT_BATCH_SIZE):
        batch = points[start : start + QDRANT_BATCH_SIZE]
        retry_call(
            lambda batch=batch: client.upsert(
                collection_name=collection, points=batch, wait=True
            )
        )
    ids = [point.id for point in points]
    found_ids: set[str] = set()
    for start in range(0, len(ids), QDRANT_BATCH_SIZE):
        batch = ids[start : start + QDRANT_BATCH_SIZE]
        found = retry_call(
            lambda batch=batch: client.retrieve(
                collection_name=collection,
                ids=batch,
                with_payload=False,
                with_vectors=False,
            )
        )
        found_ids.update(str(item.id) for item in found)
    if found_ids != {str(item) for item in ids}:
        raise RuntimeError("Qdrant did not persist every new revision point")
    # 同じcollection・sourceの新revisionだけを残し、他文書には触れない。
    old_filter = models.Filter(
        must=[
            models.FieldCondition(key="source", match=models.MatchValue(value=source)),
            models.FieldCondition(
                key="revision",
                match=models.MatchExcept.model_validate({"except": [revision]}),
            ),
        ]
    )
    retry_call(
        lambda: client.delete(
            collection_name=collection,
            points_selector=old_filter,
            wait=True,
        )
    )
    # chunk方針変更で件数が減っても、同じfile revisionの古い末尾を残さない。
    stale_tail_filter = models.Filter(
        must=[
            models.FieldCondition(key="source", match=models.MatchValue(value=source)),
            models.FieldCondition(
                key="revision", match=models.MatchValue(value=revision)
            ),
            models.FieldCondition(key="chunk", range=models.Range(gte=len(points))),
        ]
    )
    retry_call(
        lambda: client.delete(
            collection_name=collection,
            points_selector=stale_tail_filter,
            wait=True,
        )
    )
