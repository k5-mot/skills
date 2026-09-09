"""Obsidian CouchDB 読取 Skill の unit test。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
from pydantic import SecretStr


def _load_module() -> ModuleType:
    """test 対象 script を module として読み込む。

    Args:
        なし。

    Returns:
        読み込んだ query module。
    """
    path = Path(__file__).parents[1] / "scripts" / "query.py"
    spec = importlib.util.spec_from_file_location("obsidian_couchdb_query", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()


def _settings() -> Any:
    """test 用接続設定を生成する。

    Args:
        なし。

    Returns:
        外部通信しない test 用 Settings。
    """
    return MODULE.Settings(
        base_url="https://couchdb.example.test",
        username="reader",
        password=SecretStr("secret"),
    )


def _snapshot() -> dict[str, Any]:
    """親、leaf、除外対象を含む LiveSync snapshot を返す。

    Args:
        なし。

    Returns:
        `_all_docs` 互換 JSON。
    """
    documents = [
        {
            "_id": "note",
            "type": "plain",
            "path": "docs/design.md",
            "children": ["b", "a"],
        },
        {"_id": "a", "type": "leaf", "data": "second"},
        {"_id": "b", "type": "leaf", "data": "first "},
        {"_id": "hidden", "type": "plain", "path": ".obsidian/x.md", "children": []},
        {"_id": "internal", "type": "plain", "path": "ix:config.md", "children": []},
    ]
    return {"rows": [{"doc": document} for document in documents]}


def test_search_restores_leaf_order_and_uses_only_get() -> None:
    """検索は leaf 順を保ち、CouchDB へ GET 以外を送らない。

    Args:
        なし。

    Returns:
        なし。
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """CouchDB snapshot 応答を返し request を記録する。

        Args:
            request: HTTPX から受け取った request。

        Returns:
            `_all_docs` の test 応答。

        Side Effects:
            request を検証用配列へ追加する。
        """
        requests.append(request)
        return httpx.Response(200, json=_snapshot())

    reader = MODULE.CouchDbReader(_settings(), transport=httpx.MockTransport(handler))
    try:
        result = reader.search("first second")
        note = reader.read("docs/design.md")
    finally:
        reader.close()

    assert result["count"] == 1
    assert note["content"] == "first second"
    assert all(request.method == "GET" for request in requests)
    assert requests[0].url.path == "/obsidian/_all_docs"
    assert requests[0].headers["authorization"].startswith("Basic ")


def test_hidden_and_internal_paths_are_excluded() -> None:
    """hidden path と `ix:` path は一覧へ含めない。

    Args:
        なし。

    Returns:
        なし。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """path 除外確認用 snapshot を返す。

        Args:
            request: HTTPX request。

        Returns:
            `_all_docs` の test 応答。
        """
        return httpx.Response(200, json=_snapshot())

    reader = MODULE.CouchDbReader(_settings(), transport=httpx.MockTransport(handler))
    try:
        result = reader.list_notes()
    finally:
        reader.close()

    assert result["paths"] == ["docs/design.md"]


def test_domain_without_scheme_uses_https() -> None:
    """domain だけの設定値には HTTPS scheme を補う。

    Args:
        なし。

    Returns:
        なし。
    """
    assert (
        MODULE._normalize_base_url("couchdb.example.test")
        == "https://couchdb.example.test"
    )
