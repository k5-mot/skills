"""Obsidian Self-hosted LiveSync の CouchDB を読み取り専用で検索する。"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
import typer
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, SecretStr

logger = logging.getLogger(__name__)
app = typer.Typer(add_completion=False, no_args_is_help=True)


class Settings(BaseModel):
    """CouchDB の接続設定を保持する。"""

    model_config = ConfigDict(frozen=True)

    base_url: str
    username: str
    password: SecretStr
    database: str = "obsidian"
    timeout_seconds: float = 60.0
    max_notes: int = 5_000


class Note(BaseModel):
    """復元済み Obsidian ノートを表す。"""

    model_config = ConfigDict(frozen=True)

    path: str
    content: str


class CouchDbReader:
    """GET リクエストだけを使う LiveSync ノート読取クライアント。"""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """CouchDB 読取クライアントを初期化する。

        Args:
            settings: 接続先、認証情報、取得上限を含む設定。
            transport: テスト時に差し替える HTTPX transport。

        Returns:
            なし。

        Side Effects:
            HTTP client を生成する。
        """
        self._settings = settings
        self._client = httpx.Client(
            auth=httpx.BasicAuth(
                settings.username,
                settings.password.get_secret_value(),
            ),
            headers={"Accept": "application/json"},
            timeout=settings.timeout_seconds,
            transport=transport,
        )
        self._notes: list[Note] | None = None

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
        """CouchDB と対象 database の読み取り状態を返す。

        Args:
            なし。

        Returns:
            CouchDB version と database の文書件数を含む辞書。

        Raises:
            RuntimeError: HTTP 応答または JSON が不正な場合。
        """
        root = self._get_json("/")
        database = self._get_json(self._database_path())
        return {
            "ok": True,
            "couchdb": root.get("couchdb"),
            "version": root.get("version"),
            "database": database.get("db_name", self._settings.database),
            "document_count": database.get("doc_count"),
            "deleted_document_count": database.get("doc_del_count"),
        }

    def list_notes(self, prefix: str = "", limit: int = 50) -> dict[str, Any]:
        """path prefix に一致するノートを一覧する。

        Args:
            prefix: 絞り込みに使う path prefix。空文字は全ノートを対象にする。
            limit: 返す最大件数。

        Returns:
            一致件数と path 配列を含む辞書。

        Raises:
            RuntimeError: snapshot の取得または復元に失敗した場合。
        """
        matches = [
            note.path for note in self._load_notes() if note.path.startswith(prefix)
        ]
        return {"ok": True, "count": len(matches), "paths": matches[:limit]}

    def search(
        self, query: str, limit: int = 20, snippet_chars: int = 240
    ) -> dict[str, Any]:
        """path と本文を大小文字を区別せず AND 検索する。

        Args:
            query: 空白で分割する検索語。最大 200 文字。
            limit: 返す最大件数。
            snippet_chars: 各結果の本文スニペット最大文字数。

        Returns:
            path、match 箇所、スニペットを持つ検索結果。

        Raises:
            ValueError: query が空、または 200 文字を超える場合。
            RuntimeError: snapshot の取得または復元に失敗した場合。
        """
        normalized = query.strip()
        if not normalized:
            raise ValueError("query must not be empty")
        if len(normalized) > 200:
            raise ValueError("query must not exceed 200 characters")
        tokens = normalized.casefold().split()
        results: list[dict[str, str]] = []
        for note in self._load_notes():
            path_text = note.path.casefold()
            content_text = note.content.casefold()
            if not all(token in path_text or token in content_text for token in tokens):
                continue
            matched_in = (
                "path" if all(token in path_text for token in tokens) else "content"
            )
            results.append(
                {
                    "path": note.path,
                    "matched_in": matched_in,
                    "snippet": self._snippet(
                        note.content, content_text, tokens, snippet_chars
                    ),
                }
            )
        results.sort(
            key=lambda result: (result["matched_in"] != "path", result["path"])
        )
        return {"ok": True, "count": len(results), "results": results[:limit]}

    def read(self, path: str, max_chars: int = 100_000) -> dict[str, Any]:
        """path が完全一致するノートを取得する。

        Args:
            path: Vault root からの Markdown path。
            max_chars: 標準出力へ含める本文の最大文字数。

        Returns:
            path、本文、切り詰め情報を含む辞書。

        Raises:
            LookupError: 指定 path のノートが存在しない場合。
            RuntimeError: snapshot の取得または復元に失敗した場合。
        """
        note = next((item for item in self._load_notes() if item.path == path), None)
        if note is None:
            raise LookupError(f"note not found: {path}")
        return {
            "ok": True,
            "path": note.path,
            "content": note.content[:max_chars],
            "truncated": len(note.content) > max_chars,
            "original_chars": len(note.content),
        }

    def _get_json(
        self,
        path: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """許可済み CouchDB path へ GET を送り JSON object を返す。

        Args:
            path: base URL 配下の固定 API path。
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
            raise RuntimeError(f"CouchDB GET failed for {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"CouchDB response is not an object: {path}")
        return payload

    def _database_path(self) -> str:
        """対象 database の URL path を生成する。

        Args:
            なし。

        Returns:
            URL encode 済み database path。
        """
        return f"/{quote(self._settings.database, safe='')}"

    def _load_notes(self) -> list[Note]:
        """LiveSync snapshot を一度だけ取得し Markdown ノートを復元する。

        Args:
            なし。

        Returns:
            path 順の復元済みノート。

        Raises:
            RuntimeError: rows、親文書、leaf 参照が不正な場合。
        """
        if self._notes is not None:
            return self._notes
        payload = self._get_json(
            f"{self._database_path()}/_all_docs",
            {"include_docs": "true"},
        )
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise RuntimeError("CouchDB `_all_docs` response has no rows array")
        documents = self._document_map(rows)
        parents = [
            document
            for document in documents.values()
            if self._is_visible_parent(document)
        ]
        if len(parents) > self._settings.max_notes:
            raise RuntimeError(
                f"visible note count exceeds limit: {len(parents)}/{self._settings.max_notes}"
            )
        self._notes = sorted(
            (self._restore(parent, documents) for parent in parents),
            key=lambda note: note.path,
        )
        return self._notes

    @staticmethod
    def _document_map(rows: list[Any]) -> dict[str, dict[str, Any]]:
        """`_all_docs` rows を document ID の辞書へ変換する。

        Args:
            rows: CouchDB の未検証 row 配列。

        Returns:
            `_id` を key とする document 辞書。

        Raises:
            RuntimeError: row または document の構造が不正な場合。
        """
        documents: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("doc"), dict):
                raise RuntimeError("CouchDB `_all_docs` row has no document object")
            document = row["doc"]
            document_id = document.get("_id")
            if isinstance(document_id, str) and document_id:
                documents[document_id] = document
        return documents

    @staticmethod
    def _is_visible_parent(document: dict[str, Any]) -> bool:
        """document が検索対象の公開 Markdown 親文書か判定する。

        Args:
            document: LiveSync document。

        Returns:
            非削除・非 hidden・非 `ix:` の Markdown 親文書なら true。
        """
        path = document.get("path")
        if document.get("type") != "plain" or document.get("deleted") is True:
            return False
        if not isinstance(path, str) or not path.casefold().endswith(".md"):
            return False
        if path.startswith("ix:"):
            return False
        return not any(part.startswith(".") for part in path.split("/"))

    @staticmethod
    def _restore(parent: dict[str, Any], documents: dict[str, dict[str, Any]]) -> Note:
        """親文書の children 順に leaf 本文を連結する。

        Args:
            parent: 復元対象の LiveSync 親文書。
            documents: ID から全 document を引く辞書。

        Returns:
            復元済み Markdown ノート。

        Raises:
            RuntimeError: path、children、leaf data が欠落または不正な場合。
        """
        parent_id = parent.get("_id")
        path = parent.get("path")
        children = parent.get("children")
        if not isinstance(parent_id, str) or not isinstance(path, str):
            raise RuntimeError("LiveSync parent has no valid _id or path")
        if not isinstance(children, list):
            raise RuntimeError(f"LiveSync parent has no children array: {parent_id}")
        chunks: list[str] = []
        for child_id in children:
            child = documents.get(child_id) if isinstance(child_id, str) else None
            if (
                not child
                or child.get("type") != "leaf"
                or not isinstance(child.get("data"), str)
            ):
                raise RuntimeError(f"LiveSync leaf is missing or invalid: {child_id}")
            chunks.append(child["data"])
        return Note(path=path, content="".join(chunks))

    @staticmethod
    def _snippet(content: str, folded: str, tokens: list[str], limit: int) -> str:
        """最初の一致箇所を中心に短い本文スニペットを作る。

        Args:
            content: 元の Markdown 本文。
            folded: 大小文字を正規化した本文。
            tokens: 正規化済み検索語。
            limit: スニペット最大文字数。

        Returns:
            改行を空白へ畳み、必要なら省略記号を付けた文字列。
        """
        positions = [folded.find(token) for token in tokens if folded.find(token) >= 0]
        center = min(positions) if positions else 0
        start = max(0, center - limit // 3)
        end = min(len(content), start + limit)
        snippet = " ".join(content[start:end].split())
        return f"{'…' if start else ''}{snippet}{'…' if end < len(content) else ''}"


def _normalize_base_url(value: str) -> str:
    """scheme 省略を補い credential を含まない base URL を返す。

    Args:
        value: 環境変数から得た CouchDB domain または URL。

    Returns:
        末尾 slash を除いた HTTP(S) URL。

    Raises:
        ValueError: host がない、scheme が HTTP(S) 以外、credential 埋込がある場合。
    """
    candidate = value.strip()
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("OBSIDIAN_COUCHDB_DOMAIN must be an HTTP(S) host or URL")
    if parsed.username or parsed.password:
        raise ValueError("credentials must not be embedded in OBSIDIAN_COUCHDB_DOMAIN")
    normalized = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
    )
    return normalized.rstrip("/")


def _settings(env_file: Path) -> Settings:
    """dotenv と環境変数から CouchDB 設定を組み立てる。

    Args:
        env_file: 読み込む dotenv file。

    Returns:
        検証済み接続設定。

    Raises:
        ValueError: 必須環境変数が不足または不正な場合。

    Side Effects:
        dotenv の値を未設定の process 環境変数へ読み込む。
    """
    load_dotenv(env_file, override=False)
    names = (
        "OBSIDIAN_COUCHDB_DOMAIN",
        "OBSIDIAN_COUCHDB_USER",
        "OBSIDIAN_COUCHDB_PASSWORD",
    )
    values = {name: os.getenv(name, "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ValueError(f"missing environment variable(s): {', '.join(missing)}")
    return Settings(
        base_url=_normalize_base_url(values["OBSIDIAN_COUCHDB_DOMAIN"]),
        username=values["OBSIDIAN_COUCHDB_USER"],
        password=SecretStr(values["OBSIDIAN_COUCHDB_PASSWORD"]),
    )


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


def _reader(env_file: Path) -> CouchDbReader:
    """CLI 用 CouchDB reader を生成する。

    Args:
        env_file: 読み込む dotenv file。

    Returns:
        初期化済み読み取り専用 client。

    Side Effects:
        dotenv を読み、HTTP client を生成する。
    """
    return CouchDbReader(_settings(env_file))


@app.command("status")
def status_command(
    env_file: Annotated[Path, typer.Option("--env", help="dotenv file path")] = Path(
        ".env"
    ),
) -> None:
    """CouchDB と対象 database の状態を取得する。

    Args:
        env_file: 認証情報を読む dotenv file。

    Returns:
        なし。

    Side Effects:
        CouchDB へ GET を送り、JSON を標準出力へ書く。
    """
    reader = _reader(env_file)
    try:
        _emit(reader.status())
    finally:
        reader.close()


@app.command("list")
def list_command(
    prefix: Annotated[str, typer.Option(help="note path prefix")] = "",
    limit: Annotated[int, typer.Option(min=1, max=500)] = 50,
    env_file: Annotated[Path, typer.Option("--env", help="dotenv file path")] = Path(
        ".env"
    ),
) -> None:
    """対象ノートの path を一覧する。

    Args:
        prefix: 絞り込む note path prefix。
        limit: 返す最大件数。
        env_file: 認証情報を読む dotenv file。

    Returns:
        なし。

    Side Effects:
        CouchDB へ GET を送り、JSON を標準出力へ書く。
    """
    reader = _reader(env_file)
    try:
        _emit(reader.list_notes(prefix, limit))
    finally:
        reader.close()


@app.command("search")
def search_command(
    query: Annotated[str, typer.Argument(help="search terms")],
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    snippet_chars: Annotated[int, typer.Option(min=80, max=2_000)] = 240,
    env_file: Annotated[Path, typer.Option("--env", help="dotenv file path")] = Path(
        ".env"
    ),
) -> None:
    """ノートの path と本文を検索する。

    Args:
        query: 空白区切りの検索語。
        limit: 返す最大件数。
        snippet_chars: 本文スニペット最大文字数。
        env_file: 認証情報を読む dotenv file。

    Returns:
        なし。

    Side Effects:
        CouchDB へ GET を送り、JSON を標準出力へ書く。
    """
    reader = _reader(env_file)
    try:
        _emit(reader.search(query, limit, snippet_chars))
    finally:
        reader.close()


@app.command("read")
def read_command(
    path: Annotated[str, typer.Argument(help="exact Markdown note path")],
    max_chars: Annotated[int, typer.Option(min=1, max=1_000_000)] = 100_000,
    env_file: Annotated[Path, typer.Option("--env", help="dotenv file path")] = Path(
        ".env"
    ),
) -> None:
    """完全一致するノート本文を取得する。

    Args:
        path: Vault root からの Markdown path。
        max_chars: 返す本文の最大文字数。
        env_file: 認証情報を読む dotenv file。

    Returns:
        なし。

    Side Effects:
        CouchDB へ GET を送り、JSON を標準出力へ書く。
    """
    reader = _reader(env_file)
    try:
        _emit(reader.read(path, max_chars))
    finally:
        reader.close()


def main(argv: list[str] | None = None) -> int:
    """CLI を実行して終了 code を返す。

    Args:
        argv: コマンドライン引数。None の場合は process 引数を使う。

    Returns:
        成功時 0、失敗時 1。

    Side Effects:
        dotenv を読み、CouchDB へ GET を送り、結果または error を出力する。
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
        logger.error("CouchDB read failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    started_at = time.perf_counter()
    exit_code = main()
    logger.info("Elapsed time: %.3fs", time.perf_counter() - started_at)
    sys.exit(exit_code)
