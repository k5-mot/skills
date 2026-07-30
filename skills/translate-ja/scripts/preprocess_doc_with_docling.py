"""Docling Serve v1 API で入力文書を Docling schema JSON へ変換する。"""

from __future__ import annotations
import requests
import argparse
import json
import logging
import shutil
import socket
import time
import zipfile
from pathlib import Path
from time import perf_counter
from typing import Any

from config import load_dotenv, require_docling_settings
from io_utils import configure_logging, ensure_dir

LOGGER = logging.getLogger("translate-ja.preprocess_doc_with_docling")


def _format_connection_error(endpoint: str, exc: Exception) -> str:
    """Docling 接続エラーを運用者向けの短い説明へ整形する。"""

    return (
        f"Docling Serve に接続できません endpoint={endpoint}。"
        " DOCLING_SERVER_URL が到達可能なホストを指しているか、Docling Serve が起動しているかを確認してください。"
        f" detail={exc}"
    )


def _docling_options(document_timeout: int) -> dict[str, Any]:
    """Docling Serve に渡す推奨変換オプションを返す。"""

    return {
        "to_formats": ["json"],
        "do_ocr": True,
        "force_ocr": False,
        "ocr_preset": "tesseract",
        "ocr_lang": ["jpn", "jpn_vert", "eng"],
        "document_timeout": document_timeout,
        "do_picture_description": False,
        "include_images": True,
        "include_page_images": True,
        "image_export_mode": "referenced",
    }


def _docling_form_payload(document_timeout: int) -> list[tuple[str, str]]:
    """Docling Serve v1 の multipart form field を作る。"""

    return [
        ("to_formats", "json"),
        ("do_ocr", "true"),
        ("force_ocr", "false"),
        ("ocr_preset", "tesseract"),
        ("ocr_lang", "jpn"),
        ("ocr_lang", "jpn_vert"),
        ("ocr_lang", "eng"),
        ("document_timeout", str(document_timeout)),
        ("do_picture_description", "false"),
        ("include_images", "true"),
        ("include_page_images", "true"),
        ("image_export_mode", "referenced"),
        ("target_type", "zip"),
    ]


def _legacy_docling_form_payload(document_timeout: int) -> dict[str, str]:
    """古い Docling Serve 互換の JSON options form field を作る。"""

    options = _docling_options(document_timeout)
    return {
        "options": json.dumps(options),
        "to_formats": "json",
        "image_export_mode": "referenced",
        "target_type": "zip",
        "document_timeout": str(document_timeout),
    }


def _response_mentions_missing_files(response: Any) -> bool:
    """422 応答が files field 不足を示しているかを判定する。"""

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


def _request_convert(endpoint: str, input_path: Path, *, docling_timeout: int, request_timeout: int) -> Any:
    """Docling Serve へ multipart request を送り、フィールド名差分を吸収する。"""

    settings = require_docling_settings(timeout_seconds=docling_timeout)
    data_variants: list[Any] = [
        _docling_form_payload(docling_timeout),
        _legacy_docling_form_payload(docling_timeout),
    ]
    last_response = None
    for file_field in ("files", "file"):
        for data in data_variants:
            with input_path.open("rb") as file:
                files = {file_field: (input_path.name, file)}
                try:
                    response = requests.post(
                        endpoint,
                        headers={"X-Api-Key": settings.api_key},
                        files=files,
                        data=data,
                        timeout=request_timeout,
                    )
                except requests.exceptions.RequestException as exc:
                    raise RuntimeError(
                        _format_connection_error(endpoint, exc)) from exc
                except socket.gaierror as exc:
                    raise RuntimeError(
                        _format_connection_error(endpoint, exc)) from exc
            last_response = response
            if response.status_code not in {400, 422}:
                return response
            if file_field == "files" and not _response_mentions_missing_files(response):
                break
    return last_response


def _post_convert_sync(input_path: Path, output_zip: Path, *, docling_timeout: int) -> None:
    """Docling Serve の同期変換 endpoint を呼び出し、応答を保存する。"""

    settings = require_docling_settings(timeout_seconds=docling_timeout)
    endpoint = f"{settings.server_url}/v1/convert/file"
    response = _request_convert(
        endpoint, input_path, docling_timeout=docling_timeout, request_timeout=settings.timeout_seconds)
    if response.status_code >= 400:
        raise RuntimeError(
            f"Docling sync convert failed status={response.status_code} body={response.text[:500]}")
    output_zip.write_bytes(response.content)


def _poll_async(task_id: str, output_zip: Path, *, docling_timeout: int) -> None:
    """Docling Serve async task を poll して結果 zip を保存する。"""

    settings = require_docling_settings(timeout_seconds=docling_timeout)
    deadline = time.monotonic() + settings.timeout_seconds
    while time.monotonic() < deadline:
        status_response = requests.get(
            f"{settings.server_url}/v1/status/poll/{task_id}",
            headers={"X-Api-Key": settings.api_key},
            timeout=60,
        )
        if status_response.status_code >= 400:
            raise RuntimeError(
                f"Docling status poll failed status={status_response.status_code}")
        status_data = status_response.json()
        status = str(status_data.get("status")
                     or status_data.get("task_status") or "").lower()
        if status in {"success", "succeeded", "completed"}:
            result_response = requests.get(
                f"{settings.server_url}/v1/result/{task_id}",
                headers={"X-Api-Key": settings.api_key},
                timeout=settings.timeout_seconds,
            )
            if result_response.status_code >= 400:
                raise RuntimeError(
                    f"Docling result failed status={result_response.status_code}")
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


def _post_convert_async(input_path: Path, output_zip: Path, *, docling_timeout: int) -> None:
    """Docling Serve の async endpoint を呼び出す。"""

    settings = require_docling_settings(timeout_seconds=docling_timeout)
    endpoint = f"{settings.server_url}/v1/convert/file/async"
    response = _request_convert(
        endpoint, input_path, docling_timeout=docling_timeout, request_timeout=120)
    if response.status_code >= 400:
        raise RuntimeError(
            f"Docling async convert failed status={response.status_code} body={response.text[:500]}")
    payload = response.json()
    task_id = payload.get("task_id") or payload.get("id")
    if not task_id:
        raise RuntimeError("Docling async response has no task_id")
    _poll_async(str(task_id), output_zip, docling_timeout=docling_timeout)


def _extract_zip_result(zip_path: Path, output_path: Path) -> None:
    """Docling zipball から JSON と artifacts を出力ディレクトリへ展開する。"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    artifacts_dir = ensure_dir(output_path.parent / "artifacts")
    with zipfile.ZipFile(zip_path, "r") as archive:
        json_members = [name for name in archive.namelist(
        ) if name.lower().endswith(".json") and not name.endswith("/")]
        if not json_members:
            raise RuntimeError("Docling zip response did not contain JSON")
        json_member = sorted(
            json_members, key=lambda name: ("/" in name, name))[0]
        with archive.open(json_member) as source, output_path.open("wb") as target:
            shutil.copyfileobj(source, target)
        for member in archive.namelist():
            if not member.startswith("artifacts/") or member.endswith("/"):
                continue
            target = output_path.parent / member
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as file:
                shutil.copyfileobj(source, file)
    LOGGER.info("Docling artifacts を展開しました artifacts=%s", artifacts_dir)


def preprocess(input_path: Path, output_path: Path, *, docling_timeout: int, force_async: bool) -> None:
    """入力文書を Docling schema JSON へ変換する。"""

    if not input_path.exists():
        raise FileNotFoundError(f"input document not found: {input_path}")
    temp_zip = output_path.with_suffix(output_path.suffix + ".docling.zip")
    try:
        if force_async:
            _post_convert_async(input_path, temp_zip,
                                docling_timeout=docling_timeout)
        else:
            _post_convert_sync(input_path, temp_zip,
                               docling_timeout=docling_timeout)
        _extract_zip_result(temp_zip, output_path)
    finally:
        temp_zip.unlink(missing_ok=True)


def main() -> int:
    """CLI 引数を読み、Docling 前処理を実行する。"""

    configure_logging()
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Preprocess document with Docling Serve")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--docling-timeout", type=int, default=21600)
    parser.add_argument("--async", dest="force_async", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    output = Path(args.output)
    if output.exists() and not args.force:
        LOGGER.info("既存出力を再利用します output=%s", output)
        return 0
    try:
        preprocess(Path(args.input), output,
                   docling_timeout=args.docling_timeout, force_async=args.force_async)
    except KeyboardInterrupt:
        LOGGER.error("Docling 前処理を中断しました")
        return 130
    except Exception as exc:
        LOGGER.error("%s", exc)
        return 1
    LOGGER.info("Docling 前処理が完了しました output=%s", output)
    return 0


if __name__ == "__main__":
    started_at = perf_counter()
    try:
        exit_code = main()
    finally:
        LOGGER.info("処理時間 %.3f 秒", perf_counter() - started_at)
    raise SystemExit(exit_code)
