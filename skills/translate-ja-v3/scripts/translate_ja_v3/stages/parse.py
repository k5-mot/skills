"""ParseStageをDocling Serveとpypdfium2で実装する。"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import zipfile
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
import pypdfium2 as pdfium

from ..config import PipelineState, state_options, state_paths
from ..io import (
    LOGGER,
    hash_file,
    hash_json,
    read_json,
    record_stage,
    stage_cached,
    write_bytes,
    write_json,
)

COLLECTIONS = ("groups", "texts", "pictures", "tables", "key_value_items", "form_items")
CHUNK_PAGES = 10
PAGE_SCALE = 1.0


def _settings() -> tuple[str, str]:
    """Docling ServeのURLとAPI keyを返す。

    Returns:
        末尾slashを除いたURLとAPI key。

    Raises:
        RuntimeError: 必須設定が不足する場合。
    """

    url = os.getenv("DOCLING_SERVER_URL") or os.getenv("DOCLING_SERVE_URL")
    key = os.getenv("DOCLING_API_KEY") or os.getenv("DOCLING_SERVE_API_KEY")
    if not url or not key:
        raise RuntimeError("DOCLING_SERVER_URL and DOCLING_API_KEY are required")
    return url.rstrip("/"), key


def _payload() -> dict[str, str | list[str]]:
    """OOMを抑えるDocling multipart設定を返す。

    Returns:
        Docling Serveへ渡すform fields。
    """

    return {
        "to_formats": "json",
        "do_ocr": "false",
        "force_ocr": "false",
        "ocr_lang": ["jpn", "jpn_vert", "eng"],
        "do_table_structure": "true",
        "table_mode": "accurate",
        "table_cell_matching": "true",
        "do_code_enrichment": "true",
        "do_formula_enrichment": "true",
        "document_timeout": "21600",
        "include_images": "true",
        "include_page_images": "false",
        "images_scale": "1.0",
        "image_export_mode": "referenced",
        "target_type": "zip",
    }


def _convert_one(source: Path, output_json: Path, artifacts: Path) -> None:
    """1ファイルをDocling ServeでJSONとartifactsへ変換する。

    Args:
        source: 変換するPDFまたはWordファイル。
        output_json: JSON保存先。
        artifacts: artifact保存先。

    Returns:
        なし。
    """

    url, key = _settings()
    headers = {"X-Api-Key": key}
    response: httpx.Response | None = None
    for field in ("files", "file"):
        with source.open("rb") as stream:
            response = httpx.post(
                f"{url}/v1/convert/file/async",
                headers=headers,
                files={field: (source.name, stream)},
                data=_payload(),
                timeout=120,
            )
        if response.status_code not in {400, 422}:
            break
    if response is None or response.is_error:
        status = response.status_code if response else "unknown"
        raise RuntimeError(f"Docling conversion request failed status={status}")
    task_id = response.json().get("task_id") or response.json().get("id")
    if not task_id:
        raise RuntimeError("Docling conversion response has no task_id")
    deadline = time.monotonic() + 21_600
    while time.monotonic() < deadline:
        status_response = httpx.get(
            f"{url}/v1/status/poll/{task_id}", headers=headers, timeout=60
        )
        status_response.raise_for_status()
        payload = status_response.json()
        status = str(payload.get("status") or payload.get("task_status") or "").lower()
        LOGGER.debug("Polled Docling task task_id=%s status=%s", task_id, status)
        if status in {"success", "succeeded", "completed"}:
            result = httpx.get(
                f"{url}/v1/result/{task_id}", headers=headers, timeout=21_600
            )
            result.raise_for_status()
            _extract_zip(result.content, output_json, artifacts)
            return
        if status in {"failure", "failed", "error"}:
            raise RuntimeError(f"Docling task failed task_id={task_id}")
        time.sleep(10)
    raise TimeoutError(f"Docling task timed out task_id={task_id}")


def _extract_zip(payload: bytes, output_json: Path, artifacts: Path) -> None:
    """Docling ZIPから単一JSONとartifactを展開する。

    Args:
        payload: Docling response ZIP。
        output_json: JSON保存先。
        artifacts: artifact保存先。

    Returns:
        なし。

    Raises:
        ValueError: JSONがちょうど1件でない場合。
    """

    artifacts.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        json_files = [name for name in archive.namelist() if name.endswith(".json")]
        if len(json_files) != 1:
            raise ValueError("Docling ZIP must contain exactly one JSON file")
        write_bytes(output_json, archive.read(json_files[0]))
        for name in archive.namelist():
            parts = PurePosixPath(name).parts
            if name.endswith("/") or "artifacts" not in parts or ".." in parts:
                continue
            relative = parts[parts.index("artifacts") + 1 :]
            if relative:
                write_bytes(artifacts.joinpath(*relative), archive.read(name))


def _remap(value: Any, offsets: dict[str, int], page_offset: int, subdir: str) -> Any:
    """チャンクJSON内のref、ページ、artifact URIを全体座標へ直す。

    Args:
        value: 再帰変換するJSON値。
        offsets: collectionごとのindex加算値。
        page_offset: ページ番号加算値。
        subdir: artifactのchunk subdirectory。

    Returns:
        再採番済みJSON値。
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


def _merge(chunks: list[dict[str, Any]], source: Path) -> dict[str, Any]:
    """Docling chunk JSONをcollectionとtree単位で連結する。

    Args:
        chunks: ページ順のDocling JSON。
        source: 元PDF。

    Returns:
        全ページを含むDocling JSON。
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


def _render_pages(source: Path, document: dict[str, Any], artifacts: Path) -> None:
    """PDFページをローカルPNG化してDocling URIを更新する。

    Args:
        source: 元PDF。
        document: 更新するDocling JSON。
        artifacts: PNG保存先。

    Returns:
        なし。
    """

    pages = document.setdefault("pages", {})
    with pdfium.PdfDocument(source) as pdf:
        for index in range(len(pdf)):
            page_no = index + 1
            page = pdf[index]
            bitmap = page.render(scale=PAGE_SCALE)
            image = bitmap.to_pil()
            name = f"page_{page_no:06d}.png"
            with BytesIO() as buffer:
                image.save(buffer, format="PNG")
                write_bytes(artifacts / name, buffer.getvalue())
            image.close()
            bitmap.close()
            page.close()
            pages.setdefault(str(page_no), {})["image"] = {
                "mimetype": "image/png",
                "dpi": 72,
                "uri": f"artifacts/{name}",
            }


def _convert_pdf(source: Path, output_json: Path, artifacts: Path) -> None:
    """PDFを10ページずつ変換し、JSONとページ画像を連結する。

    Args:
        source: 元PDF。
        output_json: 結果JSON。
        artifacts: 結果artifact directory。

    Returns:
        なし。
    """

    with tempfile.TemporaryDirectory(prefix="translate-ja-v3-") as temporary:
        root = Path(temporary)
        chunks: list[dict[str, Any]] = []
        with pdfium.PdfDocument(source) as pdf:
            for number, start in enumerate(range(0, len(pdf), CHUNK_PAGES), 1):
                indexes = list(range(start, min(start + CHUNK_PAGES, len(pdf))))
                chunk_pdf = root / f"chunk-{number}.pdf"
                chunk_json = root / f"chunk-{number}.json"
                chunk_artifacts = root / "artifacts" / f"chunk_{number:06d}"
                with pdfium.PdfDocument.new() as destination:
                    destination.import_pages(pdf, pages=indexes)
                    destination.save(chunk_pdf)
                LOGGER.info(
                    "Started ParseStage chunk=%s pages=%s", number, len(indexes)
                )
                _convert_one(chunk_pdf, chunk_json, chunk_artifacts)
                chunks.append(read_json(chunk_json))
        document = _merge(chunks, source)
        if artifacts.exists():
            shutil.rmtree(artifacts)
        source_artifacts = root / "artifacts"
        if source_artifacts.exists():
            shutil.copytree(source_artifacts, artifacts)
        else:
            artifacts.mkdir(parents=True)
        _render_pages(source, document, artifacts)
        write_json(output_json, document)


def parse_stage(state: PipelineState) -> PipelineState:
    """入力をDocling JSONへ変換するLangGraph node。

    Args:
        state: PipelineOptionsとStagePathsを含むgraph state。

    Returns:
        Parse成果物パスを設定した部分state。
    """

    options, paths = state_options(state), state_paths(state)
    source = options.input.resolve()
    input_hash = hash_file(source)
    config_hash = hash_json(
        {"version": 1, "payload": _payload(), "chunk_pages": CHUNK_PAGES}
    )
    cached = stage_cached(
        paths.manifest, "parse", input_hash, config_hash, paths.document_json
    )
    if not options.force and cached and paths.artifacts.is_dir():
        LOGGER.info("Resumed ParseStage output=%s", paths.document_json)
        return {"current_path": str(paths.document_json), "completed_stage": "parse"}
    record_stage(
        paths.manifest, "parse", "running", input_hash, config_hash, paths.document_json
    )
    if source.suffix.lower() == ".pdf":
        _convert_pdf(source, paths.document_json, paths.artifacts)
    else:
        if paths.artifacts.exists():
            shutil.rmtree(paths.artifacts)
        _convert_one(source, paths.document_json, paths.artifacts)
    record_stage(
        paths.manifest,
        "parse",
        "completed",
        input_hash,
        config_hash,
        paths.document_json,
    )
    return {"current_path": str(paths.document_json), "completed_stage": "parse"}
