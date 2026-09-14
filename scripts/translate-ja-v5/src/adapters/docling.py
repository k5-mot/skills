"""Docling ServeへPDF変換を依頼してJSONとassetを保存する。"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import zipfile
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
import pypdfium2 as pdfium

from src.adapters.llm import retry_call
from src.state import atomic_write_json

COLLECTIONS = ("texts", "tables", "pictures", "key_value_items", "form_items", "groups")
DOCLING_CHUNK_PAGES = 10


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


def _remap(value: Any, offsets: dict[str, int], page_offset: int, subdir: str) -> Any:
    """分割Docling文書の参照、ページ番号、asset URIを全体座標へ直す。

    Args:
        value: 再帰変換するDocling値。
        offsets: collectionごとのindex加算値。
        page_offset: ページ番号加算値。
        subdir: assetを隔離するchunk directory名。

    Returns:
        全体文書用に再採番した値。
    """

    if isinstance(value, list):
        return [_remap(item, offsets, page_offset, subdir) for item in value]
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"self_ref", "$ref"} and isinstance(item, str):
            parts = item.removeprefix("#/").split("/")
            if len(parts) == 2 and parts[0] in offsets and parts[1].isdigit():
                item = f"#/{parts[0]}/{int(parts[1]) + offsets[parts[0]]}"
        elif key == "page_no" and isinstance(item, int):
            item += page_offset
        elif key == "uri" and isinstance(item, str) and item.startswith("artifacts/"):
            item = PurePosixPath("artifacts", subdir, item[10:]).as_posix()
        result[key] = _remap(item, offsets, page_offset, subdir)
    return result


def _merge_chunks(chunks: list[dict[str, Any]], source: Path) -> dict[str, Any]:
    """ページ順のDocling文書を参照整合性を保って一文書へ連結する。

    Args:
        chunks: 分割PDFから得たDocling文書。
        source: 元PDF。

    Returns:
        全ページを持つDocling文書。

    Raises:
        ValueError: 変換結果が空の場合。
    """

    merged: dict[str, Any] | None = None
    page_offset = 0
    for number, chunk in enumerate(chunks, 1):
        offsets = {name: len((merged or {}).get(name, [])) for name in COLLECTIONS}
        mapped = _remap(chunk, offsets, page_offset, f"chunk_{number:06d}")
        pages = mapped.get("pages", {})
        mapped["pages"] = {
            str(int(key) + page_offset): value for key, value in pages.items()
        }
        if merged is None:
            merged = mapped
        else:
            for name in COLLECTIONS:
                merged.setdefault(name, []).extend(mapped.get(name, []))
            merged.setdefault("pages", {}).update(mapped["pages"])
            for tree in ("body", "furniture"):
                merged.setdefault(tree, {}).setdefault("children", []).extend(
                    mapped.get(tree, {}).get("children", [])
                )
        page_offset += len(pages)
    if merged is None:
        raise ValueError("PDF produced no Docling chunks")
    merged["name"] = source.stem
    merged.setdefault("origin", {}).update(
        {"filename": source.name, "mimetype": "application/pdf"}
    )
    return merged


def _convert_one(
    source: Path,
    artifacts: Path,
    base_url: str,
    api_key: str | None = None,
    poll_interval: float = 1.0,
) -> dict[str, Any]:
    """一つのPDFをDoclingの非同期APIでJSONへ変換する。

    Args:
        source: 入力PDF。
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
    last_status = ""
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
        if status and status != last_status:
            print(f"Docling: {status}", flush=True)
            last_status = status
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
            return _extract_result(result.content, artifacts)
        if status in {"failure", "failed", "error"}:
            raise RuntimeError(f"Docling task failed: {task_id}")
        time.sleep(poll_interval)
    raise TimeoutError(f"Docling task timed out: {task_id}")


def convert_pdf(
    source: Path,
    output_json: Path,
    artifacts: Path,
    base_url: str,
    api_key: str | None = None,
    poll_interval: float = 1.0,
    chunk_pages: int = DOCLING_CHUNK_PAGES,
) -> dict[str, Any]:
    """PDFを小分けにDocling変換し、単一JSONとして保存する。

    Args:
        source: 入力PDF。
        output_json: raw JSON保存先。
        artifacts: 画像等の保存先。
        base_url: Docling Serve URL。
        api_key: 任意API key。
        poll_interval: status確認間隔。
        chunk_pages: Doclingへ一度に送る最大ページ数。

    Returns:
        保存したDocling JSON object。

    Raises:
        ValueError: ページ分割上限が不正な場合。
    """

    if chunk_pages < 1:
        raise ValueError("Docling chunk_pages must be positive")
    with pdfium.PdfDocument(source) as pdf:
        page_count = len(pdf)
        if page_count <= chunk_pages:
            document = _convert_one(
                source, artifacts, base_url, api_key, poll_interval
            )
            atomic_write_json(output_json, document)
            return document
        artifacts.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".docling-", dir=artifacts.parent
        ) as temporary:
            root = Path(temporary)
            documents: list[dict[str, Any]] = []
            total = (page_count + chunk_pages - 1) // chunk_pages
            for number, start in enumerate(range(0, page_count, chunk_pages), 1):
                indexes = list(range(start, min(start + chunk_pages, page_count)))
                chunk = root / f"chunk-{number:06d}.pdf"
                with pdfium.PdfDocument.new() as destination:
                    destination.import_pages(pdf, pages=indexes)
                    destination.save(chunk)
                first, last = indexes[0] + 1, indexes[-1] + 1
                print(
                    f"Docling: chunk {number}/{total} pages {first}-{last} started",
                    flush=True,
                )
                documents.append(
                    _convert_one(
                        chunk,
                        root / "artifacts" / f"chunk_{number:06d}",
                        base_url,
                        api_key,
                        poll_interval,
                    )
                )
                print(f"Docling: chunk {number}/{total} success", flush=True)
            document = _merge_chunks(documents, source)
            staged_artifacts = root / "artifacts"
            if artifacts.exists():
                shutil.rmtree(artifacts)
            if staged_artifacts.exists():
                shutil.move(staged_artifacts, artifacts)
            atomic_write_json(output_json, document)
            return document
