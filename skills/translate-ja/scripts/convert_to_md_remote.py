"""Docling Serve v1 API で入力文書を Markdown へ変換する。"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import socket
import sys
import time
import zipfile
from pathlib import Path
from time import perf_counter
from typing import Any

import httpx
from dotenv import load_dotenv as python_dotenv_load_dotenv

from config import require_docling_settings
from io_utils import atomic_write_text, configure_logging, ensure_dir, log_jsonl

LOGGER = logging.getLogger("translate-ja.convert_to_md_remote")


def load_dotenv(path: str | Path = ".env") -> None:
    """python-dotenv で .env を環境変数へ読み込む。

    Args:
        path: 読み込む .env ファイル。

    Returns:
        なし。

    Side Effects:
        既存環境変数を上書きせず、未設定のキーだけ os.environ へ追加する。
    """

    python_dotenv_load_dotenv(dotenv_path=Path(path), override=False)


def env_bool(name: str, default: bool) -> bool:
    """環境変数を bool として解釈する。

    Args:
        name: 環境変数名。
        default: 環境変数が未設定または空の場合の値。

    Returns:
        真偽値として解釈した結果。
    """

    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def format_connection_error(endpoint: str, exc: Exception) -> str:
    """Docling 接続エラーを運用者向けの説明へ整形する。

    Args:
        endpoint: 接続先 endpoint。
        exc: 発生した例外。

    Returns:
        ログ出力向けの説明文。
    """

    return (
        f"Docling Serve に接続できません endpoint={endpoint}。"
        " DOCLING_SERVER_URL が到達可能なホストを指しているか、Docling Serve が起動しているかを確認してください。"
        f" detail={exc}"
    )


def docling_markdown_payload(document_timeout: int) -> dict[str, str | list[str]]:
    """Docling Serve v1 に渡す Markdown 変換用 multipart form field を作る。

    Args:
        document_timeout: Docling 側の文書処理 timeout 秒数。

    Returns:
        httpx に渡す form field。
    """

    do_ocr = env_bool("DOCLING_DO_OCR", default=False)
    force_ocr = env_bool("DOCLING_FORCE_OCR", default=False)
    payload: dict[str, str | list[str]] = {
        "to_formats": os.environ.get("DOCLING_MARKDOWN_FORMAT", "md"),
        "do_ocr": str(do_ocr).lower(),
        "force_ocr": str(force_ocr).lower(),
        "document_timeout": str(document_timeout),
        "do_picture_description": "false",
        "include_images": "true",
        "include_page_images": "true",
        "image_export_mode": "referenced",
        "target_type": "zip",
    }
    if do_ocr or force_ocr:
        payload["ocr_preset"] = os.environ.get("DOCLING_OCR_PRESET", "tesseract")
        languages = os.environ.get("DOCLING_OCR_LANGS", "jpn,jpn_vert,eng")
        payload["ocr_lang"] = [
            part.strip() for part in languages.split(",") if part.strip()
        ]
    return payload


def response_mentions_missing_files(response: Any) -> bool:
    """422 応答が files field 不足を示しているかを判定する。

    Args:
        response: httpx.Response 互換 object。

    Returns:
        files field 不足を示す場合は True。
    """

    try:
        payload = response.json()
    except ValueError:
        return "files" in response.text and "Field required" in response.text
    details = payload.get("detail") if isinstance(payload, dict) else None
    if not isinstance(details, list):
        return False
    for detail in details:
        if not isinstance(detail, dict):
            continue
        loc = detail.get("loc")
        if isinstance(loc, list) and "files" in loc and detail.get("type") == "missing":
            return True
    return False


def request_convert(
    endpoint: str, input_path: Path, *, docling_timeout: int, request_timeout: int
) -> Any:
    """Docling Serve へ multipart request を送り、file/files 差分を吸収する。

    Args:
        endpoint: Docling Serve の変換 endpoint。
        input_path: 変換対象ファイル。
        docling_timeout: Docling 側の文書処理 timeout 秒数。
        request_timeout: HTTP request の timeout 秒数。

    Returns:
        httpx.Response。

    Raises:
        RuntimeError: 接続に失敗した場合。
    """

    settings = require_docling_settings(timeout_seconds=docling_timeout)
    last_response = None
    for file_field in ("files", "file"):
        with input_path.open("rb") as file:
            files = {file_field: (input_path.name, file)}
            try:
                response = httpx.post(
                    endpoint,
                    headers={"X-Api-Key": settings.api_key},
                    files=files,
                    data=docling_markdown_payload(docling_timeout),
                    timeout=request_timeout,
                )
            except httpx.RequestError as exc:
                raise RuntimeError(format_connection_error(endpoint, exc)) from exc
            except socket.gaierror as exc:
                raise RuntimeError(format_connection_error(endpoint, exc)) from exc
        last_response = response
        if response.status_code not in {400, 422}:
            return response
        if file_field == "files" and not response_mentions_missing_files(response):
            break
    return last_response


def post_convert_sync(
    input_path: Path, output_zip: Path, *, docling_timeout: int
) -> None:
    """Docling Serve の同期変換 endpoint を呼び出し、ZIP 応答を保存する。

    Args:
        input_path: 変換対象ファイル。
        output_zip: ZIP 応答の保存先。
        docling_timeout: Docling 側の文書処理 timeout 秒数。

    Returns:
        なし。

    Raises:
        RuntimeError: Docling Serve がエラー応答を返した場合。
    """

    settings = require_docling_settings(timeout_seconds=docling_timeout)
    endpoint = f"{settings.server_url}/v1/convert/file"
    response = request_convert(
        endpoint,
        input_path,
        docling_timeout=docling_timeout,
        request_timeout=settings.timeout_seconds,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Docling sync convert failed status={response.status_code} body={response.text[:500]}"
        )
    output_zip.write_bytes(response.content)


def poll_async(task_id: str, output_zip: Path, *, docling_timeout: int) -> None:
    """Docling Serve async task を poll して結果 ZIP を保存する。

    Args:
        task_id: async 変換 task id。
        output_zip: ZIP 応答の保存先。
        docling_timeout: Docling 側の文書処理 timeout 秒数。

    Returns:
        なし。

    Raises:
        RuntimeError: task が失敗した場合。
        TimeoutError: timeout までに完了しない場合。
    """

    settings = require_docling_settings(timeout_seconds=docling_timeout)
    deadline = time.monotonic() + settings.timeout_seconds
    while time.monotonic() < deadline:
        status_response = httpx.get(
            f"{settings.server_url}/v1/status/poll/{task_id}",
            headers={"X-Api-Key": settings.api_key},
            timeout=60,
        )
        if status_response.status_code >= 400:
            raise RuntimeError(
                f"Docling status poll failed status={status_response.status_code}"
            )
        status_data = status_response.json()
        status = str(
            status_data.get("status") or status_data.get("task_status") or ""
        ).lower()
        if status in {"success", "succeeded", "completed"}:
            result_response = httpx.get(
                f"{settings.server_url}/v1/result/{task_id}",
                headers={"X-Api-Key": settings.api_key},
                timeout=settings.timeout_seconds,
            )
            if result_response.status_code >= 400:
                raise RuntimeError(
                    f"Docling result failed status={result_response.status_code}"
                )
            output_zip.write_bytes(result_response.content)
            return
        if status in {"failure", "failed", "error"}:
            raise RuntimeError(f"Docling async task failed task_id={task_id}")
        LOGGER.info(
            "Docling async task is running task_id=%s status=%s position=%s",
            task_id,
            status or "unknown",
            status_data.get("task_position"),
        )
        time.sleep(10)
    raise TimeoutError(f"Docling async task timed out task_id={task_id}")


def post_convert_async(
    input_path: Path, output_zip: Path, *, docling_timeout: int
) -> None:
    """Docling Serve の async endpoint を呼び出す。

    Args:
        input_path: 変換対象ファイル。
        output_zip: ZIP 応答の保存先。
        docling_timeout: Docling 側の文書処理 timeout 秒数。

    Returns:
        なし。

    Raises:
        RuntimeError: Docling Serve がエラー応答を返した場合。
    """

    settings = require_docling_settings(timeout_seconds=docling_timeout)
    endpoint = f"{settings.server_url}/v1/convert/file/async"
    response = request_convert(
        endpoint, input_path, docling_timeout=docling_timeout, request_timeout=120
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Docling async convert failed status={response.status_code} body={response.text[:500]}"
        )
    payload = response.json()
    task_id = payload.get("task_id") or payload.get("id")
    if not task_id:
        raise RuntimeError("Docling async response has no task_id")
    poll_async(str(task_id), output_zip, docling_timeout=docling_timeout)


def markdown_member_names(archive: zipfile.ZipFile) -> list[str]:
    """Docling ZIP 内の Markdown member 名を列挙する。

    Args:
        archive: Docling Serve が返した ZIP。

    Returns:
        Markdown とみなす member 名の配列。
    """

    return [
        name
        for name in archive.namelist()
        if name.lower().endswith((".md", ".markdown")) and not name.endswith("/")
    ]


def extract_zip_result(zip_path: Path, output_path: Path) -> None:
    """Docling ZIP から Markdown と artifacts を出力する。

    Args:
        zip_path: Docling Serve が返した ZIP。
        output_path: Markdown 出力先。

    Returns:
        なし。

    Raises:
        RuntimeError: ZIP 内に Markdown がない場合。
    """

    output_path.parent.mkdir(parents=True, exist_ok=True)
    artifacts_dir = ensure_dir(output_path.parent / "artifacts")
    with zipfile.ZipFile(zip_path, "r") as archive:
        markdown_members = markdown_member_names(archive)
        if not markdown_members:
            raise RuntimeError("Docling zip response did not contain Markdown")
        markdown_member = sorted(
            markdown_members, key=lambda name: ("/" in name, name)
        )[0]
        with archive.open(markdown_member) as source:
            markdown = source.read().decode("utf-8")
        atomic_write_text(output_path, markdown)
        for member in archive.namelist():
            if member.endswith("/"):
                continue
            if not member.startswith("artifacts/") and "/artifacts/" not in member:
                continue
            target = artifacts_dir / Path(member).name
            with archive.open(member) as source, target.open("wb") as file:
                shutil.copyfileobj(source, file)
    LOGGER.info("Docling Markdown と artifacts を展開しました output=%s", output_path)


def convert_to_markdown(
    input_path: Path, output_path: Path, *, docling_timeout: int, force_async: bool
) -> None:
    """入力文書を Docling Serve で Markdown へ変換する。

    Args:
        input_path: 変換対象ファイル。
        output_path: Markdown 出力先。
        docling_timeout: Docling 側の文書処理 timeout 秒数。
        force_async: async endpoint を使うかどうか。

    Returns:
        なし。

    Side Effects:
        Docling Serve へ HTTP request を送り、Markdown と artifacts を保存する。
    """

    if not input_path.exists():
        raise FileNotFoundError(f"input document not found: {input_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_zip = output_path.with_suffix(output_path.suffix + ".docling.zip")
    try:
        if force_async:
            post_convert_async(input_path, temp_zip, docling_timeout=docling_timeout)
        else:
            post_convert_sync(input_path, temp_zip, docling_timeout=docling_timeout)
        extract_zip_result(temp_zip, output_path)
    finally:
        temp_zip.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    """CLI 引数 parser を作る。

    Returns:
        argparse.ArgumentParser。
    """

    parser = argparse.ArgumentParser(
        description="Convert a document to Markdown with Docling Serve"
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--docling-timeout", type=int, default=21600)
    parser.add_argument("--async", dest="force_async", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--env", default=".env")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 引数を読み、Docling Serve による Markdown 変換を実行する。

    Args:
        argv: コマンドライン引数。None の場合は sys.argv を使う。

    Returns:
        プロセス終了コード。
    """

    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    load_dotenv(args.env)
    output = Path(args.output)
    if output.exists() and not args.force:
        LOGGER.info("既存出力を再利用します output=%s", output)
        return 0
    try:
        convert_to_markdown(
            Path(args.input),
            output,
            docling_timeout=args.docling_timeout,
            force_async=args.force_async,
        )
        log_jsonl(
            output.parent / "logs" / "run.jsonl",
            {
                "event": "docling_markdown",
                "input": str(Path(args.input)),
                "output": str(output),
                "async": args.force_async,
            },
        )
    except KeyboardInterrupt:
        LOGGER.error("Docling Markdown 変換を中断しました")
        return 130
    except Exception as exc:
        LOGGER.error("%s", exc)
        return 1
    LOGGER.info("Docling Markdown 変換が完了しました output=%s", output)
    return 0


if __name__ == "__main__":
    started_at = perf_counter()
    try:
        exit_code = main()
    finally:
        LOGGER.info("処理時間 %.3f 秒", perf_counter() - started_at)
    sys.exit(exit_code)
