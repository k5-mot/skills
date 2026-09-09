"""LLMwiki Viewer 読取 Skill の unit test。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import httpx


def _load_module() -> ModuleType:
    """test 対象 script を module として読み込む。

    Args:
        なし。

    Returns:
        読み込んだ client module。
    """
    path = Path(__file__).parents[1] / "scripts" / "client.py"
    spec = importlib.util.spec_from_file_location("llmwiki_read_client", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()


def test_status_and_search_use_only_get() -> None:
    """状態確認と検索は固定 Viewer path へ GET だけを送る。

    Args:
        なし。

    Returns:
        なし。
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """path に対応する Viewer test 応答を返す。

        Args:
            request: HTTPX request。

        Returns:
            health または search 応答。

        Side Effects:
            request を検証用配列へ追加する。
        """
        requests.append(request)
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"results": []})

    viewer = MODULE.LlmWikiViewer(
        MODULE.Settings(base_url="https://wiki.example.test"),
        transport=httpx.MockTransport(handler),
    )
    try:
        assert viewer.status()["status"] == "ok"
        assert viewer.search("design")["results"] == []
    finally:
        viewer.close()

    assert [request.method for request in requests] == ["GET", "GET"]
    assert requests[1].url.path == "/api/search"
    assert requests[1].url.params["q"] == "design"


def test_list_filters_title_and_slug() -> None:
    """一覧の query は title と slug の両方へ部分一致する。

    Args:
        なし。

    Returns:
        なし。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """二つの page を持つ一覧応答を返す。

        Args:
            request: HTTPX request。

        Returns:
            `/api/pages` test 応答。
        """
        return httpx.Response(
            200,
            json={
                "pages": [
                    {"title": "System Design", "slug": "system-design"},
                    {"title": "Runbook", "slug": "operations"},
                ]
            },
        )

    viewer = MODULE.LlmWikiViewer(
        MODULE.Settings(base_url="https://wiki.example.test"),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = viewer.list_pages("design")
    finally:
        viewer.close()

    assert result["count"] == 1
    assert result["pages"][0]["slug"] == "system-design"


def test_read_page_encodes_slug_and_rejects_path() -> None:
    """page slug は URL encode し、複数 path segment は拒否する。

    Args:
        なし。

    Returns:
        なし。
    """
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """page path を記録して test 応答を返す。

        Args:
            request: HTTPX request。

        Returns:
            page test 応答。

        Side Effects:
            path を検証用配列へ追加する。
        """
        paths.append(request.url.raw_path.decode())
        return httpx.Response(200, json={"slug": "日本語"})

    viewer = MODULE.LlmWikiViewer(
        MODULE.Settings(base_url="https://wiki.example.test"),
        transport=httpx.MockTransport(handler),
    )
    try:
        assert viewer.read_page("concepts", "日本語")["slug"] == "日本語"
        try:
            viewer.read_page("concepts", "bad/path")
        except ValueError:
            pass
        else:
            raise AssertionError("path-like slug must be rejected")
    finally:
        viewer.close()

    assert paths == ["/api/page/concepts/%E6%97%A5%E6%9C%AC%E8%AA%9E"]
