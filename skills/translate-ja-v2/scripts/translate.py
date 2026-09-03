"""translate-ja-v2 の文書翻訳パイプラインを実行する。"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import csv
from difflib import SequenceMatcher
from functools import partial
from io import BytesIO
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from time import perf_counter
from typing import Annotated, Any, Callable, cast
from urllib.parse import unquote, urlsplit

import typer
import pypdfium2 as pdfium
from pydantic import BaseModel, ConfigDict, Field

LOGGER = logging.getLogger("translate-ja-v2")

HEADING_LABELS = {"title", "section_header", "heading", "header"}
CODE_LABELS = {"code", "program_listing"}
URL_RE = re.compile(r"https?://[^\s)>\"]+")
JAPANESE_TEXT_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
REVIEW_META_CONTEXT_MARKERS = (
    "レビュー対象",
    "翻訳対象",
    "現在の日本語訳",
    "前の日本語訳",
    "次の日本語訳",
)
REVIEW_META_FAILURE_MARKERS = (
    "入力内容が不足",
    "入力されていません",
    "提供されていません",
    "ご提示ください",
)
LOG_LEVEL = "DEBUG"
DOCLING_TIMEOUT_SECONDS = 21600
PAGE_IMAGE_SCALE = 1.0
DOCLING_PDF_CHUNK_PAGES = 10
DOCLING_COLLECTION_KEYS = (
    "groups",
    "texts",
    "pictures",
    "tables",
    "key_value_items",
    "form_items",
)
DOCLING_COLLECTION_REF_RE = re.compile(
    r"^#/(groups|texts|pictures|tables|key_value_items|form_items)/(\d+)$"
)
OPENAI_TIMEOUT_SECONDS = 1800
OPENAI_MAX_ATTEMPTS = 6
OPENAI_RETRY_INITIAL_SECONDS = 5.0
OPENAI_RETRY_MAX_SECONDS = 60.0
OPENAI_CONTEXT_LIMIT_CHARS = 50000
OPENAI_MAX_OUTPUT_TOKENS = 4096
OPENAI_BATCH_MAX_OUTPUT_TOKENS = 16384
OPENAI_SAFE_OUTPUT_CHARS = 12000
OPENAI_BATCH_MAX_ITEMS = 20
TRANSLATION_BATCH_MAX_CHARS = 1500
REVIEW_MAX_WORKERS = 4
PIPELINE_STAGES = (
    "parse",
    "normalize",
    "structure",
    "clean",
    "translate",
    "review",
    "markdown",
    "docx",
)
DEFAULT_TRANSLATION_RULES = """\
- 原文にない説明、要約、事実追加は禁止。
- 固有名詞、製品名、API名、コード、URL、パス、識別子、コマンドは英語のまま保持する。
- 用語集に一致する語は japanese を優先し、文脈上必要な場合だけ自然な助詞を補う。
- Markdown記号や表の区切り記号を追加しない。
"""


class OpenAIEmptyResponseError(RuntimeError):
    """OpenAI 互換 API が本文を返さなかったことを表す。"""


class ColorFormatter(logging.Formatter):
    """ログレベル名へANSI色を付けるFormatter。"""

    COLORS = {
        logging.DEBUG: "\033[36m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[35m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        """色付きレベル名を持つログ文字列を作る。

        Args:
            record: loggingが生成したログレコード。

        Returns:
            レベル名へANSI色を付けたログ文字列。
        """

        colored_record = copy.copy(record)
        color = self.COLORS.get(record.levelno, "")
        if color:
            colored_record.levelname = f"{color}{record.levelname}{self.RESET}"
        return super().format(colored_record)


class FrozenModel(BaseModel):
    """変更不可の Pydantic モデル基底を定義する。

    Args:
        なし。

    Returns:
        凍結された Pydantic モデル。
    """

    model_config = ConfigDict(frozen=True)


class DoclingSettings(FrozenModel):
    """Docling Serve の接続設定を保持する。

    Args:
        server_url: Docling Serve のベース URL。
        api_key: Docling Serve の API キー。
        timeout_seconds: リクエストと非同期 poll の最大待機秒数。

    Returns:
        Docling 接続に必要な設定値。
    """

    server_url: str
    api_key: str
    timeout_seconds: int


class OpenAISettings(FrozenModel):
    """OpenAI 互換 Chat Completions API の接続設定を保持する。

    Args:
        base_url: API のベース URL。
        api_key: API キー。
        model: 構造補正と翻訳に使うモデル名。
        timeout_seconds: API 呼び出しの timeout 秒数。
        context_chars: request に含めるテキストの最大文字数。

    Returns:
        OpenAI 互換 API の呼び出しに必要な設定値。
    """

    base_url: str
    api_key: str
    model: str
    timeout_seconds: int
    context_chars: int = Field(default=OPENAI_CONTEXT_LIMIT_CHARS, ge=1)


class StagePaths(FrozenModel):
    """translate-ja-v2 の主要出力パスを保持する。

    Args:
        output_dir: 成果物の親ディレクトリ。
        document_json: Docling 変換直後の JSON。
        normalized_json: 決定論的整形後の JSON。
        structured_json: VLM 構造補正後の JSON。
        cleaned_json: 句読点校正後の JSON。
        translated_json: 翻訳情報を付与した JSON。
        reviewed_json: レビュー済み翻訳情報を付与した JSON。
        markdown: 日本語 Markdown。
        docx: Word docx。
        manifest: 実行 manifest。

    Returns:
        各 stage の成果物パス。
    """

    output_dir: Path
    document_json: Path
    normalized_json: Path
    structured_json: Path
    cleaned_json: Path
    translated_json: Path
    reviewed_json: Path
    markdown: Path
    docx: Path
    manifest: Path


class PipelineOptions(FrozenModel):
    """translate-ja-v2 CLI オプションを保持する。

    Args:
        input: PDF/Word 入力ファイル。
        output_dir: 中間成果物の出力ディレクトリ。
        output: 最終 docx の出力パス。
        template: pandoc reference docx/dotx。
        skip_vlm: VLM による構造補正を省略するかどうか。
        skip_review: 翻訳レビューを省略するかどうか。
        skip_docx: docx 生成を省略するかどうか。
        force: 既存 Docling JSON があっても変換を再実行するかどうか。
        env: dotenv ファイルのパス。
        glossary: CSV 用語集のパス。
        translation_rules: 翻訳ルール Markdown のパス。
        context_chars: OpenAI request の最大テキスト文字数。
        batch_chars: 翻訳・Reviewバッチの最大原文・訳文文字数。

    Returns:
        パイプライン実行に必要な CLI オプション。
    """

    input: Path
    output_dir: Path | None = None
    output: Path | None = None
    template: Path | None = None
    skip_vlm: bool = False
    skip_review: bool = False
    skip_docx: bool = False
    force: bool = False
    env: Path = Path(".env")
    glossary: Path | None = None
    translation_rules: Path | None = None
    context_chars: int = Field(default=OPENAI_CONTEXT_LIMIT_CHARS, ge=1)
    batch_chars: int = Field(default=TRANSLATION_BATCH_MAX_CHARS, ge=1)


def configure_logging(level_name: str | None = None) -> None:
    """標準 logging を translate-ja-v2 用に設定する。

    Args:
        level_name: 明示するログレベル。None の場合はDEBUGを使う。

    Returns:
        なし。

    Side Effects:
        アプリloggerを指定レベル、外部loggerをWARNING以上に設定する。
    """

    level = getattr(
        logging,
        (level_name or LOG_LEVEL).upper(),
        logging.DEBUG,
    )
    formatter = ColorFormatter(
        "%(asctime)s %(levelname)s %(name)s "
        "file=%(pathname)s function=%(funcName)s line=%(lineno)d %(message)s"
    )
    root_handler = logging.StreamHandler()
    root_handler.setLevel(logging.WARNING)
    root_handler.setFormatter(formatter)
    logging.basicConfig(
        level=logging.WARNING,
        handlers=[root_handler],
        force=True,
    )
    app_handler = logging.StreamHandler()
    app_handler.setLevel(level)
    app_handler.setFormatter(formatter)
    LOGGER.handlers.clear()
    LOGGER.addHandler(app_handler)
    LOGGER.setLevel(level)
    LOGGER.propagate = False


def load_dotenv_file(path: str | Path = ".env") -> None:
    """python-dotenv で .env を環境変数へ読み込む。

    Args:
        path: 読み込む .env ファイル。

    Returns:
        なし。

    Side Effects:
        既存環境変数を上書きせず、未設定のキーだけ os.environ へ追加する。
    """

    from dotenv import load_dotenv as python_dotenv_load_dotenv

    env_path = Path(path)
    python_dotenv_load_dotenv(dotenv_path=env_path, override=False)


def env_first(*names: str, default: str | None = None) -> str | None:
    """複数の環境変数から最初に設定されている値を返す。

    Args:
        *names: 優先順の環境変数名。
        default: どの環境変数も未設定の場合に返す値。

    Returns:
        最初に見つかった環境変数値、または default。
    """

    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def require_docling_settings() -> DoclingSettings:
    """Docling Serve の必須設定を環境変数から読み込む。

    Args:
    Returns:
        DoclingSettings。

    Raises:
        RuntimeError: 必須環境変数が未設定の場合。
    """

    server_url = env_first("DOCLING_SERVER_URL", "DOCLING_SERVE_URL")
    api_key = env_first("DOCLING_API_KEY", "DOCLING_SERVE_API_KEY")
    if not server_url:
        raise RuntimeError("DOCLING_SERVER_URL or DOCLING_SERVE_URL is required")
    if not api_key:
        raise RuntimeError("DOCLING_API_KEY or DOCLING_SERVE_API_KEY is required")
    return DoclingSettings(
        server_url=server_url.rstrip("/"),
        api_key=api_key,
        timeout_seconds=DOCLING_TIMEOUT_SECONDS,
    )


def require_openai_settings(
    context_chars: int = OPENAI_CONTEXT_LIMIT_CHARS,
) -> OpenAISettings:
    """OpenAI 互換 API の必須設定を環境変数から読み込む。

    Args:
        context_chars: request に含めるテキストの最大文字数。
    Returns:
        OpenAISettings。

    Raises:
        RuntimeError: 必須環境変数が未設定の場合。
    """

    base_url = os.environ.get("OPENAI_BASE_URL")
    api_key = os.environ.get("OPENAI_API_KEY")
    model = os.environ.get("OPENAI_MODEL")
    if not base_url:
        raise RuntimeError("OPENAI_BASE_URL is required")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required")
    if not model:
        raise RuntimeError("OPENAI_MODEL is required")
    return OpenAISettings(
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        model=model,
        timeout_seconds=OPENAI_TIMEOUT_SECONDS,
        context_chars=context_chars,
    )


def utc_now_iso() -> str:
    """現在時刻を UTC ISO 8601 文字列で返す。

    Returns:
        秒精度の UTC ISO 8601 文字列。
    """

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_write_bytes(path: str | Path, content: bytes) -> None:
    """bytes を flush/fsync 後に atomic rename で保存する。

    Args:
        path: 保存先パス。
        content: 保存する bytes。

    Returns:
        なし。

    Side Effects:
        保存先の親ディレクトリを作成し、既存ファイルを置換する。
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def write_json(path: str | Path, data: Any) -> None:
    """JSON を UTF-8 で atomic 保存する。

    Args:
        path: 保存先パス。
        data: JSON serializable な値。

    Returns:
        なし。

    Side Effects:
        保存先ファイルを置換する。
    """

    content = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    atomic_write_bytes(path, content)


def read_json(path: str | Path) -> Any:
    """UTF-8 JSON ファイルを読み込む。

    Args:
        path: 読み込む JSON ファイル。

    Returns:
        JSON として読み込んだ Python 値。
    """

    with Path(path).open("r", encoding="utf-8") as file:
        return json.load(file)


def read_glossary_csv(path: Path | None) -> list[dict[str, str]]:
    """翻訳用語集 CSV を読み込む。

    Args:
        path: english,japanese,desc,genre,note 列を持つ CSV パス。

    Returns:
        空でない english と japanese を持つ用語 dict 配列。
    """

    if path is None:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        result: list[dict[str, str]] = []
        for row in reader:
            entry = {
                key: str(row.get(key) or "").strip()
                for key in ("english", "japanese", "desc", "genre", "note")
            }
            if entry["english"] and entry["japanese"]:
                result.append(entry)
        return result


def read_translation_rules(path: Path | None) -> str:
    """翻訳ルール本文を読み込む。

    Args:
        path: Markdown などのテキストファイル。None の場合は既定ルール。

    Returns:
        LLM に渡す翻訳ルール本文。
    """

    if path is None:
        return DEFAULT_TRANSLATION_RULES
    return path.read_text(encoding="utf-8").strip()


def sha256_file(path: str | Path) -> str:
    """ファイル内容の SHA-256 を返す。

    Args:
        path: digest 対象のファイル。

    Returns:
        hex 形式の SHA-256 digest。
    """

    import hashlib

    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: Any) -> str:
    """JSON値を正規化してSHA-256を返す。

    Args:
        value: JSON serializableな値。

    Returns:
        canonical JSONのSHA-256 digest。
    """

    import hashlib

    content = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def sha256_directory(path: Path) -> str:
    """ディレクトリ内の相対パスとファイル内容からSHA-256を返す。

    Args:
        path: digest対象のディレクトリ。

    Returns:
        ディレクトリ内容のSHA-256 digest。

    Raises:
        FileNotFoundError: ディレクトリが存在しない場合。
    """

    import hashlib

    if not path.is_dir():
        raise FileNotFoundError(f"directory not found: {path}")
    digest = hashlib.sha256()
    for item in sorted(
        candidate for candidate in path.rglob("*") if candidate.is_file()
    ):
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with item.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def sha256_path(path: Path) -> str:
    """ファイルまたはディレクトリのSHA-256を返す。

    Args:
        path: digest対象のパス。

    Returns:
        対象内容のSHA-256 digest。
    """

    return sha256_directory(path) if path.is_dir() else sha256_file(path)


def build_stage_paths(
    input_path: Path, output_dir: Path | None, output_docx: Path | None
) -> StagePaths:
    """入力ファイル名から translate-ja-v2 の出力パスを作る。

    Args:
        input_path: 入力 PDF/Word パス。
        output_dir: 明示された成果物ディレクトリ。None の場合は outputs/<stem>。
        output_docx: 明示された docx 出力パス。None の場合は output_dir 内。

    Returns:
        StagePaths。
    """

    root = output_dir or Path.cwd() / "outputs" / input_path.stem
    docx = output_docx or root / "document.ja.docx"
    return StagePaths(
        output_dir=root,
        document_json=root / f"{input_path.stem}.json",
        normalized_json=root / "document.normalized.json",
        structured_json=root / "document.structured.json",
        cleaned_json=root / "document.cleaned.json",
        translated_json=root / "document.translated.json",
        reviewed_json=root / "document.reviewed.json",
        markdown=root / "document.ja.md",
        docx=docx,
        manifest=root / "manifest.json",
    )


def docling_form_payload(document_timeout: int) -> dict[str, str | list[str]]:
    """Docling Serve v1 multipart form payload を作る。

    Args:
        document_timeout: Docling 側の文書処理 timeout 秒数。

    Returns:
        httpx に渡す form field。
    """

    payload: dict[str, str | list[str]] = {
        "to_formats": "json",
        "do_ocr": "false",
        "force_ocr": "false",
        "ocr_preset": "tesseract",
        "ocr_lang": ["jpn", "jpn_vert", "eng"],
        "do_table_structure": "true",
        "table_mode": "accurate",
        "table_cell_matching": "true",
        "do_code_enrichment": "true",
        "do_formula_enrichment": "true",
        "document_timeout": str(document_timeout),
        "include_images": "true",
        "include_page_images": "false",
        "images_scale": str(PAGE_IMAGE_SCALE),
        "image_export_mode": "referenced",
        "target_type": "zip",
    }
    return payload


def request_docling_convert(
    endpoint: str, input_path: Path, settings: DoclingSettings, request_timeout: int
) -> Any:
    """Docling Serve へ変換 request を送る。

    Args:
        endpoint: Docling Serve の変換 endpoint。
        input_path: 変換対象ファイル。
        settings: Docling 接続設定。
        request_timeout: HTTP request の timeout 秒数。

    Returns:
        httpx.Response。

    Raises:
        RuntimeError: HTTP request に失敗した場合。
    """

    import httpx

    for file_field in ("files", "file"):
        with input_path.open("rb") as file:
            files = {file_field: (input_path.name, file)}
            response = httpx.post(
                endpoint,
                headers={"X-Api-Key": settings.api_key},
                files=files,
                data=docling_form_payload(settings.timeout_seconds),
                timeout=request_timeout,
            )
        if response.status_code not in {400, 422}:
            return response
    return response


def poll_docling_task(
    task_id: str, output_zip: Path, settings: DoclingSettings
) -> None:
    """Docling Serve の async task を poll して zip を保存する。

    Args:
        task_id: async convert が返した task id。
        output_zip: 変換結果 zip の保存先。
        settings: Docling 接続設定。

    Returns:
        なし。

    Raises:
        RuntimeError: task が失敗した場合。
        TimeoutError: timeout までに完了しない場合。
    """

    import httpx

    deadline = time.monotonic() + settings.timeout_seconds
    poll_count = 0
    while time.monotonic() < deadline:
        poll_count += 1
        response = httpx.get(
            f"{settings.server_url}/v1/status/poll/{task_id}",
            headers={"X-Api-Key": settings.api_key},
            timeout=60,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Docling status poll failed status={response.status_code}"
            )
        payload = response.json()
        status = str(payload.get("status") or payload.get("task_status") or "").lower()
        LOGGER.debug(
            "Polled Docling conversion task_id=%s poll_count=%s status=%s http_status=%s",
            task_id,
            poll_count,
            status or "unknown",
            response.status_code,
        )
        if status in {"success", "succeeded", "completed"}:
            result = httpx.get(
                f"{settings.server_url}/v1/result/{task_id}",
                headers={"X-Api-Key": settings.api_key},
                timeout=settings.timeout_seconds,
            )
            if result.status_code >= 400:
                raise RuntimeError(f"Docling result failed status={result.status_code}")
            atomic_write_bytes(output_zip, result.content)
            return
        if status in {"failure", "failed", "error"}:
            raise RuntimeError(f"Docling async task failed task_id={task_id}")
        time.sleep(10)
    raise TimeoutError(f"Docling async task timed out task_id={task_id}")


def convert_docling_file(
    input_path: Path,
    output_json: Path,
    artifacts_dir: Path,
    settings: DoclingSettings,
) -> None:
    """1つの入力ファイルをDocling ServeでJSONとartifactsへ変換する。

    Args:
        input_path: Docling Serveへ送る入力ファイル。
        output_json: Docling JSON の保存先。
        artifacts_dir: PNG などの artifact 保存先。
        settings: Docling接続設定。

    Returns:
        なし。

    Side Effects:
        Docling Serve へ HTTP request を送り、JSON と artifacts を保存する。
    """

    output_json.parent.mkdir(parents=True, exist_ok=True)
    temp_zip = output_json.with_suffix(output_json.suffix + ".docling.zip")
    try:
        endpoint = f"{settings.server_url}/v1/convert/file/async"
        response = request_docling_convert(
            endpoint, input_path, settings, request_timeout=120
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Docling async convert failed status={response.status_code} body={response.text[:500]}"
            )
        payload = response.json()
        task_id = payload.get("task_id") or payload.get("id")
        if not task_id:
            raise RuntimeError("Docling async response has no task_id")
        LOGGER.info("Started Docling async conversion task_id=%s", task_id)
        poll_docling_task(str(task_id), temp_zip, settings)
        extract_docling_zip(temp_zip, output_json, artifacts_dir)
    finally:
        temp_zip.unlink(missing_ok=True)


def write_pdf_chunk(
    source_pdf: pdfium.PdfDocument,
    page_indexes: list[int],
    output_path: Path,
) -> None:
    """元PDFの指定ページだけを含む一時PDFを作る。

    Args:
        source_pdf: 読み込み済みの元PDF。
        page_indexes: 取り込む0始まりページ番号。
        output_path: 一時PDFの保存先。

    Returns:
        なし。

    Raises:
        ValueError: ページ番号が空の場合。

    Side Effects:
        指定先へPDFファイルを保存する。
    """

    if not page_indexes:
        raise ValueError("PDF chunk must contain at least one page")
    with pdfium.PdfDocument.new() as chunk_pdf:
        chunk_pdf.import_pages(source_pdf, pages=page_indexes)
        chunk_pdf.save(output_path)


def remap_docling_chunk(
    document: dict[str, Any],
    collection_offsets: dict[str, int],
    page_offset: int,
    artifact_subdir: str,
) -> dict[str, Any]:
    """チャンク内の参照・ページ番号・artifact URIを全体座標へ変換する。

    Args:
        document: Docling Serveが返したチャンクJSON。
        collection_offsets: 各collectionの既存要素数。
        page_offset: チャンク先頭より前にあるページ数。
        artifact_subdir: チャンクartifactを格納するサブディレクトリ名。

    Returns:
        全体文書用に参照を再採番したDocling JSON。

    Raises:
        ValueError: collectionまたはpagesの形が不正な場合。
    """

    remapped = copy.deepcopy(document)

    def visit(value: Any, key: str | None = None) -> Any:
        """JSON値を再帰走査してチャンク固有値を置換する。

        Args:
            value: 現在のJSON値。
            key: 親object内のkey。

        Returns:
            必要な値を置換したJSON値。
        """

        if isinstance(value, dict):
            return {
                child_key: visit(child, child_key) for child_key, child in value.items()
            }
        if isinstance(value, list):
            return [visit(child) for child in value]
        if key in {"self_ref", "$ref"} and isinstance(value, str):
            match = DOCLING_COLLECTION_REF_RE.fullmatch(value)
            if match:
                collection, index_text = match.groups()
                return (
                    f"#/{collection}/{int(index_text) + collection_offsets[collection]}"
                )
        if key == "page_no" and isinstance(value, int) and not isinstance(value, bool):
            return value + page_offset
        if key == "uri" and isinstance(value, str) and value.startswith("artifacts/"):
            relative = value.removeprefix("artifacts/")
            return PurePosixPath("artifacts", artifact_subdir, relative).as_posix()
        return value

    remapped = visit(remapped)
    if not isinstance(remapped, dict):
        raise ValueError("Remapped Docling chunk root must be an object")
    pages = remapped.get("pages")
    if not isinstance(pages, dict):
        raise ValueError("Docling chunk pages must be an object")
    remapped["pages"] = {
        str(int(str(local_page)) + page_offset): page_data
        for local_page, page_data in pages.items()
    }
    for collection in DOCLING_COLLECTION_KEYS:
        value = remapped.get(collection, [])
        if not isinstance(value, list):
            raise ValueError(f"Docling chunk {collection} must be a list")
        remapped[collection] = value
    return remapped


def merge_docling_chunks(
    chunks: list[dict[str, Any]],
    expected_page_counts: list[int],
    input_path: Path,
) -> dict[str, Any]:
    """複数チャンクのDocling JSONを参照整合性を保って連結する。

    Args:
        chunks: ページ順に並んだチャンクJSON。
        expected_page_counts: 各チャンクに含めたPDFページ数。
        input_path: 元PDFパス。

    Returns:
        元PDF全体を表す単一のDocling JSON。

    Raises:
        ValueError: チャンク数、schema、ページ、tree構造が不正な場合。
    """

    if not chunks or len(chunks) != len(expected_page_counts):
        raise ValueError("Docling chunks and page counts must be non-empty and aligned")
    merged: dict[str, Any] | None = None
    page_offset = 0
    for chunk_index, (chunk, expected_pages) in enumerate(
        zip(chunks, expected_page_counts, strict=True), start=1
    ):
        pages = chunk.get("pages")
        if not isinstance(pages, dict) or len(pages) != expected_pages:
            raise ValueError(
                f"Docling chunk page count mismatch chunk={chunk_index} "
                f"expected={expected_pages} actual={len(pages) if isinstance(pages, dict) else 'invalid'}"
            )
        expected_local_pages = {
            str(page_no) for page_no in range(1, expected_pages + 1)
        }
        if {str(page_no) for page_no in pages} != expected_local_pages:
            raise ValueError(
                f"Docling chunk page numbers are invalid chunk={chunk_index}"
            )
        collection_offsets = {
            collection: len(merged.get(collection, [])) if merged else 0
            for collection in DOCLING_COLLECTION_KEYS
        }
        remapped = remap_docling_chunk(
            chunk,
            collection_offsets,
            page_offset,
            f"chunk_{chunk_index:06d}",
        )
        if merged is None:
            merged = remapped
        else:
            for schema_key in ("schema_name", "version"):
                if merged.get(schema_key) != remapped.get(schema_key):
                    raise ValueError(
                        f"Docling chunk {schema_key} mismatch chunk={chunk_index}"
                    )
            for collection in DOCLING_COLLECTION_KEYS:
                merged[collection].extend(remapped[collection])
            for tree_name in ("body", "furniture"):
                merged_tree = merged.get(tree_name)
                chunk_tree = remapped.get(tree_name)
                if not isinstance(merged_tree, dict) or not isinstance(
                    chunk_tree, dict
                ):
                    raise ValueError(f"Docling chunk {tree_name} must be an object")
                merged_children = merged_tree.get("children")
                chunk_children = chunk_tree.get("children")
                if not isinstance(merged_children, list) or not isinstance(
                    chunk_children, list
                ):
                    raise ValueError(
                        f"Docling chunk {tree_name}.children must be a list"
                    )
                merged_children.extend(chunk_children)
            merged_pages = merged.get("pages")
            remapped_pages = remapped.get("pages")
            if not isinstance(merged_pages, dict) or not isinstance(
                remapped_pages, dict
            ):
                raise ValueError("Docling chunk pages must be an object")
            merged_pages.update(remapped_pages)
        page_offset += expected_pages

    if merged is None:
        raise ValueError("Docling chunks are empty")
    merged["name"] = input_path.stem
    origin = merged.get("origin")
    if not isinstance(origin, dict):
        origin = {}
        merged["origin"] = origin
    origin.update(
        {
            "mimetype": "application/pdf",
            "binary_hash": int(sha256_file(input_path)[:16], 16),
            "filename": input_path.name,
        }
    )
    return merged


def replace_artifacts_directory(source: Path, destination: Path) -> None:
    """準備済みartifactディレクトリを既存成果物とatomicに入れ替える。

    Args:
        source: 同一filesystem上の準備済みartifactディレクトリ。
        destination: 最終artifactディレクトリ。

    Returns:
        なし。

    Side Effects:
        既存成果物を一時退避し、sourceをdestinationへ移動する。
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = destination.with_name(f".{destination.name}.backup-{uuid.uuid4().hex}")
    try:
        if destination.exists():
            os.replace(destination, backup)
        os.replace(source, destination)
    except Exception:
        if backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    finally:
        if backup.exists():
            shutil.rmtree(backup)


def convert_pdf_with_docling_chunks(
    input_path: Path,
    output_json: Path,
    artifacts_dir: Path,
    settings: DoclingSettings,
) -> None:
    """PDFを10ページずつDocling変換し、JSONとartifactsをローカル連結する。

    Args:
        input_path: 変換対象PDF。
        output_json: 連結済みDocling JSONの保存先。
        artifacts_dir: 連結済みartifactの保存先。
        settings: Docling接続設定。

    Returns:
        なし。

    Raises:
        ValueError: PDFが空、またはDocling JSONを安全に連結できない場合。

    Side Effects:
        Docling Serveへチャンクを直列送信し、ローカル成果物をatomic置換する。
    """

    output_json.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".docling-chunks-", dir=str(output_json.parent)
    ) as temp_dir_text:
        temp_dir = Path(temp_dir_text)
        temp_artifacts = temp_dir / "artifacts"
        chunks: list[dict[str, Any]] = []
        expected_page_counts: list[int] = []
        with pdfium.PdfDocument(input_path) as source_pdf:
            page_count = len(source_pdf)
            if page_count == 0:
                raise ValueError("PDF must contain at least one page")
            for chunk_index, start_page in enumerate(
                range(0, page_count, DOCLING_PDF_CHUNK_PAGES), start=1
            ):
                end_page = min(start_page + DOCLING_PDF_CHUNK_PAGES, page_count)
                chunk_pdf = temp_dir / f"chunk_{chunk_index:06d}.pdf"
                chunk_json = temp_dir / f"chunk_{chunk_index:06d}.json"
                chunk_artifacts = temp_artifacts / f"chunk_{chunk_index:06d}"
                write_pdf_chunk(
                    source_pdf, list(range(start_page, end_page)), chunk_pdf
                )
                LOGGER.info(
                    "Started Docling PDF chunk chunk=%s pages=%s-%s total_pages=%s",
                    chunk_index,
                    start_page + 1,
                    end_page,
                    page_count,
                )
                convert_docling_file(
                    chunk_pdf,
                    chunk_json,
                    chunk_artifacts,
                    settings,
                )
                chunk_document = read_json(chunk_json)
                if not isinstance(chunk_document, dict):
                    raise ValueError(
                        f"Docling chunk JSON root must be an object chunk={chunk_index}"
                    )
                chunks.append(chunk_document)
                expected_page_counts.append(end_page - start_page)
                LOGGER.info(
                    "Completed Docling PDF chunk chunk=%s pages=%s-%s",
                    chunk_index,
                    start_page + 1,
                    end_page,
                )
        merged = merge_docling_chunks(chunks, expected_page_counts, input_path)
        temp_output_json = temp_dir / "document.json"
        write_json(temp_output_json, merged)
        render_pdf_page_images(input_path, temp_output_json, temp_artifacts)
        replace_artifacts_directory(temp_artifacts, artifacts_dir)
        atomic_write_bytes(output_json, temp_output_json.read_bytes())
        LOGGER.info(
            "Merged Docling PDF chunks chunks=%s pages=%s output=%s",
            len(chunks),
            sum(expected_page_counts),
            output_json,
        )


def convert_with_docling(
    input_path: Path, output_json: Path, artifacts_dir: Path
) -> None:
    """入力文書を Docling JSON と PNG artifacts へ変換する。

    Args:
        input_path: PDF/Word などの入力文書。
        output_json: Docling JSON の保存先。
        artifacts_dir: PNG などの artifact 保存先。

    Returns:
        なし。

    Side Effects:
        PDFは10ページずつ、他形式は1ファイルとしてDocling Serveへ送る。
    """

    settings = require_docling_settings()
    if input_path.suffix.lower() == ".pdf":
        convert_pdf_with_docling_chunks(
            input_path,
            output_json,
            artifacts_dir,
            settings,
        )
        return
    convert_docling_file(input_path, output_json, artifacts_dir, settings)


def extract_docling_zip(zip_path: Path, output_json: Path, artifacts_dir: Path) -> None:
    """Docling zip から JSON と artifacts を展開する。

    Args:
        zip_path: Docling Serve が返した zip。
        output_json: JSON 保存先。
        artifacts_dir: artifact 保存先。

    Returns:
        なし。

    Raises:
        RuntimeError: zip 内に JSON がない場合。

    Side Effects:
        JSONをatomic保存し、artifactsディレクトリ全体を新しい内容へ置換する。
    """

    artifacts_dir.parent.mkdir(parents=True, exist_ok=True)
    temp_artifacts = Path(
        tempfile.mkdtemp(
            prefix=f".{artifacts_dir.name}.", dir=str(artifacts_dir.parent)
        )
    )
    backup_artifacts = artifacts_dir.with_name(
        f".{artifacts_dir.name}.backup-{uuid.uuid4().hex}"
    )
    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            json_members = [
                name
                for name in archive.namelist()
                if PurePosixPath(name).suffix.lower() == ".json"
                and not name.endswith("/")
            ]
            if len(json_members) != 1:
                raise RuntimeError(
                    "Docling zip response must contain exactly one JSON document"
                )
            json_payload = archive.read(json_members[0])
            for member in archive.namelist():
                if member.endswith("/"):
                    continue
                parts = PurePosixPath(member).parts
                if "artifacts" not in parts:
                    continue
                relative_parts = parts[parts.index("artifacts") + 1 :]
                if not relative_parts or ".." in relative_parts:
                    continue
                target = temp_artifacts.joinpath(*relative_parts)
                atomic_write_bytes(target, archive.read(member))
        atomic_write_bytes(output_json, json_payload)
        if artifacts_dir.exists():
            os.replace(artifacts_dir, backup_artifacts)
        os.replace(temp_artifacts, artifacts_dir)
    except Exception:
        if backup_artifacts.exists() and not artifacts_dir.exists():
            os.replace(backup_artifacts, artifacts_dir)
        raise
    finally:
        if temp_artifacts.exists():
            shutil.rmtree(temp_artifacts)
        if backup_artifacts.exists():
            shutil.rmtree(backup_artifacts)


def render_pdf_page_images(
    input_path: Path, output_json: Path, artifacts_dir: Path
) -> None:
    """PDFを1ページずつPNG化し、Docling JSONへ相対URIを設定する。

    Args:
        input_path: 変換元PDF。
        output_json: Docling JSONの保存先。
        artifacts_dir: ページPNGの保存先。

    Returns:
        なし。

    Raises:
        ValueError: Docling JSONのルートまたはpagesが不正な場合。
        RuntimeError: PDFとDocling JSONのページ対応が取れない場合。

    Side Effects:
        ページPNGとDocling JSONをatomic保存する。
    """

    document = read_json(output_json)
    if not isinstance(document, dict):
        raise ValueError("Docling JSON root must be an object")
    pages = document.get("pages")
    if not isinstance(pages, dict):
        raise ValueError("Docling JSON pages must be an object")

    artifacts_dir.mkdir(parents=True, exist_ok=True)
    with pdfium.PdfDocument(input_path) as pdf:
        page_count = len(pdf)
        for page_index in range(page_count):
            page_no = page_index + 1
            page_data = pages.get(str(page_no), pages.get(page_no))
            if not isinstance(page_data, dict):
                raise RuntimeError(f"Docling JSON has no page metadata page={page_no}")

            page = pdf[page_index]
            try:
                bitmap = page.render(scale=PAGE_IMAGE_SCALE)
                try:
                    image = bitmap.to_pil()
                    try:
                        filename = f"page_{page_no:06d}.png"
                        target = artifacts_dir / filename
                        with BytesIO() as buffer:
                            image.save(buffer, format="PNG")
                            atomic_write_bytes(target, buffer.getvalue())
                        page_data["image"] = {
                            "mimetype": "image/png",
                            "dpi": int(72 * PAGE_IMAGE_SCALE),
                            "size": {
                                "width": float(bitmap.width),
                                "height": float(bitmap.height),
                            },
                            "uri": PurePosixPath(
                                artifacts_dir.name, filename
                            ).as_posix(),
                        }
                        LOGGER.debug(
                            "Rendered local PDF page image page=%s path=%s",
                            page_no,
                            target,
                        )
                    finally:
                        image.close()
                finally:
                    bitmap.close()
            finally:
                page.close()

    write_json(output_json, document)
    LOGGER.info("Local PDF page images completed pages=%s", page_count)


def label_of(item: dict[str, Any]) -> str:
    """Docling item の label/type を小文字で返す。

    Args:
        item: Docling item。

    Returns:
        label または type の小文字表現。
    """

    return str(item.get("label") or item.get("type") or "").lower()


def text_of(item: dict[str, Any]) -> str:
    """Docling item から本文文字列を取り出す。

    Args:
        item: Docling item。

    Returns:
        text/orig/content のうち最初に見つかった文字列。
    """

    for key in ("text", "orig", "content"):
        value = item.get(key)
        if isinstance(value, str):
            return value
    return ""


def page_numbers(item: dict[str, Any]) -> list[int]:
    """Docling item の prov からページ番号を抽出する。

    Args:
        item: Docling item。

    Returns:
        昇順のページ番号リスト。
    """

    pages: set[int] = set()
    prov = item.get("prov")
    if isinstance(prov, list):
        for entry in prov:
            if isinstance(entry, dict) and isinstance(entry.get("page_no"), int):
                pages.add(entry["page_no"])
    return sorted(pages)


def coordinate_position(item: dict[str, Any]) -> dict[str, Any] | None:
    """Docling item の先頭ページと bbox を読み順用座標へ変換する。

    Args:
        item: Docling item。

    Returns:
        page、vertical、left、bbox を持つ dict。有効な座標がなければ None。
    """

    prov = item.get("prov")
    if not isinstance(prov, list):
        return None
    positions: list[tuple[int, int, dict[str, Any]]] = []
    for prov_index, entry in enumerate(prov):
        if not isinstance(entry, dict):
            continue
        entry_dict = cast(dict[str, Any], entry)
        page_no = entry_dict.get("page_no")
        if not isinstance(page_no, int):
            continue
        bbox = entry_dict.get("bbox")
        if not isinstance(bbox, dict):
            continue
        bbox_dict = cast(dict[str, Any], bbox)
        values: dict[str, float] = {}
        for name in ("l", "t", "r", "b"):
            value = bbox_dict.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                break
            numeric = float(value)
            if not math.isfinite(numeric):
                break
            values[name] = numeric
        if len(values) != 4:
            continue
        origin = str(bbox_dict.get("coord_origin") or "").upper()
        if origin == "BOTTOMLEFT" or (not origin and values["t"] >= values["b"]):
            vertical = -max(values["t"], values["b"])
        else:
            vertical = min(values["t"], values["b"])
        positions.append(
            (
                page_no,
                prov_index,
                {
                    "page": page_no,
                    "vertical": vertical,
                    "left": min(values["l"], values["r"]),
                    "bbox": {
                        **values,
                        "coord_origin": origin or "INFERRED",
                    },
                },
            )
        )
    if not positions:
        return None
    return min(positions, key=lambda position: (position[0], position[1]))[2]


def reorder_text_collection(data: dict[str, Any], ordered_refs: list[str]) -> bool:
    """texts とそれを参照する JSON pointer を整合性を保って並べ替える。

    Args:
        data: 更新対象の Docling JSON。
        ordered_refs: 並べ替え後の古い text ref 配列。

    Returns:
        並べ替えに成功した場合 True。
    """

    texts = data.get("texts")
    if not isinstance(texts, list):
        return False
    by_ref: dict[str, Any] = {}
    original_refs: list[str] = []
    for index, item in enumerate(texts):
        if not isinstance(item, dict):
            return False
        ref = self_ref(cast(dict[str, Any], item), "texts", index)
        if ref in by_ref:
            return False
        by_ref[ref] = item
        original_refs.append(ref)
    if len(ordered_refs) != len(original_refs) or set(ordered_refs) != set(
        original_refs
    ):
        return False
    rank_by_ref = {ref: index for index, ref in enumerate(ordered_refs)}
    ref_mapping = {
        old_ref: f"#/texts/{new_index}"
        for new_index, old_ref in enumerate(ordered_refs)
    }

    def update_refs(value: Any) -> None:
        if isinstance(value, dict):
            children = value.get("children")
            if isinstance(children, list):
                slots = [
                    index
                    for index, child in enumerate(children)
                    if isinstance(child, dict) and child.get("$ref") in rank_by_ref
                ]
                ordered = sorted(
                    (children[index] for index in slots),
                    key=lambda child: rank_by_ref[child["$ref"]],
                )
                for index, child in zip(slots, ordered, strict=True):
                    children[index] = child
            for key, child in value.items():
                if isinstance(child, str) and child in ref_mapping:
                    value[key] = ref_mapping[child]
                else:
                    update_refs(child)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, str) and child in ref_mapping:
                    value[index] = ref_mapping[child]
                else:
                    update_refs(child)

    update_refs(data)
    data["texts"] = [by_ref[ref] for ref in ordered_refs]
    return True


def replace_text_collection(
    data: dict[str, Any], new_texts: list[Any], ref_mapping: dict[str, str]
) -> None:
    """texts を差し替え、Docling JSON 内の text 参照を張り替える。

    Args:
        data: 更新対象の Docling JSON。
        new_texts: 差し替え後の texts。
        ref_mapping: 差し替え前 ref から差し替え後 ref への対応。

    Returns:
        なし。

    Side Effects:
        texts、self_ref、$ref 参照を更新する。
    """

    def update_refs(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if isinstance(child, str) and child in ref_mapping:
                    value[key] = ref_mapping[child]
                else:
                    update_refs(child)
            children = value.get("children")
            if isinstance(children, list):
                seen_refs: set[str] = set()
                deduped: list[Any] = []
                for child in children:
                    child_ref = child.get("$ref") if isinstance(child, dict) else None
                    if isinstance(child_ref, str):
                        if child_ref in seen_refs:
                            continue
                        seen_refs.add(child_ref)
                    deduped.append(child)
                value["children"] = deduped
        elif isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, str) and child in ref_mapping:
                    value[index] = ref_mapping[child]
                else:
                    update_refs(child)

    data["texts"] = new_texts
    update_refs(data)


def normalize_coordinate_order(
    data: dict[str, Any], patches: list[dict[str, Any]]
) -> None:
    """bbox がある text をページ順・上から下・左から右へ並べる。

    Args:
        data: 更新対象の Docling JSON。
        patches: 座標補正 patch の追加先。

    Returns:
        なし。

    Side Effects:
        texts、self_ref、body/group 参照と patches を更新する。
    """

    texts = data.get("texts")
    if not isinstance(texts, list):
        return
    positioned: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for index, item in enumerate(texts):
        if not isinstance(item, dict):
            continue
        item_dict = cast(dict[str, Any], item)
        position = coordinate_position(item_dict)
        if position is not None:
            positioned.append((index, item_dict, position))
    if len(positioned) < 2:
        return
    sorted_items = sorted(
        positioned,
        key=lambda entry: (
            entry[2]["page"],
            entry[2]["vertical"],
            entry[2]["left"],
            entry[0],
        ),
    )
    original_ref_by_id = {
        id(item): self_ref(cast(dict[str, Any], item), "texts", index)
        for index, item in enumerate(texts)
        if isinstance(item, dict)
    }
    reordered = list(texts)
    for target_index, (_, item, _) in zip(
        (entry[0] for entry in positioned), sorted_items, strict=True
    ):
        reordered[target_index] = item
    before_refs = [
        original_ref_by_id[id(item)] for item in texts if isinstance(item, dict)
    ]
    after_refs = [
        original_ref_by_id[id(item)] for item in reordered if isinstance(item, dict)
    ]
    if after_refs == before_refs or not reorder_text_collection(data, after_refs):
        return
    patch = {
        "op": "reorder_texts",
        "processor": "rule",
        "rule": "bbox_reading_order",
        "rule_version": "1",
        "target": "#/texts",
        "before": before_refs,
        "after": after_refs,
        "reason": "page and bbox order: top-to-bottom, then left-to-right",
        "confidence": 0.9,
    }
    patches.append(patch)
    LOGGER.debug(
        "Applied coordinate normalization rule=%s text_count=%s",
        patch["rule"],
        len(after_refs),
    )


def self_ref(item: dict[str, Any], group: str, index: int) -> str:
    """Docling item の self_ref または推定 JSON pointer を返す。

    Args:
        item: Docling item。
        group: texts/tables/pictures などの top-level group。
        index: group 内 index。

    Returns:
        JSON pointer 形式の参照。
    """

    return str(item.get("self_ref") or f"#/{group}/{index}")


def is_heading(item: dict[str, Any]) -> bool:
    """Docling item が見出し扱いか判定する。

    Args:
        item: Docling item。

    Returns:
        見出しなら True。
    """

    return label_of(item) in HEADING_LABELS


def is_code(item: dict[str, Any]) -> bool:
    """Docling item がコードブロック扱いか判定する。

    Args:
        item: Docling item。

    Returns:
        コードなら True。
    """

    text = text_of(item)
    label = label_of(item)
    return label in CODE_LABELS or bool(
        re.search(
            r"(^|\n)\s*(def |class |import |from |```|Traceback|[A-Za-z_][\w-]*\s*=)",
            text,
        )
    )


def is_page_decoration(item: dict[str, Any]) -> bool:
    """ページヘッダーまたはフッターとして検出された要素か判定する。

    Args:
        item: Docling text item。

    Returns:
        ページ装飾要素ならTrue。
    """

    return label_of(item) in {"page_header", "page_footer"}


def heading_level(item: dict[str, Any]) -> int:
    """Docling item から見出しレベルを推定する。

    Args:
        item: Docling item。

    Returns:
        1 から 6 までの Markdown heading level。
    """

    value = item.get("level") or item.get("heading_level")
    if isinstance(value, int) and value > 0:
        return min(value, 6)
    return 1 if label_of(item) == "title" else 2


def normalize_document(
    data: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Docling JSONのtextsを座標順へ並べ替える。

    Args:
        data: Docling JSON object。

    Returns:
        整形後 JSON と patch 配列。
    """

    result = copy.deepcopy(data)
    patches: list[dict[str, Any]] = []
    normalize_coordinate_order(result, patches)
    return result, patches


def iter_table_cells(
    item: dict[str, Any], ref: str, *, wrap_strings: bool = True
) -> list[tuple[str, dict[str, Any]]]:
    """Docling table item からセル dict を列挙する。

    Args:
        item: Docling table item。
        ref: table item の JSON pointer。
        wrap_strings: grid内の文字列セルをdictへ変換するかどうか。

    Returns:
        セル参照とセル dict のタプル配列。
    """

    data = item.get("data")
    if not isinstance(data, dict):
        return []
    cells = data.get("table_cells") or data.get("cells")
    if isinstance(cells, list):
        result: list[tuple[str, dict[str, Any]]] = []
        for index, cell in enumerate(cells):
            if isinstance(cell, dict):
                result.append(
                    (
                        f"{ref}/data/table_cells/{index}",
                        cast(dict[str, Any], cell),
                    )
                )
        return result
    grid = data.get("grid")
    if not isinstance(grid, list):
        return []
    result: list[tuple[str, dict[str, Any]]] = []
    for row_index, row in enumerate(grid):
        if not isinstance(row, list):
            continue
        row = cast(list[Any], row)
        for col_index, cell in enumerate(row):
            if isinstance(cell, dict):
                result.append(
                    (
                        f"{ref}/data/grid/{row_index}/{col_index}",
                        cast(dict[str, Any], cell),
                    )
                )
            elif isinstance(cell, str) and wrap_strings:
                wrapped = {"text": cell}
                row[col_index] = wrapped
                result.append((f"{ref}/data/grid/{row_index}/{col_index}", wrapped))
    return result


def openai_client(settings: OpenAISettings) -> Any:
    """OpenAI Python client を遅延 import して返す。

    Args:
        settings: OpenAI 互換 API 設定。

    Returns:
        OpenAI client。

    Raises:
        RuntimeError: openai package が import できない場合。
    """

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai package is required for translate-ja-v2") from exc
    return OpenAI(
        base_url=settings.base_url,
        api_key=settings.api_key,
        timeout=settings.timeout_seconds,
        max_retries=0,
    )


def is_retryable_openai_error(exc: Exception) -> bool:
    """OpenAI 互換 API の一時的な失敗かを判定する。

    Args:
        exc: API 呼び出しで発生した例外。

    Returns:
        再試行してよい一時エラーなら True。
    """

    status_code = getattr(exc, "status_code", None)
    if status_code in {408, 409, 429, 500, 502, 503, 504}:
        return True
    exc_name = exc.__class__.__name__
    return exc_name in {"APIConnectionError", "APITimeoutError", "RateLimitError"}


def message_text_chars(value: Any) -> int:
    """Chat messages のテキスト部分だけを数える。

    Args:
        value: Chat messages または content。

    Returns:
        画像 data URL を除いた文字数。
    """

    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(message_text_chars(item) for item in value)
    if isinstance(value, dict):
        if value.get("type") == "image_url":
            return 0
        return sum(message_text_chars(child) for child in value.values())
    return 0


def ensure_openai_context(
    messages: list[dict[str, Any]],
    max_chars: int = OPENAI_CONTEXT_LIMIT_CHARS,
) -> None:
    """OpenAI 互換 request の文字コンテキスト上限を検証する。

    Args:
        messages: Chat messages。
        max_chars: request に含めるテキストの最大文字数。

    Raises:
        ValueError: 文字コンテキストが上限を超える場合。
    """

    size = message_text_chars(messages)
    if size > max_chars:
        raise ValueError(
            f"OpenAI request text context exceeds limit chars={size} limit={max_chars}"
        )


def chat_text(
    client: Any,
    settings: OpenAISettings,
    messages: list[dict[str, Any]],
    *,
    json_response: bool = False,
    max_tokens: int | None = None,
) -> str:
    """Chat Completions を呼び出し、本文文字列を返す。

    Args:
        client: OpenAI client。
        settings: OpenAI 互換 API 設定。
        messages: Chat messages。
        json_response: JSON object 形式をAPIへ要求するかどうか。
        max_tokens: 応答の最大トークン数。None の場合は指定しない。

    Returns:
        応答本文。

    Raises:
        RuntimeError: 応答本文が空の場合。
    """

    ensure_openai_context(messages, settings.context_chars)
    for attempt in range(1, OPENAI_MAX_ATTEMPTS + 1):
        try:
            request = {
                "model": settings.model,
                "messages": messages,
                "temperature": 0.0,
            }
            if max_tokens is not None:
                request["max_tokens"] = max_tokens
            if json_response:
                request["response_format"] = {"type": "json_object"}
            completion = client.chat.completions.create(**request)
            choices = getattr(completion, "choices", None)
            if not choices:
                raise OpenAIEmptyResponseError("OpenAI response has no choices")
            message = getattr(choices[0], "message", None)
            content = getattr(message, "content", None)
            if not content:
                raise OpenAIEmptyResponseError("OpenAI response content is empty")
            return str(content).strip()
        except Exception as exc:
            if attempt >= OPENAI_MAX_ATTEMPTS or not is_retryable_openai_error(exc):
                raise
            delay = min(
                OPENAI_RETRY_MAX_SECONDS,
                OPENAI_RETRY_INITIAL_SECONDS * (2 ** (attempt - 1)),
            )
            LOGGER.warning(
                "Retrying OpenAI request attempt=%s max_attempts=%s delay=%.1f error=%s",
                attempt,
                OPENAI_MAX_ATTEMPTS,
                delay,
                exc,
            )
            time.sleep(delay)
    raise RuntimeError("OpenAI request attempts exhausted")


def parse_json_object(text: str) -> dict[str, Any]:
    """LLM 応答から JSON object を抽出する。

    Args:
        text: LLM 応答文字列。

    Returns:
        抽出した JSON object。

    Raises:
        ValueError: JSON object が見つからない場合。
    """

    stripped = text.strip()
    decoder = json.JSONDecoder()
    for index, char in enumerate(stripped):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError(f"JSON object not found in response: {stripped[:120]}")


def build_structure_messages(
    data: dict[str, Any], artifacts_dir: Path | None = None
) -> list[dict[str, Any]]:
    """VLM/LLM 構造補正用 messages を作る。

    Args:
        data: 正規化済み Docling JSON。
        artifacts_dir: Docling が出力した PNG artifacts のディレクトリ。

    Returns:
        Chat messages。
    """

    units = collect_structure_units(data)
    table_cells = collect_table_cell_structure_units(data)
    all_units = units + table_cells
    page_no = all_units[0]["page"][0] if all_units and all_units[0]["page"] else None
    image_path = page_image_path(data, artifacts_dir, page_no)
    return build_page_structure_messages(page_no, units, image_path, table_cells)


def build_page_structure_messages(
    page_no: int | None,
    units: list[dict[str, Any]],
    image_path: Path | None = None,
    table_cells: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """1ページ分の VLM/LLM 構造補正用 messages を作る。

    Args:
        page_no: 対象ページ番号。
        units: 対象ページの text unit。
        image_path: Docling JSON の URI から解決したページ画像パス。
        table_cells: 対象ページの表セルunit。

    Returns:
        Chat messages。
    """

    request_units = [
        {key: value for key, value in unit.items() if key != "page"} for unit in units
    ]
    request_cells = [
        {key: value for key, value in cell.items() if key != "page"}
        for cell in table_cells or []
    ]
    system = (
        "あなたはDocling JSONのコード構造補正を担当するVLMです。"
        "翻訳、要約、本文の創作は禁止です。"
        "本文と誤認識されたコードはcodeへ変更し、隣接する同一コードブロック"
        "だけを結合してください。表セルはインラインコードのexact spanだけを"
        "特定してください。"
    )
    user = f"""次の1ページ分のDocling要素を読み、ページ画像とbboxを参照してコード構造だけをpatchで返してください。

ページ: {page_no if page_no is not None else "unknown"}
text要素:
{json.dumps(request_units, ensure_ascii=False)}

表セル:
{json.dumps(request_cells, ensure_ascii=False)}

返却JSON:
{{
  "patches": [
    {{"op": "set_label", "ref": "#/texts/0", "label": "code", "reason": "コード構文"}},
    {{"op": "merge_texts", "refs": ["#/texts/0", "#/texts/1"], "reason": "同じコードブロック"}},
    {{"op": "set_table_cell_inline_code", "ref": "#/tables/0/data/grid/0/0", "code_spans": ["api.call()"], "reason": "理由"}}
  ]
}}

`set_label` のlabelは `code` だけ使用できます。`merge_texts` は同一コードブロックとして隣接する要素だけに使い、本文はローカルで改行連結します。`code_spans` は表セル原文に完全一致する文字列だけを返してください。

補正不要なら {{"patches":[]}} を返してください。
"""
    content = build_multimodal_content(user, image_path)
    return [{"role": "system", "content": system}, {"role": "user", "content": content}]


def build_multimodal_content(
    prompt: str, image_path: Path | None
) -> str | list[dict[str, Any]]:
    """VLM へ渡す text とページ画像 content を作る。

    Args:
        prompt: 構造補正プロンプト本文。
        image_path: 添付するページ画像。None なら text のみ返す。

    Returns:
        OpenAI Chat Completions content。画像がなければ文字列、あれば multimodal content 配列。
    """

    if image_path is None:
        return prompt
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    content.append(
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{encoded}"},
        }
    )
    return content


def page_image_path(
    data: dict[str, Any], artifacts_dir: Path | None, page_no: int | None
) -> Path | None:
    """Docling JSON の URI から指定ページの画像パスを解決する。

    Args:
        data: Docling JSON。
        artifacts_dir: Docling PNG artifacts のディレクトリ。
        page_no: Docling の 1-origin page number。None なら画像を解決しない。

    Returns:
        URI が指す既存画像パス。解決できない場合は None。
    """

    if artifacts_dir is None or page_no is None:
        return None
    pages = data.get("pages")
    page = (
        pages.get(str(page_no), pages.get(page_no)) if isinstance(pages, dict) else None
    )
    image = page.get("image") if isinstance(page, dict) else None
    uri = image.get("uri") if isinstance(image, dict) else None
    if not isinstance(uri, str) or not uri.strip():
        LOGGER.warning("Page image URI is missing page=%s", page_no)
        return None

    parsed = urlsplit(uri)
    relative = PurePosixPath(unquote(parsed.path))
    if (
        parsed.scheme
        or parsed.netloc
        or relative.is_absolute()
        or ".." in relative.parts
    ):
        LOGGER.warning(
            "Page image URI is not a safe relative path page=%s uri=%s", page_no, uri
        )
        return None

    export_root = artifacts_dir.parent.resolve()
    path = (export_root / Path(*relative.parts)).resolve()
    if not path.is_relative_to(export_root) or not path.is_file():
        LOGGER.warning("Page image file is missing page=%s uri=%s", page_no, uri)
        return None
    return path


def collect_structure_units(data: dict[str, Any]) -> list[dict[str, Any]]:
    """構造補正用に texts の要約 unit を集める。

    Args:
        data: Docling JSON。

    Returns:
        ref、page、bbox、label、text を持つ unit 配列。
    """

    values = data.get("texts")
    if not isinstance(values, list):
        return []
    units: list[dict[str, Any]] = []
    for index, item in enumerate(values):
        if not isinstance(item, dict):
            continue
        item = cast(dict[str, Any], item)
        text = text_of(item).replace("\n", " ")
        position = coordinate_position(item)
        units.append(
            {
                "ref": self_ref(item, "texts", index),
                "page": page_numbers(item),
                "bbox": position["bbox"] if position else None,
                "label": item.get("label"),
                "text": text[:500],
            }
        )
    return units


def collect_table_cell_structure_units(data: dict[str, Any]) -> list[dict[str, Any]]:
    """構造補正用に表セルの要約unitを集める。

    Args:
        data: Docling JSON。

    Returns:
        ref、page、bbox、textを持つ表セルunit配列。
    """

    tables = data.get("tables")
    if not isinstance(tables, list):
        return []
    units: list[dict[str, Any]] = []
    for index, value in enumerate(tables):
        if not isinstance(value, dict):
            continue
        table = cast(dict[str, Any], value)
        table_ref = self_ref(table, "tables", index)
        position = coordinate_position(table)
        for cell_ref, cell in iter_table_cells(table, table_ref):
            units.append(
                {
                    "ref": cell_ref,
                    "page": page_numbers(cell) or page_numbers(table),
                    "bbox": (coordinate_position(cell) or position or {"bbox": None})[
                        "bbox"
                    ],
                    "text": str(cell.get("text") or cell.get("content") or "")[:500],
                }
            )
    return units


def page_structure_units(
    data: dict[str, Any], page_no: int | None
) -> list[dict[str, Any]]:
    """指定ページの構造補正 unit を集める。

    Args:
        data: Docling JSON。
        page_no: 対象ページ。None の場合はページ不明要素。

    Returns:
        対象ページに属する unit 配列。
    """

    result: list[dict[str, Any]] = []
    for unit in collect_structure_units(data):
        pages = unit.get("page")
        if page_no is None:
            if not pages:
                result.append(unit)
            continue
        if isinstance(pages, list) and page_no in pages:
            result.append(unit)
    return result


def page_table_cell_structure_units(
    data: dict[str, Any], page_no: int | None
) -> list[dict[str, Any]]:
    """指定ページの表セル構造補正unitを集める。

    Args:
        data: Docling JSON。
        page_no: 対象ページ。Noneの場合はページ不明要素。

    Returns:
        対象ページに属する表セルunit配列。
    """

    result: list[dict[str, Any]] = []
    for unit in collect_table_cell_structure_units(data):
        pages = unit.get("page")
        if page_no is None:
            if not pages:
                result.append(unit)
            continue
        if isinstance(pages, list) and page_no in pages:
            result.append(unit)
    return result


def structure_page_numbers(data: dict[str, Any]) -> list[int | None]:
    """texts に含まれるページ番号を文書順に返す。

    Args:
        data: Docling JSON。

    Returns:
        ページ番号配列。ページ番号がない要素があれば None を含む。
    """

    pages: list[int | None] = []
    for unit in collect_structure_units(data) + collect_table_cell_structure_units(
        data
    ):
        unit_pages = unit.get("page")
        page_no = unit_pages[0] if isinstance(unit_pages, list) and unit_pages else None
        if page_no not in pages:
            pages.append(page_no)
    return pages


def build_merge_messages(
    page_no: int | None,
    left: dict[str, Any],
    right: dict[str, Any],
    image_path: Path | None,
) -> list[dict[str, Any]]:
    """隣接 2 要素の merge 判定 messages を作る。

    Args:
        page_no: 対象ページ番号。
        left: 前方要素 unit。
        right: 後方要素 unit。
        image_path: Docling JSON の URI から解決したページ画像パス。

    Returns:
        Chat messages。
    """

    request_units = [
        {key: value for key, value in unit.items() if key != "page"}
        for unit in (left, right)
    ]
    system = (
        "あなたはDocling JSONのコード構造補正を担当するVLMです。"
        "本文と誤認識されたコードをcodeへ変更し、隣接要素が同じコードブロック"
        "ならmergeしてください。意味変更、翻訳、要約は禁止です。"
    )
    user = f"""ページ画像と隣接する2つのDocling text要素を比較し、同じコードブロックとして結合すべきか判定してください。

ページ: {page_no if page_no is not None else "unknown"}
要素:
{json.dumps(request_units, ensure_ascii=False)}

返却JSON:
{{
  "patches": [
    {{"op": "set_label", "ref": "{left["ref"]}", "label": "code", "reason": "コード構文"}},
    {{"op": "merge_texts", "refs": ["{left["ref"]}", "{right["ref"]}"], "reason": "同じコードブロック"}}
  ]
}}

本文labelの要素がコードなら `set_label` だけを返せます。同じコードブロックの前後要素だけ `merge_texts` を返してください。結合後の本文はローカルで原文を改行連結します。

結合不要なら {{"patches":[]}} を返してください。
"""
    content = build_multimodal_content(user, image_path)
    return [{"role": "system", "content": system}, {"role": "user", "content": content}]


def apply_structure_patches(
    data: dict[str, Any], patches: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """構造補正 patch を Docling JSON へ適用する。

    Args:
        data: 正規化済み Docling JSON。
        patches: VLM/LLM が返した patch 配列。

    Returns:
        補正後 JSON と適用結果配列。
    """

    result = copy.deepcopy(data)
    applied: list[dict[str, Any]] = []
    for patch in patches:
        op = patch.get("op")
        if op == "set_label" and patch.get("label") in CODE_LABELS:
            applied.append(apply_field_patch(result, patch))
        elif op == "merge_texts":
            applied.append(apply_merge_texts(result, patch))
        elif op == "set_table_cell_inline_code":
            applied.append(apply_table_cell_inline_code_patch(result, patch))
        else:
            applied.append(
                {"op": op, "status": "skipped", "reason": "unsupported operation"}
            )
    return result, applied


def apply_table_cell_inline_code_patch(
    data: dict[str, Any], patch: dict[str, Any]
) -> dict[str, Any]:
    """表セルへVLMが検出したインラインコードspanを保存する。

    Args:
        data: 更新対象JSON。
        patch: 表セルrefとcode_spansを持つpatch。

    Returns:
        patch適用結果。
    """

    ref = str(patch.get("ref") or "")
    raw_spans = patch.get("code_spans")
    if not ref.startswith("#/tables/") or not isinstance(raw_spans, list):
        return {
            "op": "set_table_cell_inline_code",
            "status": "failed",
            "error": "invalid table cell ref or code_spans",
        }
    try:
        parent, key = pointer_target(data, ref)
        cell = parent[key] if isinstance(parent, list) else parent.get(key)
    except (KeyError, IndexError, TypeError, ValueError):
        return {
            "op": "set_table_cell_inline_code",
            "status": "failed",
            "error": "unknown table cell ref",
        }
    if not isinstance(cell, dict):
        return {
            "op": "set_table_cell_inline_code",
            "status": "failed",
            "error": "table cell is not an object",
        }
    text = str(cell.get("text") or cell.get("content") or "")
    spans = list(
        dict.fromkeys(
            span
            for span in raw_spans
            if isinstance(span, str) and span and span in text
        )
    )
    if not spans:
        return {
            "op": "set_table_cell_inline_code",
            "ref": ref,
            "status": "failed",
            "error": "code_spans do not match cell text",
        }
    metadata = cell.setdefault("structure_ja_v2", {})
    before = metadata.get("inline_code_spans")
    metadata["inline_code_spans"] = spans
    return {
        "op": "set_table_cell_inline_code",
        "ref": ref,
        "status": "success",
        "before": before,
        "after": spans,
        "reason": patch.get("reason"),
    }


def inline_code_spans(cell: dict[str, Any]) -> list[str]:
    """表セルのStructure metadataからインラインコードspanを返す。

    Args:
        cell: Docling table cell。

    Returns:
        重複を除いた非空span配列。
    """

    metadata = cell.get("structure_ja_v2")
    values = metadata.get("inline_code_spans") if isinstance(metadata, dict) else None
    if not isinstance(values, list):
        return []
    return list(
        dict.fromkeys(value for value in values if isinstance(value, str) and value)
    )


def compact_unprotected_punctuation(value: str) -> str:
    """保護対象を含まない文字列の過剰な連続記号を3文字へ縮める。

    Args:
        value: 校正対象文字列。

    Returns:
        3文字以上の連続するピリオドと中黒を3文字へ縮めた文字列。
    """

    return re.sub(r"・{3,}", "・・・", re.sub(r"\.{3,}", "...", value))


def compact_repeated_punctuation(
    value: str, protected_spans: list[str] | None = None
) -> str:
    """コードspanを保持して過剰な連続記号を3文字へ縮める。

    Args:
        value: 校正対象文字列。
        protected_spans: 変更しない完全一致文字列。

    Returns:
        コードspan以外の連続するピリオドと中黒を校正した文字列。
    """

    spans = sorted(set(protected_spans or []), key=len, reverse=True)
    if not spans:
        return compact_unprotected_punctuation(value)
    pattern = re.compile("|".join(re.escape(span) for span in spans if span))
    if not pattern.pattern:
        return compact_unprotected_punctuation(value)
    result: list[str] = []
    previous_end = 0
    for match in pattern.finditer(value):
        result.append(
            compact_unprotected_punctuation(value[previous_end : match.start()])
        )
        result.append(match.group(0))
        previous_end = match.end()
    result.append(compact_unprotected_punctuation(value[previous_end:]))
    return "".join(result)


def replace_primary_text(item: dict[str, Any], value: str) -> None:
    """Docling要素で表示に使われる第1テキストフィールドを置換する。

    Args:
        item: 更新対象のDocling要素。
        value: 置換後文字列。

    Returns:
        なし。

    Side Effects:
        itemのtext、orig、contentのうち最初の文字列フィールドを更新する。
    """

    for key in ("text", "orig", "content"):
        if isinstance(item.get(key), str):
            item[key] = value
            return
    item["text"] = value


def clean_document(
    data: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """本文と表セルの過剰な連続記号を決定論的に校正する。

    Args:
        data: Structure済みDocling JSON。

    Returns:
        校正後JSONと変更patch配列。

    Side Effects:
        なし。入力JSONを複製してから処理する。
    """

    result = copy.deepcopy(data)
    patches: list[dict[str, Any]] = []
    texts = result.get("texts")
    if isinstance(texts, list):
        for index, value in enumerate(texts):
            if not isinstance(value, dict):
                continue
            item = cast(dict[str, Any], value)
            if is_heading(item) or is_code(item):
                continue
            before = text_of(item)
            after = compact_repeated_punctuation(before)
            if after == before:
                continue
            replace_primary_text(item, after)
            patches.append(
                {
                    "rule": "compact_repeated_punctuation",
                    "ref": self_ref(item, "texts", index),
                    "before": before,
                    "after": after,
                }
            )

    tables = result.get("tables")
    if isinstance(tables, list):
        for table_index, value in enumerate(tables):
            if not isinstance(value, dict):
                continue
            table = cast(dict[str, Any], value)
            table_ref = self_ref(table, "tables", table_index)
            for cell_ref, cell in iter_table_cells(
                table, table_ref, wrap_strings=False
            ):
                before = text_of(cell)
                after = compact_repeated_punctuation(before, inline_code_spans(cell))
                if after == before:
                    continue
                replace_primary_text(cell, after)
                patches.append(
                    {
                        "rule": "compact_repeated_punctuation",
                        "ref": cell_ref,
                        "before": before,
                        "after": after,
                    }
                )
            table_data = table.get("data")
            grid = table_data.get("grid") if isinstance(table_data, dict) else None
            if not isinstance(grid, list):
                continue
            for row_index, row in enumerate(grid):
                if not isinstance(row, list):
                    continue
                row = cast(list[Any], row)
                for col_index, cell in enumerate(row):
                    if not isinstance(cell, str):
                        continue
                    after = compact_repeated_punctuation(cell)
                    if after == cell:
                        continue
                    row[col_index] = after
                    patches.append(
                        {
                            "rule": "compact_repeated_punctuation",
                            "ref": f"{table_ref}/data/grid/{row_index}/{col_index}",
                            "before": cell,
                            "after": after,
                        }
                    )
    return result, patches


def pointer_target(data: dict[str, Any], pointer: str) -> tuple[Any, str | int]:
    """JSON pointer の親 container と末尾 key/index を返す。

    Args:
        data: JSON object。
        pointer: #/texts/0/text のような JSON pointer。

    Returns:
        親 container と key/index。

    Raises:
        ValueError: pointer が不正な場合。
    """

    if not pointer.startswith("#/"):
        raise ValueError(f"unsupported pointer: {pointer}")
    current: Any = data
    parts = pointer[2:].split("/")
    for part in parts[:-1]:
        key = part.replace("~1", "/").replace("~0", "~")
        current = current[int(key)] if isinstance(current, list) else current[key]
    tail = parts[-1].replace("~1", "/").replace("~0", "~")
    return current, int(tail) if isinstance(current, list) else tail


def apply_field_patch(data: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """検証済みset_label patchを適用する。

    Args:
        data: 更新対象 JSON。
        patch: field 更新 patch。

    Returns:
        適用結果。
    """

    op = str(patch.get("op"))
    ref = str(patch.get("ref") or "")
    field = "label"
    parent, key = pointer_target(data, f"{ref}/{field}")
    before = parent[key] if isinstance(parent, list) else parent.get(key)
    after = patch.get(field)
    if isinstance(parent, list):
        parent[key] = after
    else:
        parent[key] = after
    return {
        "op": op,
        "ref": ref,
        "status": "success",
        "before": before,
        "after": after,
        "reason": patch.get("reason"),
    }


def apply_merge_texts(data: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """複数 text 要素を先頭 ref の位置へ結合する。

    Args:
        data: 更新対象 JSON。
        patch: 隣接するコード要素のrefsを持つpatch。

    Returns:
        適用結果。
    """

    texts = data.get("texts")
    refs = patch.get("refs")
    if not isinstance(texts, list) or not isinstance(refs, list) or len(refs) < 2:
        return {"op": "merge_texts", "status": "failed", "error": "invalid refs"}
    requested = [str(ref) for ref in refs]
    current_refs = [
        self_ref(cast(dict[str, Any], item), "texts", index)
        for index, item in enumerate(texts)
        if isinstance(item, dict)
    ]
    if len(current_refs) != len(texts) or any(
        ref not in current_refs for ref in requested
    ):
        return {"op": "merge_texts", "status": "failed", "error": "unknown ref"}
    indexes = sorted(current_refs.index(ref) for ref in requested)
    if len(indexes) != len(set(indexes)) or indexes != list(
        range(indexes[0], indexes[-1] + 1)
    ):
        return {
            "op": "merge_texts",
            "status": "failed",
            "error": "refs must be unique and adjacent",
        }
    wanted = [current_refs[index] for index in indexes]

    first_index = indexes[0]
    merged = copy.deepcopy(cast(dict[str, Any], texts[first_index]))
    merged["text"] = "\n".join(
        text_of(cast(dict[str, Any], texts[current_refs.index(ref)])) for ref in wanted
    )
    merged["label"] = "code"
    merged.pop("level", None)
    prov: list[Any] = []
    for ref in wanted:
        item = texts[current_refs.index(ref)]
        if isinstance(item, dict) and isinstance(item.get("prov"), list):
            prov.extend(item["prov"])
    if prov:
        merged["prov"] = prov
    merged.pop("translate_ja_v2", None)

    wanted_set = set(wanted)
    new_texts: list[Any] = []
    ref_mapping: dict[str, str] = {}
    merged_ref = (
        f"#/texts/{sum(ref not in wanted_set for ref in current_refs[:first_index])}"
    )
    for old_ref, item in zip(current_refs, texts, strict=True):
        if old_ref == wanted[0]:
            ref_mapping[old_ref] = merged_ref
            new_texts.append(merged)
            continue
        if old_ref in wanted_set:
            ref_mapping[old_ref] = merged_ref
            continue
        ref_mapping[old_ref] = f"#/texts/{len(new_texts)}"
        new_texts.append(item)
    replace_text_collection(data, new_texts, ref_mapping)
    return {
        "op": "merge_texts",
        "status": "success",
        "refs": wanted,
        "text": merged["text"],
        "reason": patch.get("reason"),
    }


def parse_structure_response(response: str) -> list[dict[str, Any]]:
    """VLM/LLM 応答から structure patches を取り出す。

    Args:
        response: LLM 応答本文。

    Returns:
        patch dict 配列。

    Raises:
        ValueError: patches が配列でない場合。
    """

    payload = parse_json_object(response)
    patches = payload.get("patches")
    if not isinstance(patches, list):
        raise ValueError("structure response must contain patches list")
    return [patch for patch in patches if isinstance(patch, dict)]


def request_structure_patches(
    client: Any,
    settings: OpenAISettings,
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Structure APIを呼び、検証済みpatchを一時的な生成不全時に再試行する。

    Args:
        client: OpenAI client。
        settings: OpenAI settings。
        messages: Structure用Chat messages。

    Returns:
        JSONとしてparseできたStructure patch配列。

    Raises:
        OpenAIEmptyResponseError: 最大試行後も本文が空の場合。
        ValueError: 最大試行後もStructure JSONが不正な場合。

    Side Effects:
        OpenAI互換APIを呼び、一時的な生成不全時に指数backoffで待機する。
    """

    for attempt in range(1, OPENAI_MAX_ATTEMPTS + 1):
        try:
            response = chat_text(
                client,
                settings,
                messages,
                json_response=True,
                max_tokens=OPENAI_MAX_OUTPUT_TOKENS,
            )
            return parse_structure_response(response)
        except (OpenAIEmptyResponseError, ValueError) as exc:
            if attempt >= OPENAI_MAX_ATTEMPTS:
                raise
            delay = min(
                OPENAI_RETRY_MAX_SECONDS,
                OPENAI_RETRY_INITIAL_SECONDS * (2 ** (attempt - 1)),
            )
            LOGGER.warning(
                "Retrying Structure generation attempt=%s max_attempts=%s "
                "delay=%.1f error=%s",
                attempt,
                OPENAI_MAX_ATTEMPTS,
                delay,
                exc,
            )
            time.sleep(delay)
    raise RuntimeError("Structure generation attempts exhausted")


def structure_page_with_vlm(
    data: dict[str, Any],
    page_no: int | None,
    client: Any,
    settings: OpenAISettings,
    artifacts_dir: Path | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """1ページ分を VLM で構造補正する。

    Args:
        data: 更新対象 Docling JSON。
        page_no: 対象ページ番号。
        client: OpenAI client。
        settings: OpenAI settings。
        artifacts_dir: Docling artifacts directory。

    Returns:
        補正後 JSON と適用 patch 配列。
    """

    units = page_structure_units(data, page_no)
    table_cells = page_table_cell_structure_units(data, page_no)
    if not units and not table_cells:
        return data, []
    image_path = page_image_path(data, artifacts_dir, page_no)
    messages = build_page_structure_messages(page_no, units, image_path, table_cells)
    if message_text_chars(messages) <= settings.context_chars:
        patches = request_structure_patches(client, settings, messages)
        return apply_structure_patches(data, patches)
    LOGGER.debug(
        "Falling back to pairwise structure page=%s units=%s", page_no, len(units)
    )
    current, applied = structure_page_pairwise(
        data, page_no, client, settings, image_path
    )
    current, table_applied = structure_table_cells_with_vlm(
        current, page_no, client, settings, image_path
    )
    return current, applied + table_applied


def structure_table_cells_with_vlm(
    data: dict[str, Any],
    page_no: int | None,
    client: Any,
    settings: OpenAISettings,
    image_path: Path | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """表セルをcontext上限内のまとまりでVLM構造補正する。

    Args:
        data: 更新対象Docling JSON。
        page_no: 対象ページ番号。
        client: OpenAI client。
        settings: OpenAI settings。
        image_path: 対象ページ画像。

    Returns:
        補正後JSONと適用patch配列。

    Raises:
        ValueError: 単一表セルでもcontext上限を超える場合。
    """

    remaining = page_table_cell_structure_units(data, page_no)
    current = data
    applied: list[dict[str, Any]] = []
    while remaining:
        chunk: list[dict[str, Any]] = []
        for cell in remaining:
            candidate = chunk + [cell]
            messages = build_page_structure_messages(page_no, [], image_path, candidate)
            if message_text_chars(messages) > settings.context_chars:
                break
            chunk = candidate
        if not chunk:
            raise ValueError(
                "table cell structure request exceeds OpenAI context limit"
            )
        messages = build_page_structure_messages(page_no, [], image_path, chunk)
        patches = request_structure_patches(client, settings, messages)
        current, chunk_applied = apply_structure_patches(current, patches)
        applied.extend(chunk_applied)
        remaining = remaining[len(chunk) :]
    return current, applied


def structure_page_pairwise(
    data: dict[str, Any],
    page_no: int | None,
    client: Any,
    settings: OpenAISettings,
    image_path: Path | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """隣接要素を順番に比較して1ページのコード構造を補正する。

    Args:
        data: 更新対象 Docling JSON。
        page_no: 対象ページ番号。
        client: OpenAI client。
        settings: OpenAI settings。
        image_path: Docling JSON の URI から解決したページ画像パス。

    Returns:
        補正後 JSON と適用 patch 配列。
    """

    current = data
    applied: list[dict[str, Any]] = []

    index = 0
    while index < len(page_structure_units(current, page_no)) - 1:
        units = page_structure_units(current, page_no)
        messages = build_merge_messages(
            page_no, units[index], units[index + 1], image_path
        )
        if message_text_chars(messages) > settings.context_chars:
            raise ValueError("merge comparison exceeds OpenAI context limit")
        patches = request_structure_patches(client, settings, messages)
        current, merge_applied = apply_structure_patches(current, patches)
        successful_merge = any(
            patch.get("op") == "merge_texts" and patch.get("status") == "success"
            for patch in merge_applied
        )
        applied.extend(merge_applied)
        if not successful_merge:
            index += 1

    return current, applied


def structure_document(
    data: dict[str, Any],
    *,
    skip_vlm: bool,
    artifacts_dir: Path | None = None,
    context_chars: int = OPENAI_CONTEXT_LIMIT_CHARS,
    resume_data: dict[str, Any] | None = None,
    completed_ids: set[str] | None = None,
    on_progress: Callable[[dict[str, Any], list[str]], None] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """VLM/LLM で見出し・本文の構造を補正する。

    Args:
        data: 正規化済み Docling JSON。
        skip_vlm: VLM 呼び出しをスキップするかどうか。
        artifacts_dir: Docling PNG artifacts のディレクトリ。
        context_chars: OpenAI request の最大テキスト文字数。
        resume_data: 前回checkpointの部分成果物。
        completed_ids: 処理済みの入力text ref。
        on_progress: ページ完了時に部分成果物と完了refを通知するcallback。

    Returns:
        補正後 JSON と patch 適用結果。
    """

    source_units = collect_structure_units(data) + collect_table_cell_structure_units(
        data
    )
    if skip_vlm:
        result = copy.deepcopy(data)
        if on_progress:
            on_progress(result, [str(unit["ref"]) for unit in source_units])
        return result, []
    settings = require_openai_settings(context_chars)
    client = openai_client(settings)
    result = copy.deepcopy(resume_data if resume_data is not None else data)
    completed = completed_ids if completed_ids is not None else set()
    applied: list[dict[str, Any]] = []
    for page_no in structure_page_numbers(data):
        page_ids = [
            str(unit["ref"])
            for unit in page_structure_units(data, page_no)
            + page_table_cell_structure_units(data, page_no)
        ]
        if page_ids and all(element_id in completed for element_id in page_ids):
            continue
        result, page_applied = structure_page_with_vlm(
            result, page_no, client, settings, artifacts_dir
        )
        applied.extend(page_applied)
        if on_progress:
            on_progress(result, page_ids)
    return result, applied


def glossary_hits(source: str, glossary: list[dict[str, str]]) -> list[dict[str, str]]:
    """原文に含まれる用語集 entry を抽出する。

    Args:
        source: 翻訳対象テキスト。
        glossary: read_glossary_csv が返す用語集。

    Returns:
        原文に english が含まれる entry 配列。
    """

    lowered = source.lower()
    return [
        entry
        for entry in glossary
        if entry.get("english") and entry["english"].lower() in lowered
    ]


def translate_text(
    client: Any,
    settings: OpenAISettings,
    source: str,
    *,
    style: str,
    glossary: list[dict[str, str]] | None = None,
    translation_rules: str = DEFAULT_TRANSLATION_RULES,
) -> str:
    """短いテキストを日本語へ翻訳する。

    Args:
        client: OpenAI client。
        settings: OpenAI 互換 API 設定。
        source: 原文。
        style: heading/table/body の翻訳スタイル。
        glossary: 原文に一致した用語集 entry。
        translation_rules: LLM に渡す翻訳ルール。

    Returns:
        日本語訳。
    """

    if not source.strip():
        return ""
    system = (
        "あなたは専門文書の日英翻訳者です。原文にない説明、要約、事実追加は禁止です。"
    )
    terms = glossary or []
    user = f"""次の{style}を日本語へ翻訳してください。

翻訳ルール:
{translation_rules.strip()}

用語集:
{json.dumps(terms, ensure_ascii=False)}

出力は翻訳文だけにしてください。

原文:
{source}
"""
    return chat_text(
        client,
        settings,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
    )


def pack_translation_blocks(
    blocks: list[list[dict[str, Any]]],
    max_chars: int = TRANSLATION_BATCH_MAX_CHARS,
) -> list[list[dict[str, Any]]]:
    """意味ブロックを原文文字数の上限内で翻訳バッチへ詰める。

    Args:
        blocks: 見出しと本文など、分離を避けたい翻訳対象の配列。
        max_chars: 1バッチに含める原文の最大文字数。

    Returns:
        入力順を保った翻訳バッチ配列。

    Raises:
        ValueError: max_chars が1未満の場合。
    """

    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    pieces: list[list[dict[str, Any]]] = []
    for block in blocks:
        piece: list[dict[str, Any]] = []
        piece_chars = 0
        for item in block:
            item_chars = len(str(item["text"]))
            if piece and piece_chars + item_chars > max_chars:
                pieces.append(piece)
                piece = []
                piece_chars = 0
            piece.append(item)
            piece_chars += item_chars
        if piece:
            pieces.append(piece)

    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for piece in pieces:
        piece_chars = sum(len(str(item["text"])) for item in piece)
        if current and current_chars + piece_chars > max_chars:
            batches.append(current)
            current = []
            current_chars = 0
        current.extend(piece)
        current_chars += piece_chars
    if current:
        batches.append(current)
    return batches


def fit_batches_to_char_limit(
    batches: list[list[dict[str, Any]]],
    max_chars: int,
    measure_chars: Callable[[list[dict[str, Any]]], int],
    limit_name: str,
) -> list[list[dict[str, Any]]]:
    """測定した文字数が指定上限内になるようバッチを分割する。

    Args:
        batches: 意味とbatch_charsを基準に作成済みのバッチ。
        max_chars: 測定値の最大文字数。
        measure_chars: 候補要素から測定文字数を返す関数。
        limit_name: エラーに表示する上限名。

    Returns:
        入力順を保ち、各測定値が上限内となるバッチ配列。

    Raises:
        ValueError: max_charsが1未満、または単一要素でも上限を超える場合。
    """

    if max_chars < 1:
        raise ValueError(f"{limit_name} limit must be positive")
    fitted: list[list[dict[str, Any]]] = []
    for batch in batches:
        start = 0
        while start < len(batch):
            low = 1
            high = len(batch) - start
            best = 0
            while low <= high:
                size = (low + high) // 2
                candidate = batch[start : start + size]
                if measure_chars(candidate) <= max_chars:
                    best = size
                    low = size + 1
                else:
                    high = size - 1
            if best == 0:
                item_id = batch[start].get("id")
                raise ValueError(
                    f"single LLM item exceeds {limit_name} limit id={item_id} "
                    f"limit={max_chars}"
                )
            fitted.append(batch[start : start + best])
            start += best
    return fitted


def fit_batches_to_context(
    batches: list[list[dict[str, Any]]],
    context_chars: int,
    build_messages: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
) -> list[list[dict[str, Any]]]:
    """完成したChat messagesがcontext上限内になるようバッチを分割する。

    Args:
        batches: 意味とbatch_charsを基準に作成済みのバッチ。
        context_chars: messagesの最大テキスト文字数。
        build_messages: 候補要素から実際のmessagesを作る関数。

    Returns:
        入力順を保ち、各messagesが上限内となるバッチ配列。

    Raises:
        ValueError: context_charsが1未満、または単一要素でも上限を超える場合。
    """

    return fit_batches_to_char_limit(
        batches,
        context_chars,
        lambda items: message_text_chars(build_messages(items)),
        "context",
    )


def estimated_translation_response_chars(items: list[dict[str, Any]]) -> int:
    """翻訳バッチの完成応答JSON文字数を原文長から保守的に見積もる。

    Args:
        items: textを持つ翻訳対象。

    Returns:
        原文を仮の訳文とした応答JSONの文字数。
    """

    payload = {
        "translations": [
            {"id": str(index), "translated_text": str(item["text"])}
            for index, item in enumerate(items, start=1)
        ]
    }
    return len(json.dumps(payload, ensure_ascii=False))


def estimated_review_response_chars(items: list[dict[str, Any]]) -> int:
    """Reviewバッチの完成応答JSON文字数を現在の訳文から見積もる。

    Args:
        items: translated_textを持つReview対象。

    Returns:
        現在の訳文を仮のレビュー結果とした応答JSONの文字数。
    """

    payload = {
        "reviews": [
            {"id": str(index), "reviewed_text": str(item["translated_text"])}
            for index, item in enumerate(items, start=1)
        ]
    }
    return len(json.dumps(payload, ensure_ascii=False))


def fit_batches_to_output(
    batches: list[list[dict[str, Any]]],
    estimate_chars: Callable[[list[dict[str, Any]]], int],
) -> list[list[dict[str, Any]]]:
    """項目数と推定応答JSONが安全上限内になるようバッチを分割する。

    Args:
        batches: 入力順を保ったLLMバッチ。
        estimate_chars: 候補バッチの推定応答文字数を返す関数。

    Returns:
        項目数と推定応答が安全上限内となるバッチ配列。
    """

    item_fitted_batches = [
        batch[start : start + OPENAI_BATCH_MAX_ITEMS]
        for batch in batches
        for start in range(0, len(batch), OPENAI_BATCH_MAX_ITEMS)
    ]
    return fit_batches_to_char_limit(
        item_fitted_batches,
        OPENAI_SAFE_OUTPUT_CHARS,
        estimate_chars,
        "output",
    )


def build_translation_messages(
    items: list[dict[str, Any]], translation_rules: str
) -> list[dict[str, Any]]:
    """重複文脈と用語集を集約した翻訳用messagesを作る。

    Args:
        items: id、text、style、context、glossaryを持つ翻訳対象。
        translation_rules: LLMへ渡す翻訳ルール。

    Returns:
        OpenAI Chat Completionsへ渡すmessages。
    """

    contexts: dict[str, str] = {}
    context_ids: dict[str, str] = {}
    glossary_by_json: dict[str, dict[str, str]] = {}
    request_items: list[dict[str, Any]] = []
    for local_id, item in enumerate(items, start=1):
        request_item: dict[str, Any] = {
            "id": str(local_id),
            "style": str(item["style"]),
            "text": str(item["text"]),
        }
        context = str(item.get("context") or "")
        if context:
            context_id = context_ids.get(context)
            if context_id is None:
                context_id = f"c{len(contexts) + 1}"
                context_ids[context] = context_id
                contexts[context_id] = context
            request_item["context_id"] = context_id
        spans = item.get("inline_code_spans")
        if isinstance(spans, list) and spans:
            request_item["inline_code_spans"] = spans
        for term in item.get("glossary") or []:
            if isinstance(term, dict):
                key = json.dumps(term, ensure_ascii=False, sort_keys=True)
                glossary_by_json.setdefault(key, cast(dict[str, str], term))
        request_items.append(request_item)

    system = (
        "あなたは専門文書の日英翻訳者です。原文にない説明、要約、事実追加は禁止です。"
        "入力IDを変更せずJSONだけを返してください。inline_code_spansは変更しません。"
    )
    user = f"""次の要素を日本語へ翻訳してください。

翻訳ルール:
{translation_rules.strip()}

共有文脈JSON:
{json.dumps(contexts, ensure_ascii=False)}

共有用語集JSON:
{json.dumps(list(glossary_by_json.values()), ensure_ascii=False)}

返却JSON:
{{"translations":[{{"id":"入力と同じID","translated_text":"日本語訳"}}]}}

context_idは共有文脈の参照です。用語集はenglishが原文に一致する場合だけ適用してください。IDの追加、削除、変更、重複は禁止です。

入力JSON:
{json.dumps(request_items, ensure_ascii=False)}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def normalize_batch_response_id(value: Any) -> str | None:
    """LLMが返す文字列または整数のバッチIDを文字列へ正規化する。

    Args:
        value: JSON応答内のID値。

    Returns:
        検証可能な文字列ID。不正な型ならNone。
    """

    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def translate_batch(
    client: Any,
    settings: OpenAISettings,
    items: list[dict[str, Any]],
    *,
    translation_rules: str = DEFAULT_TRANSLATION_RULES,
    _invalid_attempt: int = 1,
) -> dict[str, str]:
    """複数の原文をID付きJSONで一括翻訳する。

    Args:
        client: OpenAI client。
        settings: OpenAI 互換 API 設定。
        items: id、text、style、context、glossary を持つ翻訳対象。
        translation_rules: LLM に渡す翻訳ルール。
        _invalid_attempt: 単一要素の生成不全に対する現在の試行番号。

    Returns:
        入力IDから日本語訳への辞書。

    Raises:
        ValueError: 応答IDが入力と一致しない場合、または訳文が不正な場合。

    Side Effects:
        OpenAI 互換 API を1回呼び出す。
    """

    if not items:
        return {}
    original_ids = [str(item["id"]) for item in items]
    if len(original_ids) != len(set(original_ids)):
        raise ValueError("translation batch contains duplicate ids")
    expected_ids = [str(index) for index in range(1, len(items) + 1)]

    def split_batch(error: Exception) -> dict[str, str]:
        """不正なバッチを二分し、単一要素なら上限付きで再試行する。

        Args:
            error: バッチを採用できない理由。

        Returns:
            入力IDから再翻訳結果への辞書。

        Raises:
            Exception: 単一要素で最大試行回数に達した場合の元の例外。
        """

        if len(items) == 1:
            if _invalid_attempt >= OPENAI_MAX_ATTEMPTS:
                raise error
            delay = min(
                OPENAI_RETRY_MAX_SECONDS,
                OPENAI_RETRY_INITIAL_SECONDS * (2 ** (_invalid_attempt - 1)),
            )
            LOGGER.warning(
                "Retrying invalid translation generation id=%s attempt=%s "
                "max_attempts=%s delay=%.1f error=%s",
                items[0]["id"],
                _invalid_attempt,
                OPENAI_MAX_ATTEMPTS,
                delay,
                error,
            )
            time.sleep(delay)
            return translate_batch(
                client,
                settings,
                items,
                translation_rules=translation_rules,
                _invalid_attempt=_invalid_attempt + 1,
            )
        middle = len(items) // 2
        LOGGER.warning(
            "Splitting invalid translation batch items=%s error=%s", len(items), error
        )
        return {
            **translate_batch(
                client,
                settings,
                items[:middle],
                translation_rules=translation_rules,
            ),
            **translate_batch(
                client,
                settings,
                items[middle:],
                translation_rules=translation_rules,
            ),
        }

    messages = build_translation_messages(items, translation_rules)
    try:
        response = chat_text(
            client,
            settings,
            messages,
            json_response=True,
            max_tokens=OPENAI_BATCH_MAX_OUTPUT_TOKENS,
        )
    except OpenAIEmptyResponseError as exc:
        return split_batch(exc)
    try:
        try:
            payload = json.loads(response)
        except json.JSONDecodeError:
            payload = parse_json_object(response)
    except ValueError as exc:
        return split_batch(exc)
    translations = payload if isinstance(payload, list) else payload.get("translations")
    if isinstance(payload, dict) and {"id", "translated_text"} <= payload.keys():
        translations = [payload]
    if not isinstance(translations, list):
        return split_batch(
            ValueError("translation response must contain translations list")
        )
    result: dict[str, str] = {}
    for entry in translations:
        if not isinstance(entry, dict):
            return split_batch(ValueError("translation entry must be an object"))
        item_id = normalize_batch_response_id(entry.get("id"))
        translated_text = entry.get("translated_text")
        if (
            item_id is None
            or not isinstance(translated_text, str)
            or not translated_text.strip()
        ):
            return split_batch(
                ValueError("translation entry must contain string id and text")
            )
        if item_id in result:
            return split_batch(
                ValueError(f"translation response contains duplicate id: {item_id}")
            )
        result[item_id] = translated_text
    if set(result) != set(expected_ids):
        missing = sorted(set(expected_ids) - set(result))
        unknown = sorted(set(result) - set(expected_ids))
        return split_batch(
            ValueError(
                "translation response ids do not match "
                f"missing={missing} unknown={unknown}"
            )
        )
    translated_by_original_id = {
        original_id: result[str(index)]
        for index, original_id in enumerate(original_ids, start=1)
    }
    LOGGER.debug(
        "Translated batch items=%s source_chars=%s",
        len(items),
        sum(len(str(item["text"])) for item in items),
    )
    return translated_by_original_id


def apply_text_translation(
    item: dict[str, Any], translated: str, terms: list[dict[str, str]]
) -> None:
    """text item に検証済みの訳文を反映する。

    Args:
        item: 更新対象の Docling text item。
        translated: 対応する日本語訳。
        terms: 原文に一致した用語集 entry。

    Returns:
        なし。

    Side Effects:
        item の translate_ja_v2 フィールドを更新する。
    """

    text = text_of(item).strip()
    meta = item.setdefault("translate_ja_v2", {})
    if is_heading(item):
        meta.update(
            {
                "kind": "heading",
                "text_en": text,
                "text_ja": translated,
                "render_text": f"{text} / {translated}",
                "translated": True,
                "glossary_terms": [term["english"] for term in terms],
            }
        )
        return
    meta.update(
        {
            "kind": "body",
            "text_en": text,
            "text_ja": translated,
            "render_text": translated,
            "translated": True,
            "glossary_terms": [term["english"] for term in terms],
        }
    )


def translate_text_items(
    values: list[Any],
    client: Any,
    settings: OpenAISettings,
    glossary: list[dict[str, str]],
    translation_rules: str,
    batch_chars: int = TRANSLATION_BATCH_MAX_CHARS,
    completed_ids: set[str] | None = None,
    on_progress: Callable[[list[str]], None] | None = None,
) -> None:
    """見出し階層と後続本文を意味ブロック化して一括翻訳する。

    Args:
        values: Docling texts 配列。
        client: OpenAI client。
        settings: OpenAI 互換 API 設定。
        glossary: CSV から読み込んだ用語集。
        translation_rules: LLM に渡す翻訳ルール。
        batch_chars: 翻訳バッチの最大原文文字数。
        completed_ids: checkpointで完了済みのtext ref。
        on_progress: 要素完了時にref配列を通知するcallback。

    Returns:
        なし。

    Side Effects:
        text item の翻訳 metadata を更新し、OpenAI 互換 API を呼び出す。
    """

    blocks: list[list[dict[str, Any]]] = []
    block: list[dict[str, Any]] = []
    block_root_level: int | None = None
    heading_stack: list[tuple[int, str]] = []
    immediate_completed: list[str] = []
    completed = completed_ids if completed_ids is not None else set()
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            continue
        item = cast(dict[str, Any], value)
        ref = self_ref(item, "texts", index)
        text = text_of(item).strip()
        if not text:
            if ref not in completed:
                immediate_completed.append(ref)
            continue
        if is_page_decoration(item):
            if ref not in completed:
                item.setdefault("translate_ja_v2", {}).update(
                    {"kind": "decoration", "render_text": text, "translated": False}
                )
                immediate_completed.append(ref)
            continue
        if is_code(item):
            if ref not in completed:
                item.setdefault("translate_ja_v2", {}).update(
                    {"kind": "code", "render_text": text, "translated": False}
                )
                immediate_completed.append(ref)
            continue
        if is_heading(item):
            level = heading_level(item)
            if block and (block_root_level is None or level <= block_root_level):
                blocks.append(block)
                block = []
                block_root_level = None
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, text))
            if ref not in completed and block_root_level is None:
                block_root_level = level
        if ref in completed:
            continue
        section_context = " > ".join(title for _level, title in heading_stack)
        terms = glossary_hits(text, glossary)
        block.append(
            {
                "id": ref,
                "text": text,
                "style": "見出し" if is_heading(item) else "本文",
                "context": section_context,
                "glossary": terms,
                "item": item,
                "terms": terms,
            }
        )
    if block:
        blocks.append(block)
    if immediate_completed and on_progress:
        on_progress(immediate_completed)

    output_fitted_batches = fit_batches_to_output(
        pack_translation_blocks(blocks, max_chars=batch_chars),
        estimated_translation_response_chars,
    )
    batches = fit_batches_to_context(
        output_fitted_batches,
        settings.context_chars,
        partial(build_translation_messages, translation_rules=translation_rules),
    )
    for batch in batches:
        translations = translate_batch(
            client,
            settings,
            batch,
            translation_rules=translation_rules,
        )
        for target in batch:
            apply_text_translation(
                cast(dict[str, Any], target["item"]),
                translations[str(target["id"])],
                cast(list[dict[str, str]], target["terms"]),
            )
        if on_progress:
            on_progress([str(target["id"]) for target in batch])


def translate_document(
    data: dict[str, Any],
    glossary: list[dict[str, str]] | None = None,
    translation_rules: str = DEFAULT_TRANSLATION_RULES,
    context_chars: int = OPENAI_CONTEXT_LIMIT_CHARS,
    batch_chars: int = TRANSLATION_BATCH_MAX_CHARS,
    resume_data: dict[str, Any] | None = None,
    completed_ids: set[str] | None = None,
    on_progress: Callable[[dict[str, Any], list[str]], None] | None = None,
) -> dict[str, Any]:
    """Docling JSON の各要素へ日本語翻訳フィールドを追加する。

    Args:
        data: 構造補正済み Docling JSON。
        glossary: CSV から読み込んだ用語集。
        translation_rules: 翻訳ルール本文。
        context_chars: OpenAI request の最大テキスト文字数。
        batch_chars: 翻訳バッチの最大原文文字数。
        resume_data: 前回checkpointの部分成果物。
        completed_ids: checkpointで完了済みの要素ID。
        on_progress: 要素完了時に部分成果物とID配列を通知するcallback。

    Returns:
        翻訳フィールドを追加した JSON。
    """

    result = copy.deepcopy(resume_data if resume_data is not None else data)
    settings = require_openai_settings(context_chars)
    client = openai_client(settings)
    glossary_entries = glossary or []
    completed = completed_ids if completed_ids is not None else set()

    def notify(element_ids: list[str]) -> None:
        """完了IDを蓄積し、現在の部分成果物を通知する。

        Args:
            element_ids: 新たに完了した要素ID。

        Returns:
            なし。
        """

        completed.update(element_ids)
        if on_progress:
            on_progress(result, element_ids)

    texts = result.get("texts")
    if isinstance(texts, list):
        translate_text_items(
            texts,
            client,
            settings,
            glossary_entries,
            translation_rules,
            batch_chars,
            completed,
            notify,
        )
    tables = result.get("tables")
    if isinstance(tables, list):
        for index, value in enumerate(tables):
            if not isinstance(value, dict):
                continue
            item = cast(dict[str, Any], value)
            translate_table_item(
                item,
                client,
                settings,
                self_ref(item, "tables", index),
                glossary_entries,
                translation_rules,
                batch_chars,
                completed,
                notify,
            )
    return result


def translation_element_ids(data: dict[str, Any]) -> set[str]:
    """Translate工程で状態管理する全要素IDを返す。

    Args:
        data: 構造補正済みDocling JSON。

    Returns:
        text、表タイトル、表セルのID集合。
    """

    element_ids: set[str] = set()
    texts = data.get("texts")
    if isinstance(texts, list):
        for index, item in enumerate(texts):
            if isinstance(item, dict):
                element_ids.add(self_ref(cast(dict[str, Any], item), "texts", index))
    tables = data.get("tables")
    if isinstance(tables, list):
        for index, item in enumerate(tables):
            if not isinstance(item, dict):
                continue
            table = cast(dict[str, Any], item)
            ref = self_ref(table, "tables", index)
            if str(table.get("caption") or table.get("title") or "").strip():
                element_ids.add(f"{ref}/caption")
            element_ids.update(
                cell_ref for cell_ref, _cell in iter_table_cells(table, ref)
            )
    return element_ids


def translate_text_item(
    item: dict[str, Any],
    client: Any,
    settings: OpenAISettings,
    glossary: list[dict[str, str]] | None = None,
    translation_rules: str = DEFAULT_TRANSLATION_RULES,
) -> None:
    """Docling text item に翻訳フィールドを追加する。

    Args:
        item: Docling text item。
        client: OpenAI client。
        settings: OpenAI 互換 API 設定。
        glossary: CSV から読み込んだ用語集。
        translation_rules: 翻訳ルール本文。

    Returns:
        なし。

    Side Effects:
        item の translate_ja_v2 フィールドを更新する。
    """

    text = text_of(item).strip()
    meta = item.setdefault("translate_ja_v2", {})
    if not text:
        return
    if is_page_decoration(item):
        meta.update({"kind": "decoration", "render_text": text, "translated": False})
        return
    if is_code(item):
        meta.update({"kind": "code", "render_text": text, "translated": False})
        return
    terms = glossary_hits(text, glossary or [])
    if is_heading(item):
        ja = translate_text(
            client,
            settings,
            text,
            style="見出し",
            glossary=terms,
            translation_rules=translation_rules,
        )
        meta.update(
            {
                "kind": "heading",
                "text_en": text,
                "text_ja": ja,
                "render_text": f"{text} / {ja}",
                "translated": True,
                "glossary_terms": [term["english"] for term in terms],
            }
        )
        return
    ja = translate_text(
        client,
        settings,
        text,
        style="本文",
        glossary=terms,
        translation_rules=translation_rules,
    )
    meta.update(
        {
            "kind": "body",
            "text_en": text,
            "text_ja": ja,
            "render_text": ja,
            "translated": True,
            "glossary_terms": [term["english"] for term in terms],
        }
    )


def translate_table_item(
    item: dict[str, Any],
    client: Any,
    settings: OpenAISettings,
    ref: str,
    glossary: list[dict[str, str]] | None = None,
    translation_rules: str = DEFAULT_TRANSLATION_RULES,
    batch_chars: int = TRANSLATION_BATCH_MAX_CHARS,
    completed_ids: set[str] | None = None,
    on_progress: Callable[[list[str]], None] | None = None,
) -> None:
    """Docling table item のタイトルとセルへ翻訳フィールドを追加する。

    Args:
        item: Docling table item。
        client: OpenAI client。
        settings: OpenAI 互換 API 設定。
        ref: table item の JSON pointer。
        glossary: CSV から読み込んだ用語集。
        translation_rules: 翻訳ルール本文。
        batch_chars: 翻訳バッチの最大原文文字数。
        completed_ids: checkpointで完了済みの表要素ID。
        on_progress: 要素完了時にID配列を通知するcallback。

    Returns:
        なし。

    Side Effects:
        item と cell の translate_ja_v2 フィールドを更新する。
    """

    targets: list[dict[str, Any]] = []
    immediate_completed: list[str] = []
    completed = completed_ids if completed_ids is not None else set()
    caption = str(item.get("caption") or item.get("title") or "").strip()
    caption_ref = f"{ref}/caption"
    if caption and caption_ref not in completed:
        terms = glossary_hits(caption, glossary or [])
        targets.append(
            {
                "id": caption_ref,
                "text": caption,
                "style": "表タイトル",
                "context": caption,
                "glossary": terms,
                "kind": "caption",
                "item": item,
                "terms": terms,
            }
        )
    for cell_ref, cell in iter_table_cells(item, ref):
        if cell_ref in completed:
            continue
        source = str(cell.get("text") or cell.get("content") or "").strip()
        cell_meta = cell.setdefault("translate_ja_v2", {})
        if not source:
            immediate_completed.append(cell_ref)
            continue
        code_spans = inline_code_spans(cell)
        if source in code_spans:
            cell_meta.update(
                {
                    "kind": "code",
                    "text_en": source,
                    "render_text": source,
                    "translated": False,
                }
            )
            immediate_completed.append(cell_ref)
            continue
        if looks_protected(source):
            cell_meta.update(
                {"text_en": source, "render_text": source, "translated": False}
            )
            immediate_completed.append(cell_ref)
            continue
        terms = glossary_hits(source, glossary or [])
        targets.append(
            {
                "id": cell_ref,
                "text": source,
                "style": "表セル",
                "context": caption,
                "glossary": terms,
                "kind": "cell",
                "item": cell,
                "terms": terms,
                "inline_code_spans": code_spans,
            }
        )

    if immediate_completed and on_progress:
        on_progress(immediate_completed)
    output_fitted_batches = fit_batches_to_output(
        pack_translation_blocks([targets], max_chars=batch_chars),
        estimated_translation_response_chars,
    )
    batches = fit_batches_to_context(
        output_fitted_batches,
        settings.context_chars,
        partial(build_translation_messages, translation_rules=translation_rules),
    )
    for batch in batches:
        translations = translate_batch(
            client,
            settings,
            batch,
            translation_rules=translation_rules,
        )
        for target in batch:
            translated = translations[str(target["id"])]
            target_item = cast(dict[str, Any], target["item"])
            terms = cast(list[dict[str, str]], target["terms"])
            if target["kind"] == "caption":
                target_item.setdefault("translate_ja_v2", {}).update(
                    {
                        "caption_en": caption,
                        "caption_ja": translated,
                        "caption_render": f"{caption} / {translated}",
                        "glossary_terms": [term["english"] for term in terms],
                    }
                )
                continue
            source = str(target["text"])
            target_item.setdefault("translate_ja_v2", {}).update(
                {
                    "text_en": source,
                    "text_ja": translated,
                    "render_text": translated,
                    "translated": True,
                    "glossary_terms": [term["english"] for term in terms],
                }
            )
        if on_progress:
            on_progress([str(target["id"]) for target in batch])


def review_document(
    data: dict[str, Any],
    translation_rules: str = DEFAULT_TRANSLATION_RULES,
    context_chars: int = OPENAI_CONTEXT_LIMIT_CHARS,
    batch_chars: int = TRANSLATION_BATCH_MAX_CHARS,
    resume_data: dict[str, Any] | None = None,
    completed_ids: set[str] | None = None,
    on_progress: Callable[[dict[str, Any], list[str]], None] | None = None,
) -> tuple[dict[str, Any], int]:
    """翻訳済み metadata を近接要素と照合して校正する。

    Args:
        data: 翻訳 metadata 付き Docling JSON。
        translation_rules: LLM に渡す翻訳ルール。
        context_chars: OpenAI request の最大テキスト文字数。
        batch_chars: 1バッチに含める原文と訳文の最大文字数。
        resume_data: 前回checkpointの部分成果物。
        completed_ids: checkpointで完了済みのレビュー対象ID。
        on_progress: 要素完了時に部分成果物とID配列を通知するcallback。

    Returns:
        レビュー済み JSON と変更件数。
    """

    result = copy.deepcopy(resume_data if resume_data is not None else data)
    targets = collect_review_targets(result)
    if not targets:
        return result, 0
    settings = require_openai_settings(context_chars)
    client = openai_client(settings)
    completed = completed_ids if completed_ids is not None else set()
    changes = 0
    add_review_neighbors(targets)
    pending_targets = [
        target for target in targets if str(target["id"]) not in completed
    ]
    output_fitted_batches = fit_batches_to_output(
        pack_translation_blocks([pending_targets], max_chars=batch_chars),
        estimated_review_response_chars,
    )
    batches = fit_batches_to_context(
        output_fitted_batches,
        settings.context_chars,
        partial(build_review_messages, translation_rules=translation_rules),
    )
    max_workers = min(REVIEW_MAX_WORKERS, len(batches))
    if max_workers == 0:
        return result, 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                review_batch,
                client,
                settings,
                batch,
                translation_rules=translation_rules,
            ): batch
            for batch in batches
        }
        for future in as_completed(futures):
            batch = futures[future]
            reviewed = future.result()
            changes += apply_review_results(batch, reviewed)
            completed_ids_in_batch = [str(target["id"]) for target in batch]
            completed.update(completed_ids_in_batch)
            if on_progress:
                on_progress(result, completed_ids_in_batch)
    return result, changes


def collect_review_targets(data: dict[str, Any]) -> list[dict[str, Any]]:
    """レビュー対象の翻訳済み text、表タイトル、表セルを文書順に集める。

    Args:
        data: 翻訳 metadata 付き Docling JSON。

    Returns:
        review_batch に渡す内部 target 配列。
    """

    targets: list[dict[str, Any]] = []
    texts = data.get("texts")
    if isinstance(texts, list):
        for index, value in enumerate(texts):
            if not isinstance(value, dict):
                continue
            item = cast(dict[str, Any], value)
            meta = item.get("translate_ja_v2")
            if isinstance(meta, dict) and meta.get("translated") is True:
                add_review_target(
                    targets,
                    {
                        "id": self_ref(item, "texts", index),
                        "kind": str(meta.get("kind") or "text"),
                        "source_text": str(meta.get("text_en") or text_of(item)),
                        "translated_text": str(meta.get("text_ja") or ""),
                        "meta": meta,
                        "text_field": "text_ja",
                        "render_field": "render_text",
                    },
                )

    tables = data.get("tables")
    if isinstance(tables, list):
        for index, value in enumerate(tables):
            if not isinstance(value, dict):
                continue
            item = cast(dict[str, Any], value)
            ref = self_ref(item, "tables", index)
            caption = str(item.get("caption") or item.get("title") or "").strip()
            meta = item.get("translate_ja_v2")
            if isinstance(meta, dict) and isinstance(meta.get("caption_ja"), str):
                add_review_target(
                    targets,
                    {
                        "id": f"{ref}/caption",
                        "kind": "caption",
                        "source_text": str(meta.get("caption_en") or caption),
                        "translated_text": str(meta.get("caption_ja") or ""),
                        "meta": meta,
                        "text_field": "caption_ja",
                        "render_field": "caption_render",
                    },
                )
            for cell_ref, cell in iter_table_cells(item, ref):
                cell_meta = cell.get("translate_ja_v2")
                if isinstance(cell_meta, dict) and cell_meta.get("translated") is True:
                    add_review_target(
                        targets,
                        {
                            "id": cell_ref,
                            "kind": "cell",
                            "source_text": str(
                                cell_meta.get("text_en")
                                or cell.get("text")
                                or cell.get("content")
                                or ""
                            ),
                            "translated_text": str(cell_meta.get("text_ja") or ""),
                            "inline_code_spans": inline_code_spans(cell),
                            "meta": cell_meta,
                            "text_field": "text_ja",
                            "render_field": "render_text",
                        },
                    )
    return targets


def add_review_target(targets: list[dict[str, Any]], target: dict[str, Any]) -> None:
    """空訳を除外し、batch size 計算用 text を足して target を追加する。"""

    if not str(target.get("source_text") or "").strip():
        return
    if not str(target.get("translated_text") or "").strip():
        return
    target["text"] = f"{target['source_text']}\n{target['translated_text']}"
    targets.append(target)


def add_review_neighbors(targets: list[dict[str, Any]]) -> None:
    """誤コピーのローカル検証用に前後の原文と訳文を添える。

    Args:
        targets: 文書順に並んだレビュー対象。

    Returns:
        なし。

    Side Effects:
        各対象へ前後要素の原文と訳文を追加する。APIには送信しない。
    """

    for index, target in enumerate(targets):
        target["previous_source_text"] = (
            str(targets[index - 1]["source_text"]) if index > 0 else ""
        )
        target["previous_text_ja"] = (
            str(targets[index - 1]["translated_text"]) if index > 0 else ""
        )
        target["next_source_text"] = (
            str(targets[index + 1]["source_text"]) if index + 1 < len(targets) else ""
        )
        target["next_text_ja"] = (
            str(targets[index + 1]["translated_text"])
            if index + 1 < len(targets)
            else ""
        )


def review_rejection_reason(item: dict[str, Any], reviewed_text: str) -> str | None:
    """隣接要素の誤コピーや異常な長文化を検出する。

    Args:
        item: 原文、原訳、前後要素を持つレビュー対象。
        reviewed_text: APIが返したレビュー後の訳文。

    Returns:
        採用できない理由。採用可能な場合はNone。
    """

    current = str(item["translated_text"]).strip()
    reviewed = reviewed_text.strip()
    introduced_meta_context = any(
        marker in reviewed and marker not in current
        for marker in REVIEW_META_CONTEXT_MARKERS
    )
    if introduced_meta_context and any(
        marker in reviewed for marker in REVIEW_META_FAILURE_MARKERS
    ):
        return "review response is a meta-level request for missing input"
    if JAPANESE_TEXT_RE.search(current) and not JAPANESE_TEXT_RE.search(reviewed):
        return "review response removes all Japanese text"
    if len(reviewed) > max(200, len(current) * 1.5):
        return "review response is disproportionately longer than current translation"
    if len(current) >= 100 and len(reviewed) < len(current) * 0.6:
        return "review response is disproportionately shorter than current translation"
    source = str(item["source_text"]).strip()
    for position in ("previous", "next"):
        neighbor_source = str(item.get(f"{position}_source_text") or "").strip()
        neighbor_translation = str(item.get(f"{position}_text_ja") or "").strip()
        if not neighbor_source or not neighbor_translation or neighbor_source == source:
            continue
        neighbor_similarity = SequenceMatcher(
            None, reviewed, neighbor_translation, autojunk=False
        ).ratio()
        current_similarity = SequenceMatcher(
            None, reviewed, current, autojunk=False
        ).ratio()
        if neighbor_similarity >= 0.95 and current_similarity < 0.8:
            return f"review response matches {position} element"
    return None


def build_review_messages(
    items: list[dict[str, Any]], translation_rules: str
) -> list[dict[str, Any]]:
    """重複する前後文脈を含まないReview用messagesを作る。

    Args:
        items: 原文と現在訳を持つReview対象。
        translation_rules: LLMへ渡す翻訳ルール。

    Returns:
        OpenAI Chat Completionsへ渡すmessages。
    """

    request_items: list[dict[str, Any]] = []
    for local_id, item in enumerate(items, start=1):
        request_item: dict[str, Any] = {
            "id": str(local_id),
            "source_text": str(item["source_text"]),
            "translated_text": str(item["translated_text"]),
        }
        spans = item.get("inline_code_spans")
        if isinstance(spans, list) and spans:
            request_item["inline_code_spans"] = spans
        request_items.append(request_item)

    system = (
        "あなたは専門文書の日英翻訳レビュー担当者です。"
        "原文にない説明、要約、事実追加は禁止です。"
        "入力IDを変更せずJSONだけを返してください。"
    )
    user = f"""翻訳済み要素をレビューし、必要な場合だけ日本語訳を修正してください。

レビュー観点:
- 原文の意味、数量、否定、固有名詞が保たれているか。
- バッチ内で同じ概念・英語表現の日本語表記が揺れていないか。
- inline_code_spansが変更されていないか。

翻訳ルール:
{translation_rules.strip()}

返却JSON:
{{"reviews":[{{"id":"入力と同じID","reviewed_text":"レビュー後の日本語訳"}}]}}

入力順を文書順として参照してください。IDの追加、削除、変更、重複は禁止です。修正不要ならtranslated_textをそのまま返してください。

入力JSON:
{json.dumps(request_items, ensure_ascii=False)}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def review_batch(
    client: Any,
    settings: OpenAISettings,
    items: list[dict[str, Any]],
    *,
    translation_rules: str = DEFAULT_TRANSLATION_RULES,
) -> dict[str, str]:
    """翻訳済み要素をID付きJSON応答で一括レビューする。

    Args:
        client: OpenAI client。
        settings: OpenAI 互換 API 設定。
        items: review target 配列。
        translation_rules: LLM に渡す翻訳ルール。

    Returns:
        入力IDからレビュー後訳文への辞書。

    Raises:
        ValueError: 入力IDが重複している場合。
    """

    if not items:
        return {}
    original_ids = [str(item["id"]) for item in items]
    if len(original_ids) != len(set(original_ids)):
        raise ValueError("review batch contains duplicate ids")
    expected_ids = [str(index) for index in range(1, len(items) + 1)]

    def split_or_keep_original(error: Exception) -> dict[str, str]:
        """不正なバッチを二分し、単一要素なら原訳を返す。

        Args:
            error: バッチを採用できない理由。

        Returns:
            入力IDから再Review結果または原訳への辞書。
        """

        if len(items) == 1:
            item = items[0]
            LOGGER.warning(
                "Keeping original translation after invalid review response "
                "id=%s error=%s",
                item["id"],
                error,
            )
            return {str(item["id"]): str(item["translated_text"])}
        middle = len(items) // 2
        LOGGER.warning(
            "Splitting invalid review batch items=%s error=%s", len(items), error
        )
        return {
            **review_batch(
                client,
                settings,
                items[:middle],
                translation_rules=translation_rules,
            ),
            **review_batch(
                client,
                settings,
                items[middle:],
                translation_rules=translation_rules,
            ),
        }

    messages = build_review_messages(items, translation_rules)
    try:
        response = chat_text(
            client,
            settings,
            messages,
            json_response=True,
            max_tokens=OPENAI_BATCH_MAX_OUTPUT_TOKENS,
        )
    except OpenAIEmptyResponseError as exc:
        return split_or_keep_original(exc)
    try:
        try:
            payload = json.loads(response)
        except json.JSONDecodeError:
            payload = parse_json_object(response)
    except ValueError as exc:
        return split_or_keep_original(exc)
    if isinstance(payload, list):
        reviews = payload
    elif isinstance(payload, dict):
        reviews = payload.get("reviews")
        if {"id", "reviewed_text"} <= payload.keys():
            reviews = [payload]
    else:
        return split_or_keep_original(ValueError("review response must be an object"))
    if not isinstance(reviews, list):
        return split_or_keep_original(
            ValueError("review response must contain reviews list")
        )
    result: dict[str, str] = {}
    for entry in reviews:
        if not isinstance(entry, dict):
            return split_or_keep_original(ValueError("review entry must be an object"))
        item_id = normalize_batch_response_id(entry.get("id"))
        reviewed_text = entry.get("reviewed_text")
        if (
            item_id is None
            or not isinstance(reviewed_text, str)
            or not reviewed_text.strip()
        ):
            return split_or_keep_original(
                ValueError("review entry must contain string id and text")
            )
        if item_id in result:
            return split_or_keep_original(
                ValueError(f"review response contains duplicate id: {item_id}")
            )
        result[item_id] = reviewed_text
    if set(result) != set(expected_ids):
        missing = sorted(set(expected_ids) - set(result))
        unknown = sorted(set(result) - set(expected_ids))
        return split_or_keep_original(
            ValueError(
                f"review response ids do not match missing={missing} unknown={unknown}"
            )
        )
    reviewed_by_original_id: dict[str, str] = {}
    for index, item in enumerate(items, start=1):
        item_id = str(item["id"])
        reviewed_text = result[str(index)]
        rejection_reason = review_rejection_reason(item, reviewed_text)
        if rejection_reason:
            LOGGER.warning(
                "Keeping original translation after invalid review id=%s error=%s",
                item_id,
                rejection_reason,
            )
            reviewed_text = str(item["translated_text"])
        reviewed_by_original_id[item_id] = reviewed_text
    LOGGER.debug("Reviewed batch items=%s", len(items))
    return reviewed_by_original_id


def apply_review_results(
    targets: list[dict[str, Any]], reviewed_texts: dict[str, str]
) -> int:
    """レビュー結果を translate_ja_v2 metadata に反映する。

    Args:
        targets: review target 配列。
        reviewed_texts: ID からレビュー後訳文への辞書。

    Returns:
        変更した要素数。
    """

    changes = 0
    for target in targets:
        item_id = str(target["id"])
        reviewed = reviewed_texts[item_id].strip()
        meta = cast(dict[str, Any], target["meta"])
        text_field = str(target["text_field"])
        before = str(meta.get(text_field) or "")
        if reviewed == before:
            continue
        changed_chars = sum(
            max(before_end - before_start, reviewed_end - reviewed_start)
            for tag, before_start, before_end, reviewed_start, reviewed_end in SequenceMatcher(
                None, before, reviewed, autojunk=False
            ).get_opcodes()
            if tag != "equal"
        )
        LOGGER.debug("Review changed id=%s changed_chars=%s", item_id, changed_chars)
        meta[text_field] = reviewed
        target["translated_text"] = reviewed
        target["text"] = f"{target['source_text']}\n{reviewed}"
        render_field = str(target["render_field"])
        if render_field == "caption_render" or target["kind"] == "heading":
            meta[render_field] = f"{target['source_text']} / {reviewed}"
        else:
            meta[render_field] = reviewed
        changes += 1
    return changes


def looks_protected(text: str) -> bool:
    """翻訳しないほうがよいコード・URL・識別子か判定する。

    Args:
        text: 判定対象文字列。

    Returns:
        保護対象なら True。
    """

    stripped = text.strip()
    if URL_RE.search(stripped):
        return True
    if re.fullmatch(r"[\w./:\-\\]+", stripped) and not re.search(r"\s", stripped):
        return True
    return bool(
        re.search(
            r"(^|\n)\s*(Traceback|[A-Za-z_][\w-]*\s*=|def |class |import )", stripped
        )
    )


def collect_render_items(data: dict[str, Any]) -> list[tuple[str, int, dict[str, Any]]]:
    """Markdown rendering 対象 item を文書順に集める。

    Args:
        data: 翻訳済み Docling JSON。

    Returns:
        group、index、item のタプル配列。
    """

    items: list[tuple[str, int, dict[str, Any]]] = []
    for group in ("texts", "tables", "pictures"):
        values = data.get(group)
        if not isinstance(values, list):
            continue
        for index, item in enumerate(values):
            if isinstance(item, dict):
                items.append((group, index, cast(dict[str, Any], item)))
    return sorted(
        items, key=lambda entry: ((page_numbers(entry[2]) or [10**9])[0], entry[1])
    )


def render_markdown(data: dict[str, Any]) -> str:
    """翻訳済み Docling JSON を Markdown へ変換する。

    Args:
        data: 翻訳済み Docling JSON。

    Returns:
        Markdown 文字列。
    """

    parts: list[str] = []
    for group, index, item in collect_render_items(data):
        if group == "texts":
            rendered = render_text_item(item)
        elif group == "tables":
            rendered = render_table_item(item, self_ref(item, group, index))
        else:
            rendered = render_picture_item(item)
        if rendered.strip():
            parts.append(rendered.strip())
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(parts)).strip() + "\n"


def render_text_item(item: dict[str, Any]) -> str:
    """Docling text item を Markdown へ変換する。

    Args:
        item: Docling text item。

    Returns:
        Markdown 断片。
    """

    raw_meta = item.get("translate_ja_v2")
    meta = cast(dict[str, Any], raw_meta) if isinstance(raw_meta, dict) else {}
    text = str(meta.get("render_text") or text_of(item)).strip()
    if not text:
        return ""
    if is_code(item):
        return f"```\n{text}\n```"
    if is_heading(item):
        return f"{'#' * heading_level(item)} {text}"
    return text


def render_table_item(item: dict[str, Any], ref: str) -> str:
    """Docling table item を Markdown table へ変換する。

    Args:
        item: Docling table item。
        ref: table item の JSON pointer。

    Returns:
        Markdown table 断片。
    """

    rows = table_rows(item, ref)
    if not rows:
        return text_of(item)
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    header = normalized[0]
    body = normalized[1:]
    lines: list[str] = []
    raw_meta = item.get("translate_ja_v2")
    meta = cast(dict[str, Any], raw_meta) if isinstance(raw_meta, dict) else {}
    caption = str(
        meta.get("caption_render") or item.get("caption") or item.get("title") or ""
    ).strip()
    if caption:
        lines.append(f"**{caption}**")
        lines.append("")
    lines.append(markdown_table_line(header))
    lines.append(markdown_table_line(["---"] * width))
    lines.extend(markdown_table_line(row) for row in body)
    return "\n".join(lines)


def table_rows(item: dict[str, Any], ref: str) -> list[list[str]]:
    """Docling table item から Markdown 用セル行列を作る。

    Args:
        item: Docling table item。
        ref: table item の JSON pointer。

    Returns:
        セル文字列の行列。
    """

    data = item.get("data")
    if not isinstance(data, dict):
        return []
    grid = data.get("grid")
    if isinstance(grid, list):
        return table_rows_from_grid(grid)
    cells = iter_table_cells(item, ref)
    if not cells:
        return []
    normalized: list[tuple[int, int, str]] = []
    max_row = -1
    max_col = -1
    for _cell_ref, cell in cells:
        row = cell.get("start_row_offset_idx", cell.get("row", cell.get("row_idx", 0)))
        col = cell.get("start_col_offset_idx", cell.get("col", cell.get("col_idx", 0)))
        if not isinstance(row, int) or not isinstance(col, int):
            continue
        normalized.append((row, col, cell_render_text(cell)))
        max_row = max(max_row, row)
        max_col = max(max_col, col)
    rows = [["" for _ in range(max_col + 1)] for _ in range(max_row + 1)]
    for row, col, text in normalized:
        rows[row][col] = text
    return rows


def table_rows_from_grid(grid: list[Any]) -> list[list[str]]:
    """Docling grid から Markdown 用セル行列を作る。

    Args:
        grid: Docling table data.grid。

    Returns:
        セル文字列の行列。
    """

    rows: list[list[str]] = []
    for row in grid:
        if not isinstance(row, list):
            continue
        rendered: list[str] = []
        for cell in row:
            if isinstance(cell, dict):
                rendered.append(cell_render_text(cell))
            else:
                rendered.append(str(cell or "").strip())
        rows.append(rendered)
    return rows


def cell_render_text(cell: dict[str, Any]) -> str:
    """table cell の Markdown 表示文字列を返す。

    Args:
        cell: table cell dict。

    Returns:
        Markdown table cell 用文字列。
    """

    raw_meta = cell.get("translate_ja_v2")
    meta = cast(dict[str, Any], raw_meta) if isinstance(raw_meta, dict) else {}
    text = str(meta.get("render_text") or cell.get("text") or cell.get("content") or "")
    return render_inline_code(text.replace("\n", " ").strip(), inline_code_spans(cell))


def render_inline_code(value: str, spans: list[str]) -> str:
    """文字列内のインラインコードspanをMarkdown codeとして囲む。

    Args:
        value: 表示対象文字列。
        spans: 原文と完全一致するインラインコードspan。

    Returns:
        spanをbacktickで囲んだ文字列。
    """

    result = value
    for span in sorted(set(spans), key=len, reverse=True):
        result = result.replace(span, f"`{span}`")
    return result


def markdown_table_line(row: list[str]) -> str:
    """Markdown table の 1 行を作る。

    Args:
        row: セル文字列配列。

    Returns:
        Markdown table 1 行。
    """

    escaped = [cell.replace("|", "\\|") for cell in row]
    return "| " + " | ".join(escaped) + " |"


def render_picture_item(item: dict[str, Any]) -> str:
    """Docling picture item を Markdown image へ変換する。

    Args:
        item: Docling picture item。

    Returns:
        Markdown image 断片。参照がなければ空文字。
    """

    image = item.get("image")
    if isinstance(image, dict) and isinstance(image.get("uri"), str):
        caption = str(item.get("caption") or item.get("text") or "image").strip()
        return f"![{caption}]({image['uri']})"
    return ""


def convert_markdown_to_docx(
    markdown_path: Path, docx_path: Path, template_path: Path | None
) -> None:
    """Markdown を Word docx へ変換する。

    Args:
        markdown_path: 入力 Markdown。
        docx_path: 出力 docx。
        template_path: pandoc reference doc。None の場合は指定しない。

    Returns:
        なし。

    Side Effects:
        pandoc で docx を作成する。

    Raises:
        RuntimeError: pandoc が利用できない場合。
    """

    markdown_path = markdown_path.resolve()
    docx_path = docx_path.resolve()
    template_path = template_path.resolve() if template_path else None
    if shutil.which("pandoc") is None:
        raise RuntimeError("pandoc is required for docx output; use --skip-docx")
    command = [
        "pandoc",
        str(markdown_path),
        "--from",
        "markdown",
        "--to",
        "docx",
        "--output",
        str(docx_path),
    ]
    if template_path:
        if not template_path.exists():
            raise FileNotFoundError(f"template not found: {template_path}")
        command.extend(["--reference-doc", str(template_path)])
    docx_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(command, check=True, cwd=markdown_path.parent)


def update_manifest(path: Path, event: dict[str, Any]) -> None:
    """manifest にstage状態と監査eventを保存する。

    Args:
        path: manifest path。
        event: 追記する event。

    Returns:
        なし。

    Side Effects:
        manifest JSONのstage状態とeventsを作成または更新する。
    """

    manifest = (
        read_json(path)
        if path.exists()
        else {
            "schema_version": 2,
            "run_id": str(uuid.uuid4()),
            "stages": {},
            "events": [],
        }
    )
    manifest["schema_version"] = 2
    stages = manifest.setdefault("stages", {})
    created_at = manifest.setdefault("created_at", utc_now_iso())
    events = manifest.setdefault("events", [])
    stored_event = copy.deepcopy(event)
    stored_event.setdefault("timestamp", utc_now_iso())
    if isinstance(events, list) and (
        stored_event.get("stage") == "start"
        or stored_event.get("status") in {"completed", "skipped"}
    ):
        events.append(
            {key: value for key, value in stored_event.items() if key != "elements"}
        )
    stage = stored_event.get("stage")
    if isinstance(stages, dict) and isinstance(stage, str) and stage != "start":
        stages[stage] = stored_event
    if stage == "start":
        if isinstance(stages, dict):
            for stage_name in PIPELINE_STAGES:
                stages.setdefault(
                    stage_name,
                    {
                        "stage": stage_name,
                        "status": "pending",
                        "updated_at": created_at,
                    },
                )
        manifest["source"] = {
            "path": stored_event.get("input"),
            "sha256": stored_event.get("input_sha256"),
        }
    manifest["updated_at"] = utc_now_iso()
    write_json(path, manifest)


def stage_is_resumable(
    manifest_path: Path,
    stage: str,
    output_path: Path,
    input_hash: str,
    config_hash: str,
    extra_outputs: dict[str, Path] | None = None,
) -> bool:
    """完了済みstageの入力・設定・成果物hashが有効か検証する。

    Args:
        manifest_path: stage状態を保持するmanifest。
        stage: 検証するstage名。
        output_path: stageの主成果物。
        input_hash: 現在の入力hash。
        config_hash: 現在の設定hash。
        extra_outputs: 追加成果物名とパス。Parseのartifactsなどに使う。

    Returns:
        既存成果物を再利用できる場合はTrue。
    """

    if not manifest_path.is_file() or not output_path.is_file():
        return False
    manifest = read_json(manifest_path)
    stages = manifest.get("stages") if isinstance(manifest, dict) else None
    state = stages.get(stage) if isinstance(stages, dict) else None
    if not isinstance(state, dict) or state.get("status") != "completed":
        return False
    if (
        state.get("input_sha256") != input_hash
        or state.get("config_sha256") != config_hash
    ):
        return False
    try:
        if state.get("output_sha256") != sha256_file(output_path):
            LOGGER.warning(
                "Stage output hash mismatch stage=%s output=%s", stage, output_path
            )
            return False
        for name, path in (extra_outputs or {}).items():
            if state.get(f"{name}_sha256") != sha256_path(path):
                LOGGER.warning(
                    "Stage output hash mismatch stage=%s output=%s", stage, path
                )
                return False
    except (FileNotFoundError, OSError):
        return False
    LOGGER.info("Resuming completed stage stage=%s output=%s", stage, output_path)
    return True


def load_stage_checkpoint(
    manifest_path: Path,
    stage: str,
    output_path: Path,
    input_hash: str,
    config_hash: str,
) -> tuple[dict[str, Any], set[str]] | None:
    """実行途中stageのJSON checkpointと完了要素IDを読み込む。

    Args:
        manifest_path: stage状態を保持するmanifest。
        stage: 読み込むstage名。
        output_path: 部分成果物JSON。
        input_hash: 現在の入力hash。
        config_hash: 現在の設定hash。

    Returns:
        部分成果物と完了要素ID。再利用できない場合はNone。
    """

    if not manifest_path.is_file() or not output_path.is_file():
        return None
    manifest = read_json(manifest_path)
    stages = manifest.get("stages") if isinstance(manifest, dict) else None
    state = stages.get(stage) if isinstance(stages, dict) else None
    if not isinstance(state, dict) or state.get("status") != "running":
        return None
    if (
        state.get("input_sha256") != input_hash
        or state.get("config_sha256") != config_hash
    ):
        return None
    try:
        if state.get("output_sha256") != sha256_file(output_path):
            LOGGER.warning(
                "Stage checkpoint hash mismatch stage=%s output=%s", stage, output_path
            )
            return None
        document = read_json(output_path)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    elements = state.get("elements")
    completed: set[str] = set()
    if isinstance(elements, dict):
        completed = {
            str(element_id)
            for element_id, element_state in elements.items()
            if isinstance(element_state, dict)
            and element_state.get("status") == "completed"
        }
    LOGGER.info(
        "Resuming stage checkpoint stage=%s completed_elements=%s",
        stage,
        len(completed),
    )
    return document, completed


def write_stage_checkpoint(
    manifest_path: Path,
    stage: str,
    output_path: Path,
    document: dict[str, Any],
    input_hash: str,
    config_hash: str,
    element_ids: set[str],
    completed_ids: set[str],
) -> None:
    """部分成果物と要素別進捗をatomic保存する。

    Args:
        manifest_path: stage状態を保持するmanifest。
        stage: checkpoint対象のstage名。
        output_path: 部分成果物JSONの保存先。
        document: 現在までの処理結果。
        input_hash: stage入力のhash。
        config_hash: stage設定のhash。
        element_ids: stageが管理する全要素ID。
        completed_ids: 完了した要素ID。

    Returns:
        なし。

    Side Effects:
        部分成果物とmanifestをatomic更新する。
    """

    write_json(output_path, document)
    manifest = (
        read_json(manifest_path)
        if manifest_path.exists()
        else {
            "schema_version": 2,
            "run_id": str(uuid.uuid4()),
            "stages": {},
            "events": [],
        }
    )
    manifest["schema_version"] = 2
    stages = manifest.setdefault("stages", {})
    if isinstance(stages, dict):
        stages[stage] = {
            "stage": stage,
            "status": "running",
            "input_sha256": input_hash,
            "config_sha256": config_hash,
            "output": str(output_path),
            "output_sha256": sha256_file(output_path),
            "elements": {
                element_id: {
                    "status": (
                        "completed" if element_id in completed_ids else "pending"
                    )
                }
                for element_id in sorted(element_ids)
            },
            "updated_at": utc_now_iso(),
        }
    manifest["updated_at"] = utc_now_iso()
    write_json(manifest_path, manifest)


def record_stage_start(
    manifest_path: Path,
    stage: str,
    output_path: Path,
    input_hash: str,
    config_hash: str,
    element_ids: set[str] | None = None,
) -> None:
    """未完了stageをrunningとしてmanifestへ記録する。

    Args:
        manifest_path: 保存するmanifest。
        stage: 開始するstage名。
        output_path: stageの主成果物予定パス。
        input_hash: stage入力のhash。
        config_hash: stage設定のhash。
        element_ids: 要素別管理をするstageの全要素ID。

    Returns:
        なし。

    Side Effects:
        manifestをatomic更新する。
    """

    details: dict[str, Any] = {}
    if element_ids is not None:
        details["elements"] = {
            element_id: {"status": "pending"} for element_id in sorted(element_ids)
        }
    update_manifest(
        manifest_path,
        {
            "stage": stage,
            "status": "running",
            "input_sha256": input_hash,
            "config_sha256": config_hash,
            "output": str(output_path),
            **details,
        },
    )
    LOGGER.info("Stage started stage=%s output=%s", stage, output_path)


def record_stage_skipped(manifest_path: Path, stage: str, reason: str) -> None:
    """省略されたstageをmanifestへ記録する。

    Args:
        manifest_path: 保存するmanifest。
        stage: 省略したstage名。
        reason: 省略理由。

    Returns:
        なし。

    Side Effects:
        manifestをatomic更新する。
    """

    update_manifest(
        manifest_path,
        {"stage": stage, "status": "skipped", "reason": reason},
    )
    LOGGER.info("Stage skipped stage=%s reason=%s", stage, reason)


def element_progress(
    element_ids: set[str], completed_ids: set[str]
) -> dict[str, dict[str, str]]:
    """全対象IDのpending/completed状態を組み立てる。

    Args:
        element_ids: stageが管理する全要素ID。
        completed_ids: 完了した要素ID。

    Returns:
        要素IDをキーにした状態map。
    """

    return {
        element_id: {
            "status": "completed" if element_id in completed_ids else "pending"
        }
        for element_id in sorted(element_ids)
    }


def record_stage_completion(
    manifest_path: Path,
    stage: str,
    output_path: Path,
    input_hash: str,
    config_hash: str,
    *,
    extra_outputs: dict[str, Path] | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """完了stageの入力・設定・成果物hashをmanifestへ記録する。

    Args:
        manifest_path: 保存するmanifest。
        stage: 完了したstage名。
        output_path: stageの主成果物。
        input_hash: stage入力のhash。
        config_hash: stage設定のhash。
        extra_outputs: 追加成果物名とパス。
        details: patch数などstage固有の記録。

    Returns:
        なし。

    Side Effects:
        manifestをatomic更新する。
    """

    event: dict[str, Any] = {
        "stage": stage,
        "status": "completed",
        "input_sha256": input_hash,
        "config_sha256": config_hash,
        "output": str(output_path),
        "output_sha256": sha256_file(output_path),
    }
    for name, path in (extra_outputs or {}).items():
        event[name] = str(path)
        event[f"{name}_sha256"] = sha256_path(path)
    if details:
        event.update(details)
    update_manifest(manifest_path, event)
    LOGGER.info("Stage completed stage=%s output=%s", stage, output_path)


class ParseStage(FrozenModel):
    """入力文書を Docling JSON へ変換し、読み込む。"""

    input_path: Path
    paths: StagePaths
    artifacts_dir: Path
    force: bool

    def run(self) -> dict[str, Any]:
        """Docling JSON を返す。"""

        input_hash = sha256_file(self.input_path)
        config_hash = sha256_json(
            {
                "version": 3,
                "payload": docling_form_payload(DOCLING_TIMEOUT_SECONDS),
                "pdf_chunk_pages": DOCLING_PDF_CHUNK_PAGES,
                "local_page_images": {
                    "renderer": "pypdfium2",
                    "scale": PAGE_IMAGE_SCALE,
                },
            }
        )
        can_resume = not self.force and stage_is_resumable(
            self.paths.manifest,
            "parse",
            self.paths.document_json,
            input_hash,
            config_hash,
            {"artifacts": self.artifacts_dir},
        )
        if not can_resume:
            record_stage_start(
                self.paths.manifest,
                "parse",
                self.paths.document_json,
                input_hash,
                config_hash,
            )
            convert_with_docling(
                self.input_path,
                self.paths.document_json,
                self.artifacts_dir,
            )
            record_stage_completion(
                self.paths.manifest,
                "parse",
                self.paths.document_json,
                input_hash,
                config_hash,
                extra_outputs={"artifacts": self.artifacts_dir},
            )
        document = read_json(self.paths.document_json)
        if not isinstance(document, dict):
            raise ValueError("Docling JSON root must be an object")
        return document


class NormalizeStage(FrozenModel):
    """座標に基づく決定論的補正を実行する。"""

    paths: StagePaths

    def run(self, document: dict[str, Any]) -> dict[str, Any]:
        """正規化済み文書を返す。"""

        input_hash = sha256_json(document)
        config_hash = sha256_json({"version": 2})
        if stage_is_resumable(
            self.paths.manifest,
            "normalize",
            self.paths.normalized_json,
            input_hash,
            config_hash,
        ):
            normalized = read_json(self.paths.normalized_json)
            if not isinstance(normalized, dict):
                raise ValueError("Normalized JSON root must be an object")
            return normalized
        record_stage_start(
            self.paths.manifest,
            "normalize",
            self.paths.normalized_json,
            input_hash,
            config_hash,
        )
        normalized, patches = normalize_document(document)
        write_json(self.paths.normalized_json, normalized)
        record_stage_completion(
            self.paths.manifest,
            "normalize",
            self.paths.normalized_json,
            input_hash,
            config_hash,
            details={
                "patches": len(patches),
                "coordinate_patches": sum(
                    patch.get("rule") == "bbox_reading_order" for patch in patches
                ),
            },
        )
        return normalized


class StructureStage(FrozenModel):
    """VLM による構造補正を実行する。"""

    paths: StagePaths
    artifacts_dir: Path
    skip_vlm: bool
    context_chars: int = OPENAI_CONTEXT_LIMIT_CHARS

    def run(self, document: dict[str, Any]) -> dict[str, Any]:
        """構造補正済み文書を返す。"""

        input_hash = sha256_json(
            {
                "document": document,
                "artifacts_sha256": (
                    sha256_directory(self.artifacts_dir)
                    if self.artifacts_dir.is_dir() and not self.skip_vlm
                    else None
                ),
            }
        )
        config_hash = sha256_json(
            {
                "version": 5,
                "skip_vlm": self.skip_vlm,
                "context_chars": self.context_chars,
                "model": None if self.skip_vlm else os.environ.get("OPENAI_MODEL"),
            }
        )
        if stage_is_resumable(
            self.paths.manifest,
            "structure",
            self.paths.structured_json,
            input_hash,
            config_hash,
        ):
            structured = read_json(self.paths.structured_json)
            if not isinstance(structured, dict):
                raise ValueError("Structured JSON root must be an object")
            return structured
        checkpoint = load_stage_checkpoint(
            self.paths.manifest,
            "structure",
            self.paths.structured_json,
            input_hash,
            config_hash,
        )
        resume_data, completed_ids = checkpoint or (None, set())
        element_ids = {
            str(unit["ref"])
            for unit in collect_structure_units(document)
            + collect_table_cell_structure_units(document)
        }
        if checkpoint is None:
            record_stage_start(
                self.paths.manifest,
                "structure",
                self.paths.structured_json,
                input_hash,
                config_hash,
                element_ids,
            )

        def save_progress(
            current: dict[str, Any], newly_completed_ids: list[str]
        ) -> None:
            """Structure部分成果物と完了text refを保存する。

            Args:
                current: 現在の構造補正結果。
                newly_completed_ids: 新たに完了した入力text ref。

            Returns:
                なし。
            """

            completed_ids.update(newly_completed_ids)
            write_stage_checkpoint(
                self.paths.manifest,
                "structure",
                self.paths.structured_json,
                current,
                input_hash,
                config_hash,
                element_ids,
                completed_ids,
            )

        structured, patches = structure_document(
            document,
            skip_vlm=self.skip_vlm,
            artifacts_dir=self.artifacts_dir,
            context_chars=self.context_chars,
            resume_data=resume_data,
            completed_ids=completed_ids,
            on_progress=save_progress,
        )
        completed_ids.update(element_ids)
        write_json(self.paths.structured_json, structured)
        record_stage_completion(
            self.paths.manifest,
            "structure",
            self.paths.structured_json,
            input_hash,
            config_hash,
            details={
                "patches": len(patches),
                "elements": element_progress(element_ids, completed_ids),
            },
        )
        return structured


class CleanStage(FrozenModel):
    """本文と表セルの連続記号を決定論的に校正する。

    Args:
        paths: 各工程の成果物パス。

    Returns:
        工程単位Resumeに対応するClean工程。
    """

    paths: StagePaths

    def run(self, document: dict[str, Any]) -> dict[str, Any]:
        """Clean済み文書を返す。

        Args:
            document: Structure済みDocling JSON。

        Returns:
            連続記号を校正したDocling JSON。

        Side Effects:
            document.cleaned.jsonとmanifestの工程状態を更新する。
        """

        input_hash = sha256_json(document)
        config_hash = sha256_json({"version": 1, "characters": [".", "・"]})
        if stage_is_resumable(
            self.paths.manifest,
            "clean",
            self.paths.cleaned_json,
            input_hash,
            config_hash,
        ):
            cleaned = read_json(self.paths.cleaned_json)
            if not isinstance(cleaned, dict):
                raise ValueError("Cleaned JSON root must be an object")
            return cleaned
        record_stage_start(
            self.paths.manifest,
            "clean",
            self.paths.cleaned_json,
            input_hash,
            config_hash,
        )
        cleaned, patches = clean_document(document)
        write_json(self.paths.cleaned_json, cleaned)
        record_stage_completion(
            self.paths.manifest,
            "clean",
            self.paths.cleaned_json,
            input_hash,
            config_hash,
            details={"patches": len(patches)},
        )
        return cleaned


class TranslateStage(FrozenModel):
    """文書要素を日本語へ翻訳する。"""

    paths: StagePaths
    glossary_path: Path | None = None
    translation_rules_path: Path | None = None
    context_chars: int = OPENAI_CONTEXT_LIMIT_CHARS
    batch_chars: int = TRANSLATION_BATCH_MAX_CHARS

    def run(self, document: dict[str, Any]) -> dict[str, Any]:
        """翻訳 metadata を付与した文書を返す。"""

        glossary = read_glossary_csv(self.glossary_path)
        translation_rules = read_translation_rules(self.translation_rules_path)
        input_hash = sha256_json(document)
        config_hash = sha256_json(
            {
                "version": 7,
                "model": os.environ.get("OPENAI_MODEL"),
                "context_chars": self.context_chars,
                "batch_chars": self.batch_chars,
                "glossary": glossary,
                "translation_rules": translation_rules,
            }
        )
        if stage_is_resumable(
            self.paths.manifest,
            "translate",
            self.paths.translated_json,
            input_hash,
            config_hash,
        ):
            translated = read_json(self.paths.translated_json)
            if not isinstance(translated, dict):
                raise ValueError("Translated JSON root must be an object")
            return translated
        checkpoint = load_stage_checkpoint(
            self.paths.manifest,
            "translate",
            self.paths.translated_json,
            input_hash,
            config_hash,
        )
        resume_data, completed_ids = checkpoint or (None, set())
        element_ids = translation_element_ids(document)
        if checkpoint is None:
            record_stage_start(
                self.paths.manifest,
                "translate",
                self.paths.translated_json,
                input_hash,
                config_hash,
                element_ids,
            )

        def save_progress(
            current: dict[str, Any], newly_completed_ids: list[str]
        ) -> None:
            """Translate部分成果物と完了要素IDを保存する。

            Args:
                current: 現在の翻訳結果。
                newly_completed_ids: 新たに完了した要素ID。

            Returns:
                なし。
            """

            completed_ids.update(newly_completed_ids)
            write_stage_checkpoint(
                self.paths.manifest,
                "translate",
                self.paths.translated_json,
                current,
                input_hash,
                config_hash,
                element_ids,
                completed_ids,
            )

        translated = translate_document(
            document,
            glossary=glossary,
            translation_rules=translation_rules,
            context_chars=self.context_chars,
            batch_chars=self.batch_chars,
            resume_data=resume_data,
            completed_ids=completed_ids,
            on_progress=save_progress,
        )
        completed_ids.update(element_ids)
        write_json(self.paths.translated_json, translated)
        record_stage_completion(
            self.paths.manifest,
            "translate",
            self.paths.translated_json,
            input_hash,
            config_hash,
            details={"elements": element_progress(element_ids, completed_ids)},
        )
        return translated


class ReviewStage(FrozenModel):
    """翻訳済み文書をレビューする。"""

    paths: StagePaths
    translation_rules_path: Path | None = None
    context_chars: int = OPENAI_CONTEXT_LIMIT_CHARS
    batch_chars: int = TRANSLATION_BATCH_MAX_CHARS

    def run(self, document: dict[str, Any]) -> dict[str, Any]:
        """レビュー済み文書を返す。"""

        translation_rules = read_translation_rules(self.translation_rules_path)
        input_hash = sha256_json(document)
        config_hash = sha256_json(
            {
                "version": 6,
                "model": os.environ.get("OPENAI_MODEL"),
                "context_chars": self.context_chars,
                "batch_chars": self.batch_chars,
                "translation_rules": translation_rules,
            }
        )
        if stage_is_resumable(
            self.paths.manifest,
            "review",
            self.paths.reviewed_json,
            input_hash,
            config_hash,
        ):
            reviewed = read_json(self.paths.reviewed_json)
            if not isinstance(reviewed, dict):
                raise ValueError("Reviewed JSON root must be an object")
            return reviewed
        checkpoint = load_stage_checkpoint(
            self.paths.manifest,
            "review",
            self.paths.reviewed_json,
            input_hash,
            config_hash,
        )
        resume_data, completed_ids = checkpoint or (None, set())
        element_ids = {str(target["id"]) for target in collect_review_targets(document)}
        if checkpoint is None:
            record_stage_start(
                self.paths.manifest,
                "review",
                self.paths.reviewed_json,
                input_hash,
                config_hash,
                element_ids,
            )

        def save_progress(
            current: dict[str, Any], newly_completed_ids: list[str]
        ) -> None:
            """Review部分成果物と完了対象IDを保存する。

            Args:
                current: 現在のレビュー結果。
                newly_completed_ids: 新たに完了したレビュー対象ID。

            Returns:
                なし。
            """

            completed_ids.update(newly_completed_ids)
            write_stage_checkpoint(
                self.paths.manifest,
                "review",
                self.paths.reviewed_json,
                current,
                input_hash,
                config_hash,
                element_ids,
                completed_ids,
            )

        reviewed, changes = review_document(
            document,
            translation_rules=translation_rules,
            context_chars=self.context_chars,
            batch_chars=self.batch_chars,
            resume_data=resume_data,
            completed_ids=completed_ids,
            on_progress=save_progress,
        )
        completed_ids.update(element_ids)
        write_json(self.paths.reviewed_json, reviewed)
        record_stage_completion(
            self.paths.manifest,
            "review",
            self.paths.reviewed_json,
            input_hash,
            config_hash,
            details={
                "changes": changes,
                "elements": element_progress(element_ids, completed_ids),
            },
        )
        return reviewed


class RenderStage(FrozenModel):
    """翻訳済み文書から Markdown を生成する。"""

    paths: StagePaths

    def run(self, document: dict[str, Any]) -> Path:
        """生成した Markdown のパスを返す。"""

        input_hash = sha256_json(document)
        config_hash = sha256_json({"version": 1})
        if stage_is_resumable(
            self.paths.manifest,
            "markdown",
            self.paths.markdown,
            input_hash,
            config_hash,
        ):
            return self.paths.markdown
        record_stage_start(
            self.paths.manifest,
            "markdown",
            self.paths.markdown,
            input_hash,
            config_hash,
        )
        markdown = render_markdown(document)
        atomic_write_bytes(self.paths.markdown, markdown.encode("utf-8"))
        record_stage_completion(
            self.paths.manifest,
            "markdown",
            self.paths.markdown,
            input_hash,
            config_hash,
        )
        return self.paths.markdown


class DocxStage(FrozenModel):
    """Markdown を Word docx へ変換する。"""

    paths: StagePaths
    template: Path | None

    def run(self, markdown_path: Path) -> Path:
        """生成した docx のパスを返す。"""

        input_hash = sha256_file(markdown_path)
        config_hash = sha256_json(
            {
                "version": 1,
                "template_sha256": (
                    sha256_file(self.template) if self.template is not None else None
                ),
            }
        )
        if stage_is_resumable(
            self.paths.manifest,
            "docx",
            self.paths.docx,
            input_hash,
            config_hash,
        ):
            return self.paths.docx
        record_stage_start(
            self.paths.manifest,
            "docx",
            self.paths.docx,
            input_hash,
            config_hash,
        )
        convert_markdown_to_docx(markdown_path, self.paths.docx, self.template)
        record_stage_completion(
            self.paths.manifest,
            "docx",
            self.paths.docx,
            input_hash,
            config_hash,
        )
        return self.paths.docx


def run_pipeline(args: PipelineOptions) -> StagePaths:
    """CLI 引数に従って translate-ja-v2 パイプラインを実行する。

    Args:
        args: Typer から組み立てた CLI オプション。

    Returns:
        StagePaths。

    Side Effects:
        Docling/OpenAI/pandoc を呼び出し、成果物を出力する。
    """

    input_path = args.input.resolve()
    paths = build_stage_paths(
        input_path,
        args.output_dir.resolve() if args.output_dir else None,
        args.output.resolve() if args.output else None,
    )
    paths.output_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir = paths.output_dir / "artifacts"
    update_manifest(
        paths.manifest,
        {
            "stage": "start",
            "input": str(input_path),
            "input_sha256": sha256_file(input_path),
        },
    )
    docling = ParseStage(
        input_path=input_path,
        paths=paths,
        artifacts_dir=artifacts_dir,
        force=args.force,
    ).run()
    normalized = NormalizeStage(paths=paths).run(docling)
    structured = StructureStage(
        paths=paths,
        artifacts_dir=artifacts_dir,
        skip_vlm=args.skip_vlm,
        context_chars=args.context_chars,
    ).run(normalized)
    cleaned = CleanStage(paths=paths).run(structured)
    translated = TranslateStage(
        paths=paths,
        glossary_path=args.glossary,
        translation_rules_path=args.translation_rules,
        context_chars=args.context_chars,
        batch_chars=args.batch_chars,
    ).run(cleaned)
    if args.skip_review:
        record_stage_skipped(paths.manifest, "review", "--skip-review")
        render_source = translated
    else:
        render_source = ReviewStage(
            paths=paths,
            translation_rules_path=args.translation_rules,
            context_chars=args.context_chars,
            batch_chars=args.batch_chars,
        ).run(translated)
    markdown_path = RenderStage(paths=paths).run(render_source)
    if not args.skip_docx:
        DocxStage(
            paths=paths,
            template=args.template.resolve() if args.template else None,
        ).run(markdown_path)
    else:
        record_stage_skipped(paths.manifest, "docx", "--skip-docx")
    return paths


app = typer.Typer(
    add_completion=False,
    help="translate-ja-v2 document translation pipeline",
)


@app.command()
def cli(
    input: Annotated[Path, typer.Option(help="PDF/Word input path")],
    output_dir: Annotated[
        Path | None, typer.Option(help="artifact output directory")
    ] = None,
    output: Annotated[Path | None, typer.Option(help="final docx output path")] = None,
    template: Annotated[
        Path | None, typer.Option(help="pandoc reference docx/dotx")
    ] = None,
    skip_vlm: Annotated[
        bool, typer.Option(help="skip VLM structure correction")
    ] = False,
    skip_review: Annotated[bool, typer.Option(help="skip translation review")] = False,
    skip_docx: Annotated[bool, typer.Option(help="write Markdown/JSON only")] = False,
    force: Annotated[
        bool,
        typer.Option(help="rerun Docling conversion even if JSON exists"),
    ] = False,
    env: Annotated[Path, typer.Option(help="dotenv path")] = Path(".env"),
    glossary: Annotated[
        Path | None,
        typer.Option(help="CSV glossary with english,japanese,desc,genre,note"),
    ] = None,
    translation_rules: Annotated[
        Path | None, typer.Option(help="translation rules text file")
    ] = None,
    context_chars: Annotated[
        int,
        typer.Option(min=1, help="maximum text characters per OpenAI request"),
    ] = OPENAI_CONTEXT_LIMIT_CHARS,
    batch_chars: Annotated[
        int,
        typer.Option(
            min=1,
            help="maximum source and translation characters per LLM batch",
        ),
    ] = TRANSLATION_BATCH_MAX_CHARS,
) -> None:
    """CLI から translate-ja-v2 パイプラインを実行する。

    Args:
        input: PDF/Word 入力ファイル。
        output_dir: 中間成果物の出力ディレクトリ。
        output: 最終 docx の出力パス。
        template: pandoc reference docx/dotx。
        skip_vlm: VLM による構造補正を省略するかどうか。
        skip_review: 翻訳レビューを省略するかどうか。
        skip_docx: docx 生成を省略するかどうか。
        force: 既存 Docling JSON があっても変換を再実行するかどうか。
        env: dotenv ファイルのパス。
        glossary: CSV 用語集のパス。
        translation_rules: 翻訳ルール本文ファイルのパス。
        context_chars: OpenAI request の最大テキスト文字数。
        batch_chars: 翻訳・Reviewバッチの最大原文・訳文文字数。

    Returns:
        なし。

    Side Effects:
        パイプラインを実行し、終了コードを Typer へ渡す。
    """

    options = PipelineOptions(
        input=input,
        output_dir=output_dir,
        output=output,
        template=template,
        skip_vlm=skip_vlm,
        skip_review=skip_review,
        skip_docx=skip_docx,
        force=force,
        env=env,
        glossary=glossary,
        translation_rules=translation_rules,
        context_chars=context_chars,
        batch_chars=batch_chars,
    )
    load_dotenv_file(options.env)
    configure_logging()
    try:
        paths = run_pipeline(options)
    except KeyboardInterrupt:
        LOGGER.error("translate-ja-v2 was interrupted")
        raise typer.Exit(code=130) from None
    except Exception as exc:
        LOGGER.exception("translate-ja-v2 failed: %s", exc)
        raise typer.Exit(code=1) from None
    LOGGER.info(
        "translate-ja-v2 completed markdown=%s docx=%s", paths.markdown, paths.docx
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint を実行する。

    Args:
        argv: コマンドライン引数。None の場合は sys.argv を使う。

    Returns:
        プロセス終了コード。

    Side Effects:
        環境変数、ログ、Docling/OpenAI/pandoc、成果物ファイルを扱う。
    """

    command = typer.main.get_command(app)
    try:
        result = command.main(args=argv, standalone_mode=False)
    except typer.Exit as exc:
        return int(exc.exit_code or 0)
    return int(result or 0)


if __name__ == "__main__":
    started_at = perf_counter()
    exit_code = main()
    LOGGER.debug("Elapsed time %.3f seconds", perf_counter() - started_at)
    sys.exit(exit_code)
