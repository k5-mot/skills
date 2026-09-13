"""Docling ServeへPDF変換を依頼してJSONとassetを保存する。"""

from __future__ import annotations

import json
import time
import zipfile
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from src.adapters.llm import retry_call
from src.state import atomic_write_json


def _headers(api_key: str | None) -> dict[str, str]:
    """任意のDocling API keyをHTTP headerへ変換する。

    Args:
        api_key: Docling API key。

    Returns:
        keyがある場合だけ`X-Api-Key`を持つheader。
    """

    return {"X-Api-Key": api_key} if api_key else {}


def _payload() -> dict[str, str]:
    """翻訳に必要なDocling変換optionを返す。

    Returns:
        multipart form fields。
    """

    return {
        "to_formats": "json",
        "do_ocr": "false",
        "do_table_structure": "true",
        "table_mode": "accurate",
        "do_code_enrichment": "true",
        "do_formula_enrichment": "true",
        "include_images": "true",
        "include_page_images": "false",
        "image_export_mode": "referenced",
        "target_type": "zip",
    }


def _extract_result(payload: bytes, artifacts: Path) -> dict[str, Any]:
    """Docling ZIPから単一JSONと安全なassetを取り出す。

    Args:
        payload: Docling result ZIP。
        artifacts: asset保存directory。

    Returns:
        Docling JSON object。

    Raises:
        ValueError: ZIP内JSON件数または内容が不正な場合。
    """

    with zipfile.ZipFile(BytesIO(payload)) as archive:
        json_names = [name for name in archive.namelist() if name.endswith(".json")]
        if len(json_names) != 1:
            raise ValueError("Docling result must contain exactly one JSON file")
        value = json.loads(archive.read(json_names[0]))
        if not isinstance(value, dict):
            raise ValueError("Docling JSON must be an object")
        artifacts.mkdir(parents=True, exist_ok=True)
        for name in archive.namelist():
            parts = PurePosixPath(name).parts
            if name.endswith("/") or "artifacts" not in parts or ".." in parts:
                continue
            relative = parts[parts.index("artifacts") + 1 :]
            if relative:
                target = artifacts.joinpath(*relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
    return value


def convert_pdf(
    source: Path,
    output_json: Path,
    artifacts: Path,
    base_url: str,
    api_key: str | None = None,
    poll_interval: float = 1.0,
) -> dict[str, Any]:
    """PDFをDoclingの非同期APIでJSONへ変換する。

    Args:
        source: 入力PDF。
        output_json: raw JSON保存先。
        artifacts: 画像等の保存先。
        base_url: Docling Serve URL。
        api_key: 任意API key。
        poll_interval: status確認間隔。

    Returns:
        保存したDocling JSON object。

    Raises:
        RuntimeError: task失敗またはresponse契約違反の場合。
        TimeoutError: 6時間以内に完了しない場合。
    """

    url = base_url.rstrip("/")

    def submit() -> httpx.Response:
        """PDF streamを開き直してtaskを登録する。

        Returns:
            Docling submit response。
        """

        with source.open("rb") as stream:
            response = httpx.post(
                f"{url}/v1/convert/file/async",
                headers=_headers(api_key),
                files={"files": (source.name, stream, "application/pdf")},
                data=_payload(),
                timeout=300,
            )
            response.raise_for_status()
            return response

    submitted = retry_call(submit).json()
    task_id = submitted.get("task_id") or submitted.get("id")
    if not task_id:
        raise RuntimeError("Docling response has no task_id")
    deadline = time.monotonic() + 21_600
    while time.monotonic() < deadline:

        def poll() -> httpx.Response:
            """Docling task状態を一度取得する。

            Returns:
                成功statusのHTTP response。
            """

            response = httpx.get(
                f"{url}/v1/status/poll/{task_id}",
                headers=_headers(api_key),
                timeout=60,
            )
            response.raise_for_status()
            return response

        status_response = retry_call(poll)
        status_payload = status_response.json()
        status = str(
            status_payload.get("task_status") or status_payload.get("status", "")
        ).casefold()
        if status in {"success", "succeeded", "completed"}:

            def download() -> httpx.Response:
                """完了したDocling resultを一度取得する。

                Returns:
                    成功statusのZIP response。
                """

                response = httpx.get(
                    f"{url}/v1/result/{task_id}",
                    headers=_headers(api_key),
                    timeout=300,
                )
                response.raise_for_status()
                return response

            result = retry_call(download)
            document = _extract_result(result.content, artifacts)
            atomic_write_json(output_json, document)
            return document
        if status in {"failure", "failed", "error"}:
            raise RuntimeError(f"Docling task failed: {task_id}")
        time.sleep(poll_interval)
    raise TimeoutError(f"Docling task timed out: {task_id}")
