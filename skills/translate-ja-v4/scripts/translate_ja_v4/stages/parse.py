"""ParseStageをDocling Serveとpypdfium2で実装する。"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import zipfile
from io import BytesIO
from math import hypot
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from uuid import uuid4

import httpx
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from ..config import PipelineOptions, PipelineState, state_options, state_paths
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
PAGE_SCALE = 1.0


class DoclingValidationError(ValueError):
    """Docling JSONまたはartifactの契約違反を表す。"""


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


def _request_with_retry(
    request: Callable[[], httpx.Response], options: PipelineOptions, label: str
) -> httpx.Response:
    """一時的なHTTP障害だけをPipelineOptionsに従って再試行する。

    Args:
        request: 1回のHTTP requestを行う関数。
        options: timeoutとretry間隔を含むPipelineOptions。
        label: logに出すrequest種別。

    Returns:
        最後に得られたHTTP response。

    Raises:
        httpx.TransportError: 最終試行でも通信できなかった場合。
    """

    retryable = {408, 409, 429, 500, 502, 503, 504}
    attempts = options.max_retries + 1
    for attempt in range(1, attempts + 1):
        try:
            response = request()
            if response.status_code not in retryable or attempt == attempts:
                return response
        except httpx.TransportError:
            if attempt == attempts:
                raise
        delay = min(
            options.retry_max_seconds,
            options.retry_initial_seconds * (2 ** (attempt - 1)),
        )
        LOGGER.warning(
            "Docling request failed; retrying request=%s attempt=%s/%s delay=%.1fs",
            label,
            attempt,
            attempts,
            delay,
        )
        time.sleep(delay)
    raise RuntimeError("Docling retry loop ended unexpectedly")


def _convert_one(
    source: Path,
    output_json: Path,
    artifacts: Path,
    options: PipelineOptions,
) -> None:
    """1ファイルをDocling ServeでJSONとartifactsへ変換する。

    Args:
        source: 変換するPDFまたはWordファイル。
        output_json: JSON保存先。
        artifacts: artifact保存先。
        options: request timeoutとretry設定。

    Returns:
        なし。
    """

    url, key = _settings()
    headers = {"X-Api-Key": key}
    response: httpx.Response | None = None
    for field in ("files", "file"):

        def submit() -> httpx.Response:
            """変換対象fileを開き直してDoclingへ送る。

            Returns:
                DoclingのHTTP response。
            """

            with source.open("rb") as stream:
                return httpx.post(
                    f"{url}/v1/convert/file/async",
                    headers=headers,
                    files={field: (source.name, stream)},
                    data=_payload(),
                    timeout=options.request_timeout_seconds,
                )

        response = _request_with_retry(submit, options, "submit")
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
        status_response = _request_with_retry(
            lambda: httpx.get(
                f"{url}/v1/status/poll/{task_id}",
                headers=headers,
                timeout=min(options.request_timeout_seconds, 60),
            ),
            options,
            "poll",
        )
        status_response.raise_for_status()
        payload = status_response.json()
        status = str(payload.get("status") or payload.get("task_status") or "").lower()
        LOGGER.debug("Polled Docling task task_id=%s status=%s", task_id, status)
        if status in {"success", "succeeded", "completed"}:
            result = _request_with_retry(
                lambda: httpx.get(
                    f"{url}/v1/result/{task_id}",
                    headers=headers,
                    timeout=options.request_timeout_seconds,
                ),
                options,
                "result",
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


def _resolve_pointer(document: dict[str, Any], ref: str) -> bool:
    """JSON pointerが文書内の値へ解決できるか判定する。

    Args:
        document: 検証対象Docling JSON。
        ref: `#/` で始まるJSON pointer。

    Returns:
        pointerが解決できればTrue。
    """

    value: Any = document
    try:
        for part in ref.removeprefix("#/").split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            value = value[int(key)] if isinstance(value, list) else value[key]
    except (KeyError, IndexError, TypeError, ValueError):
        return False
    return True


def _validate_document(
    document: dict[str, Any], expected_pages: int | None = None
) -> str:
    """対応可能なDocling schemaと参照整合性を検証する。

    Args:
        document: Docling Serveから得たJSON object。
        expected_pages: 入力PDFの期待ページ数。Noneなら件数を検証しない。

    Returns:
        検証済みschema名とversion。

    Raises:
        DoclingValidationError: 必須field、型、ページ、参照が不正な場合。
    """

    schema = document.get("schema_name")
    version = document.get("version")
    if schema != "DoclingDocument" or not isinstance(version, str) or not version:
        raise DoclingValidationError(
            f"unsupported Docling schema schema={schema!r} version={version!r}"
        )
    for key in COLLECTIONS:
        if not isinstance(document.get(key), list):
            raise DoclingValidationError(f"Docling collection must be a list: {key}")
    for key in ("body", "furniture"):
        tree = document.get(key)
        if not isinstance(tree, dict) or not isinstance(tree.get("children"), list):
            raise DoclingValidationError(f"Docling tree is invalid: {key}")
    pages = document.get("pages")
    if not isinstance(pages, dict) or any(not str(key).isdigit() for key in pages):
        raise DoclingValidationError("Docling pages must use numeric object keys")
    page_numbers = sorted(int(key) for key in pages)
    if page_numbers != list(range(1, len(page_numbers) + 1)):
        raise DoclingValidationError("Docling page numbers must be consecutive from 1")
    if expected_pages is not None and len(pages) != expected_pages:
        raise DoclingValidationError(
            f"Docling page count mismatch expected={expected_pages} actual={len(pages)}"
        )
    for collection in COLLECTIONS:
        for index, item in enumerate(document[collection]):
            if not isinstance(item, dict):
                raise DoclingValidationError(
                    f"Docling collection item must be an object: {collection}/{index}"
                )
            expected_ref = f"#/{collection}/{index}"
            if item.get("self_ref") != expected_ref:
                raise DoclingValidationError(
                    f"Docling self_ref mismatch expected={expected_ref}"
                )
            prov = item.get("prov", [])
            for entry in prov if isinstance(prov, list) else []:
                page = entry.get("page_no") if isinstance(entry, dict) else None
                if page is not None and str(page) not in pages:
                    raise DoclingValidationError(
                        f"Docling provenance references missing page: {page}"
                    )
    for index, table in enumerate(document["tables"]):
        data = table.get("data")
        if not isinstance(data, dict) or not any(
            isinstance(data.get(key), list) for key in ("grid", "table_cells", "cells")
        ):
            raise DoclingValidationError(f"unsupported Docling table schema: {index}")

    def validate_refs(value: Any) -> None:
        """文書内の全`$ref`を再帰検証する。

        Args:
            value: 検証するJSON値。

        Returns:
            なし。

        Raises:
            DoclingValidationError: 解決不能な参照がある場合。
        """

        if isinstance(value, dict):
            ref = value.get("$ref")
            if (
                isinstance(ref, str)
                and ref.startswith("#/")
                and not _resolve_pointer(document, ref)
            ):
                raise DoclingValidationError(f"unresolved Docling reference: {ref}")
            for child in value.values():
                validate_refs(child)
        elif isinstance(value, list):
            for child in value:
                validate_refs(child)

    validate_refs(document)
    return f"{schema}/{version}"


def _convert_validated(
    source: Path,
    output_json: Path,
    artifacts: Path,
    options: PipelineOptions,
    expected_pages: int | None,
) -> dict[str, Any]:
    """Docling変換とschema検証を行い、検証失敗時は再変換する。

    Args:
        source: 変換する入力file。
        output_json: 一時JSON保存先。
        artifacts: 一時artifact directory。
        options: retry設定。
        expected_pages: 入力の期待ページ数。

    Returns:
        検証済みDocling JSON。

    Raises:
        DoclingValidationError: 最終試行でも検証に失敗した場合。
    """

    attempts = options.max_retries + 1
    for attempt in range(1, attempts + 1):
        output_json.unlink(missing_ok=True)
        if artifacts.exists():
            shutil.rmtree(artifacts)
        _convert_one(source, output_json, artifacts, options)
        document = read_json(output_json)
        try:
            schema = _validate_document(document, expected_pages)
            LOGGER.debug("Validated Docling document schema=%s", schema)
            return document
        except DoclingValidationError:
            if attempt == attempts:
                raise
            delay = min(
                options.retry_max_seconds,
                options.retry_initial_seconds * (2 ** (attempt - 1)),
            )
            LOGGER.warning(
                "Docling validation failed; retrying conversion attempt=%s/%s delay=%.1fs",
                attempt,
                attempts,
                delay,
            )
            time.sleep(delay)
    raise RuntimeError("Docling validation retry loop ended unexpectedly")


def _artifact_inventory(path: Path) -> dict[str, Any]:
    """artifact directoryの件数、容量、安定hashを返す。

    Args:
        path: 検査するartifact directory。

    Returns:
        file_count、total_bytes、sha256を持つobject。
    """

    entries = [
        {
            "path": item.relative_to(path).as_posix(),
            "size": item.stat().st_size,
            "sha256": hash_file(item),
        }
        for item in sorted(value for value in path.rglob("*") if value.is_file())
    ]
    return {
        "file_count": len(entries),
        "total_bytes": sum(item["size"] for item in entries),
        "sha256": hash_json(entries),
    }


def _validate_artifacts(document: dict[str, Any], artifacts: Path) -> None:
    """Docling JSON内のartifact URIが安全な実在fileを指すか検証する。

    Args:
        document: artifact URIを含むDocling JSON。
        artifacts: URIの `artifacts/` に対応するdirectory。

    Returns:
        なし。

    Raises:
        DoclingValidationError: 不正URIまたは欠落fileがある場合。
    """

    def visit(value: Any) -> None:
        """JSONを再帰走査してartifact URIを検証する。

        Args:
            value: 検証するJSON値。

        Returns:
            なし。
        """

        if isinstance(value, dict):
            uri = value.get("uri")
            if isinstance(uri, str) and uri.startswith("artifacts/"):
                parts = PurePosixPath(uri).parts
                if ".." in parts or len(parts) < 2:
                    raise DoclingValidationError(f"unsafe artifact URI: {uri}")
                candidate = artifacts.joinpath(*parts[1:])
                if not candidate.is_file():
                    raise DoclingValidationError(f"missing artifact: {uri}")
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(document)


def _commit_parse(
    document: dict[str, Any], output_json: Path, staging: Path, artifacts: Path
) -> dict[str, Any]:
    """検証済みJSONとartifact directoryを一括して公開する。

    Args:
        document: 保存するDocling JSON。
        output_json: 公開JSON path。
        staging: 完成済みartifact staging directory。
        artifacts: 公開artifact directory。

    Returns:
        公開したartifact inventory。

    Side Effects:
        旧artifactをbackupしてstagingと入れ替え、JSONをatomic保存する。
    """

    _validate_artifacts(document, staging)
    inventory = _artifact_inventory(staging)
    backup = artifacts.with_name(f".{artifacts.name}.backup-{uuid4().hex}")
    artifacts.parent.mkdir(parents=True, exist_ok=True)
    if artifacts.exists():
        os.replace(artifacts, backup)
    try:
        os.replace(staging, artifacts)
        write_json(output_json, document)
    except Exception:
        if artifacts.exists():
            shutil.rmtree(artifacts)
        if backup.exists():
            os.replace(backup, artifacts)
        raise
    if backup.exists():
        shutil.rmtree(backup)
    return inventory


def _artifacts_cached(manifest: Path, artifacts: Path) -> bool:
    """manifest記録と現在のartifact directory hashが一致するか判定する。

    Args:
        manifest: ParseStage状態を持つmanifest.json。
        artifacts: 検証するartifact directory。

    Returns:
        件数、容量、directory hashが一致すればTrue。
    """

    if not manifest.is_file() or not artifacts.is_dir():
        return False
    expected = read_json(manifest).get("stages", {}).get("parse", {}).get("artifacts")
    return isinstance(expected, dict) and expected == _artifact_inventory(artifacts)


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
    """PDFページをPNG化し、text spanとDocling URIを更新する。

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
            pages.setdefault(str(page_no), {})["text_spans"] = _extract_spans(page)
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


def _extract_spans(page: Any) -> list[dict[str, Any]]:
    """PDFiumのtext objectをfont情報付きspanへ変換する。

    Args:
        page: pypdfium2のPDF page。

    Returns:
        text、bbox、font、size、weightを持つspan配列。
    """

    spans: list[dict[str, Any]] = []
    text_page = page.get_textpage()
    try:
        objects = page.get_objects(
            filter=[pdfium_c.FPDF_PAGEOBJ_TEXT], textpage=text_page
        )
        for text_object in objects:
            try:
                text = text_object.extract().strip()
                if not text:
                    continue
                left, bottom, right, top = text_object.get_bounds()
                font = text_object.get_font()
                font_name = font.get_base_name()
                matrix = text_object.get_matrix()
                scale = max(hypot(matrix.a, matrix.b), hypot(matrix.c, matrix.d))
                weight = int(font.get_weight())
                if "bold" in font_name.lower():
                    weight = max(weight, 700)
                spans.append(
                    {
                        "id": len(spans),
                        "text": text,
                        "bbox": {
                            "l": float(left),
                            "t": float(top),
                            "r": float(right),
                            "b": float(bottom),
                            "coord_origin": "BOTTOMLEFT",
                        },
                        "font": font_name,
                        "size": round(float(text_object.get_font_size()) * scale, 3),
                        "weight": weight,
                    }
                )
            except Exception as error:
                LOGGER.debug("Skipped unreadable PDF text span error=%s", error)
    finally:
        text_page.close()
    return spans


def _convert_pdf(
    source: Path,
    output_json: Path,
    artifacts: Path,
    options: PipelineOptions,
) -> dict[str, Any]:
    """PDFを10ページずつ変換し、JSONとページ画像を連結する。

    Args:
        source: 元PDF。
        output_json: 結果JSON。
        artifacts: 結果artifact directory。
        options: PDF chunk数、timeout、retry設定。

    Returns:
        公開したartifact inventory。
    """

    artifacts.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{artifacts.name}.staging-", dir=artifacts.parent)
    )
    try:
        with tempfile.TemporaryDirectory(prefix="translate-ja-v4-") as temporary:
            root = Path(temporary)
            chunks: list[dict[str, Any]] = []
            with pdfium.PdfDocument(source) as pdf:
                page_count = len(pdf)
                for number, start in enumerate(
                    range(0, page_count, options.pdf_chunk_pages), 1
                ):
                    indexes = list(
                        range(start, min(start + options.pdf_chunk_pages, page_count))
                    )
                    chunk_pdf = root / f"chunk-{number}.pdf"
                    chunk_json = root / f"chunk-{number}.json"
                    chunk_artifacts = root / "artifacts" / f"chunk_{number:06d}"
                    with pdfium.PdfDocument.new() as destination:
                        destination.import_pages(pdf, pages=indexes)
                        destination.save(chunk_pdf)
                    LOGGER.info(
                        "Started ParseStage chunk=%s pages=%s", number, len(indexes)
                    )
                    chunks.append(
                        _convert_validated(
                            chunk_pdf,
                            chunk_json,
                            chunk_artifacts,
                            options,
                            len(indexes),
                        )
                    )
            document = _merge(chunks, source)
            _validate_document(document, page_count)
            source_artifacts = root / "artifacts"
            if source_artifacts.exists():
                shutil.copytree(source_artifacts, staging, dirs_exist_ok=True)
            _render_pages(source, document, staging)
            return _commit_parse(document, output_json, staging, artifacts)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _convert_document(
    source: Path,
    output_json: Path,
    artifacts: Path,
    options: PipelineOptions,
) -> dict[str, Any]:
    """PDF以外の入力をstaging上で検証して公開する。

    Args:
        source: 変換する入力file。
        output_json: 公開JSON path。
        artifacts: 公開artifact directory。
        options: timeoutとretry設定。

    Returns:
        公開したartifact inventory。
    """

    artifacts.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{artifacts.name}.staging-", dir=artifacts.parent)
    )
    with tempfile.TemporaryDirectory(prefix="translate-ja-v4-") as temporary:
        staged_json = Path(temporary) / "document.json"
        try:
            document = _convert_validated(
                source, staged_json, staging, options, expected_pages=None
            )
            return _commit_parse(document, output_json, staging, artifacts)
        finally:
            if staging.exists():
                shutil.rmtree(staging)


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
        {
            "version": 3,
            "payload": _payload(),
            "chunk_pages": options.pdf_chunk_pages,
            "text_spans": "pypdfium2",
        }
    )
    cached = stage_cached(
        paths.manifest, "parse", input_hash, config_hash, paths.document_json
    )
    if (
        not options.force
        and cached
        and _artifacts_cached(paths.manifest, paths.artifacts)
    ):
        LOGGER.info("Resumed ParseStage output=%s", paths.document_json)
        return {"current_path": str(paths.document_json), "completed_stage": "parse"}
    record_stage(
        paths.manifest, "parse", "running", input_hash, config_hash, paths.document_json
    )
    artifacts = (
        _convert_pdf(source, paths.document_json, paths.artifacts, options)
        if source.suffix.lower() == ".pdf"
        else _convert_document(source, paths.document_json, paths.artifacts, options)
    )
    record_stage(
        paths.manifest,
        "parse",
        "completed",
        input_hash,
        config_hash,
        paths.document_json,
        {"artifacts": artifacts},
    )
    return {"current_path": str(paths.document_json), "completed_stage": "parse"}
