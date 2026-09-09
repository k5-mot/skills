"""inferlab LLMwiki Viewer を読み取り専用で参照する。"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
import typer
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)
app = typer.Typer(add_completion=False, no_args_is_help=True)


class Settings(BaseModel):
    """LLMwiki Viewer の接続設定を保持する。"""

    model_config = ConfigDict(frozen=True)

    base_url: str
    timeout_seconds: float = 30.0


class LlmWikiViewer:
    """GET だけを許可する LLMwiki Viewer client。"""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Viewer client を初期化する。

        Args:
            settings: Viewer base URL と timeout の設定。
            transport: テスト時に差し替える HTTPX transport。

        Returns:
            なし。

        Side Effects:
            HTTP client を生成する。
        """
        self._settings = settings
        self._client = httpx.Client(
            headers={"Accept": "application/json"},
            timeout=settings.timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        """保持している HTTP client を閉じる。

        Args:
            なし。

        Returns:
            なし。

        Side Effects:
            HTTP 接続資源を解放する。
        """
        self._client.close()

    def status(self) -> dict[str, Any]:
        """Viewer snapshot の health 情報を取得する。

        Args:
            なし。

        Returns:
            `/api/health` の JSON object。

        Raises:
            RuntimeError: HTTP 応答または JSON が不正な場合。
        """
        return self._get_json("/api/health")

    def list_pages(self, query: str = "", limit: int = 50) -> dict[str, Any]:
        """Viewer snapshot のページ一覧を title または slug で絞り込む。

        Args:
            query: 大小文字を区別しない title・slug の部分一致文字列。
            limit: 返す最大件数。

        Returns:
            一致件数とページ情報を含む辞書。

        Raises:
            RuntimeError: Viewer 応答の pages が不正な場合。
        """
        payload = self._get_json("/api/pages")
        pages = payload.get("pages")
        if not isinstance(pages, list):
            raise RuntimeError("LLMwiki `/api/pages` response has no pages array")
        token = query.strip().casefold()
        matches = [
            page
            for page in pages
            if isinstance(page, dict)
            and (
                not token
                or token in str(page.get("title", "")).casefold()
                or token in str(page.get("slug", "")).casefold()
            )
        ]
        return {"ok": True, "count": len(matches), "pages": matches[:limit]}

    def search(self, query: str) -> dict[str, Any]:
        """Viewer snapshot を lexical AND 検索する。

        Args:
            query: 最大 200 文字の検索文字列。

        Returns:
            title hit を優先した最大 50 件の検索結果。

        Raises:
            ValueError: query が空、または 200 文字を超える場合。
            RuntimeError: HTTP 応答または JSON が不正な場合。
        """
        normalized = query.strip()
        if not normalized:
            raise ValueError("query must not be empty")
        if len(normalized) > 200:
            raise ValueError("query must not exceed 200 characters")
        return self._get_json("/api/search", {"q": normalized})

    def read_page(
        self, directory: Literal["concepts", "queries"], slug: str
    ) -> dict[str, Any]:
        """一つの compiled wiki page を取得する。

        Args:
            directory: `concepts` または `queries`。
            slug: 拡張子を含まない page slug。

        Returns:
            sanitized HTML、citation、frontmatter、freshness を含む page payload。

        Raises:
            ValueError: directory または slug が不正な場合。
            RuntimeError: HTTP 応答または JSON が不正な場合。
        """
        if directory not in {"concepts", "queries"}:
            raise ValueError("directory must be concepts or queries")
        if not slug or "/" in slug or slug in {".", ".."}:
            raise ValueError("slug must be one non-empty path segment")
        encoded_slug = quote(slug, safe="")
        return self._get_json(f"/api/page/{directory}/{encoded_slug}")

    def index(self) -> dict[str, Any]:
        """自動生成された wiki index を取得する。

        Args:
            なし。

        Returns:
            sanitized HTML と outgoing link を含む index payload。

        Raises:
            RuntimeError: index がない、または HTTP 応答が不正な場合。
        """
        return self._get_json("/api/index")

    def _get_json(
        self,
        path: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """固定された Viewer API path へ GET を送る。

        Args:
            path: 呼出元が選んだ許可済み API path。
            params: GET query parameter。

        Returns:
            JSON object として検証した応答。

        Raises:
            RuntimeError: HTTP 応答または JSON 形式が不正な場合。
        """
        url = f"{self._settings.base_url}{path}"
        try:
            response = self._client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"LLMwiki Viewer GET failed for {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"LLMwiki Viewer response is not an object: {path}")
        return payload


def _normalize_base_url(value: str) -> str:
    """Viewer base URL を HTTP(S) URL として検証する。

    Args:
        value: 環境変数から得た URL。

    Returns:
        credential、query、fragment、末尾 slash を含まない URL。

    Raises:
        ValueError: URL が HTTP(S) でない、host がない、credential を含む場合。
    """
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("LLM_WIKI_COMPILER_SERVE_URL must be an HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError(
            "credentials must not be embedded in LLM_WIKI_COMPILER_SERVE_URL"
        )
    normalized = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
    )
    return normalized.rstrip("/")


def _settings(env_file: Path) -> Settings:
    """dotenv と環境変数から Viewer 設定を組み立てる。

    Args:
        env_file: 読み込む dotenv file。

    Returns:
        検証済み Viewer 設定。

    Raises:
        ValueError: URL 環境変数が未設定または不正な場合。

    Side Effects:
        dotenv の値を未設定の process 環境変数へ読み込む。
    """
    load_dotenv(env_file, override=False)
    value = os.getenv("LLM_WIKI_COMPILER_SERVE_URL", "").strip()
    if not value:
        raise ValueError("missing environment variable: LLM_WIKI_COMPILER_SERVE_URL")
    return Settings(base_url=_normalize_base_url(value))


def _viewer(env_file: Path) -> LlmWikiViewer:
    """CLI 用 Viewer client を生成する。

    Args:
        env_file: 読み込む dotenv file。

    Returns:
        初期化済み読み取り専用 client。

    Side Effects:
        dotenv を読み、HTTP client を生成する。
    """
    return LlmWikiViewer(_settings(env_file))


def _emit(payload: dict[str, Any]) -> None:
    """CLI の最終 JSON 結果を標準出力へ書く。

    Args:
        payload: JSON serializable な結果。

    Returns:
        なし。

    Side Effects:
        UTF-8 JSON を標準出力へ書く。
    """
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("status")
def status_command(
    env_file: Annotated[Path, typer.Option("--env", help="dotenv file path")] = Path(
        ".env"
    ),
) -> None:
    """LLMwiki Viewer の health を取得する。

    Args:
        env_file: Viewer URL を読む dotenv file。

    Returns:
        なし。

    Side Effects:
        Viewer へ GET を送り、JSON を標準出力へ書く。
    """
    viewer = _viewer(env_file)
    try:
        _emit(viewer.status())
    finally:
        viewer.close()


@app.command("list")
def list_command(
    query: Annotated[str, typer.Option(help="title or slug substring")] = "",
    limit: Annotated[int, typer.Option(min=1, max=500)] = 50,
    env_file: Annotated[Path, typer.Option("--env", help="dotenv file path")] = Path(
        ".env"
    ),
) -> None:
    """Compiled page を一覧する。

    Args:
        query: title または slug の部分一致文字列。
        limit: 返す最大件数。
        env_file: Viewer URL を読む dotenv file。

    Returns:
        なし。

    Side Effects:
        Viewer へ GET を送り、JSON を標準出力へ書く。
    """
    viewer = _viewer(env_file)
    try:
        _emit(viewer.list_pages(query, limit))
    finally:
        viewer.close()


@app.command("search")
def search_command(
    query: Annotated[str, typer.Argument(help="lexical AND search query")],
    env_file: Annotated[Path, typer.Option("--env", help="dotenv file path")] = Path(
        ".env"
    ),
) -> None:
    """Viewer snapshot の title と本文を検索する。

    Args:
        query: 最大 200 文字の検索文字列。
        env_file: Viewer URL を読む dotenv file。

    Returns:
        なし。

    Side Effects:
        Viewer へ GET を送り、JSON を標準出力へ書く。
    """
    viewer = _viewer(env_file)
    try:
        _emit(viewer.search(query))
    finally:
        viewer.close()


@app.command("read")
def read_command(
    directory: Annotated[Literal["concepts", "queries"], typer.Argument()],
    slug: Annotated[str, typer.Argument(help="page slug without .md")],
    env_file: Annotated[Path, typer.Option("--env", help="dotenv file path")] = Path(
        ".env"
    ),
) -> None:
    """一つの compiled page を取得する。

    Args:
        directory: `concepts` または `queries`。
        slug: 拡張子なしの page slug。
        env_file: Viewer URL を読む dotenv file。

    Returns:
        なし。

    Side Effects:
        Viewer へ GET を送り、JSON を標準出力へ書く。
    """
    viewer = _viewer(env_file)
    try:
        _emit(viewer.read_page(directory, slug))
    finally:
        viewer.close()


@app.command("index")
def index_command(
    env_file: Annotated[Path, typer.Option("--env", help="dotenv file path")] = Path(
        ".env"
    ),
) -> None:
    """LLMwiki の自動生成 index を取得する。

    Args:
        env_file: Viewer URL を読む dotenv file。

    Returns:
        なし。

    Side Effects:
        Viewer へ GET を送り、JSON を標準出力へ書く。
    """
    viewer = _viewer(env_file)
    try:
        _emit(viewer.index())
    finally:
        viewer.close()


def main(argv: list[str] | None = None) -> int:
    """CLI を実行して終了 code を返す。

    Args:
        argv: コマンドライン引数。None の場合は process 引数を使う。

    Returns:
        成功時 0、失敗時 1。

    Side Effects:
        dotenv を読み、Viewer へ GET を送り、結果または error を出力する。
    """
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(levelname)s:%(name)s:file=%(pathname)s:"
            "function=%(funcName)s:line=%(lineno)d:%(message)s"
        ),
    )
    try:
        app(args=argv, standalone_mode=False)
    except Exception as exc:
        logger.error("LLMwiki read failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    started_at = time.perf_counter()
    exit_code = main()
    logger.info("Elapsed time: %.3fs", time.perf_counter() - started_at)
    sys.exit(exit_code)
