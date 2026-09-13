"""Qdrantの検索とrevision単位の安全な置換を提供する。"""

from __future__ import annotations

from typing import Any

from qdrant_client import QdrantClient, models

from src.adapters.llm import embeddings, retry_call
from src.config import Settings


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
    """新revisionを検証後に同じsourceの旧revisionだけ削除する。

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
    client = _client(settings)
    retry_call(
        lambda: client.upsert(collection_name=collection, points=points, wait=True)
    )
    ids = [point.id for point in points]
    found = retry_call(
        lambda: client.retrieve(
            collection_name=collection,
            ids=ids,
            with_payload=False,
            with_vectors=False,
        )
    )
    if {str(item.id) for item in found} != {str(item) for item in ids}:
        raise RuntimeError("Qdrant did not persist every new revision point")
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
