"""translate-ja-v2 の文書翻訳パイプラインを実行する。"""

from __future__ import annotations

import base64
import copy
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
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any, cast

import typer
from pydantic import BaseModel, ConfigDict

LOGGER = logging.getLogger("translate-ja-v2")

HEADING_LABELS = {"title", "section_header", "heading", "header"}
CODE_LABELS = {"code", "program_listing"}
URL_RE = re.compile(r"https?://[^\s)>\"]+")


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

    Returns:
        OpenAI 互換 API の呼び出しに必要な設定値。
    """

    base_url: str
    api_key: str
    model: str
    timeout_seconds: int


class StagePaths(FrozenModel):
    """translate-ja-v2 の主要出力パスを保持する。

    Args:
        output_dir: 成果物の親ディレクトリ。
        docling_json: Docling 変換直後の JSON。
        normalized_json: 決定論的整形後の JSON。
        structured_json: VLM 構造補正後の JSON。
        translated_json: 翻訳情報を付与した JSON。
        markdown: 日本語 Markdown。
        docx: Word docx。
        manifest: 実行 manifest。

    Returns:
        各 stage の成果物パス。
    """

    output_dir: Path
    docling_json: Path
    normalized_json: Path
    structured_json: Path
    translated_json: Path
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
        skip_docx: docx 生成を省略するかどうか。
        force: 既存 Docling JSON があっても変換を再実行するかどうか。
        env: dotenv ファイルのパス。

    Returns:
        パイプライン実行に必要な CLI オプション。
    """

    input: Path
    output_dir: Path | None = None
    output: Path | None = None
    template: Path | None = None
    skip_vlm: bool = False
    skip_docx: bool = False
    force: bool = False
    env: Path = Path(".env")


def configure_logging(level_name: str | None = None) -> None:
    """標準 logging を translate-ja-v2 用に設定する。

    Args:
        level_name: 明示するログレベル。None の場合は LOG_LEVEL 環境変数を使う。

    Returns:
        なし。

    Side Effects:
        root logger の basicConfig を設定する。
    """

    level = getattr(
        logging,
        (level_name or os.environ.get("LOG_LEVEL") or "INFO").upper(),
        logging.INFO,
    )
    logging.basicConfig(
        level=level,
        format=(
            "%(asctime)s %(levelname)s %(name)s "
            "file=%(pathname)s function=%(funcName)s line=%(lineno)d %(message)s"
        ),
    )


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
        timeout_seconds=int(os.environ.get("DOCLING_TIMEOUT_SECONDS", "21600")),
    )


def require_openai_settings() -> OpenAISettings:
    """OpenAI 互換 API の必須設定を環境変数から読み込む。

    Args:
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
        timeout_seconds=int(os.environ.get("OPENAI_TIMEOUT_SECONDS", "1800")),
    )


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


def build_stage_paths(
    input_path: Path, output_dir: Path | None, output_docx: Path | None
) -> StagePaths:
    """入力ファイル名から translate-ja-v2 の出力パスを作る。

    Args:
        input_path: 入力 PDF/Word パス。
        output_dir: 明示された成果物ディレクトリ。None の場合は入力横の output-v2。
        output_docx: 明示された docx 出力パス。None の場合は output_dir 内。

    Returns:
        StagePaths。
    """

    root = output_dir or input_path.parent / "output-v2"
    stem = input_path.stem
    docx = output_docx or root / f"{stem}.ja.docx"
    return StagePaths(
        output_dir=root,
        docling_json=root / f"{stem}.docling.json",
        normalized_json=root / f"{stem}.normalized.json",
        structured_json=root / f"{stem}.structured.json",
        translated_json=root / f"{stem}.translated.json",
        markdown=root / f"{stem}.ja.md",
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

    do_ocr = env_bool("DOCLING_DO_OCR", default=False)
    force_ocr = env_bool("DOCLING_FORCE_OCR", default=False)
    payload: dict[str, str | list[str]] = {
        "to_formats": "json",
        "do_ocr": str(do_ocr).lower(),
        "force_ocr": str(force_ocr).lower(),
        "document_timeout": str(document_timeout),
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
        LOGGER.info(
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
        Docling Serve へ HTTP request を送り、JSON と artifacts を保存する。
    """

    settings = require_docling_settings()
    output_json.parent.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
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
    """

    with zipfile.ZipFile(zip_path, "r") as archive:
        json_members = [
            name
            for name in archive.namelist()
            if name.lower().endswith(".json") and not name.endswith("/")
        ]
        if not json_members:
            raise RuntimeError("Docling zip response did not contain JSON")
        json_member = sorted(json_members, key=lambda name: ("/" in name, name))[0]
        with archive.open(json_member) as source:
            atomic_write_bytes(output_json, source.read())
        for member in archive.namelist():
            if member.endswith("/"):
                continue
            if not member.startswith("artifacts/") and "/artifacts/" not in member:
                continue
            target = artifacts_dir / Path(member).name
            with archive.open(member) as source:
                atomic_write_bytes(target, source.read())


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
    LOGGER.info(
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


def clean_text(value: str) -> str:
    """URL を保護しながら余分な空白・記号を整形する。

    Args:
        value: 原文テキスト。

    Returns:
        整形後テキスト。
    """

    urls: list[str] = []
    protected = URL_RE.sub(lambda match: stash_url(match, urls), value)
    protected = re.sub(r"\.{4,}", "...", protected)
    protected = re.sub(r"[‐‑‒–—―-]{4,}", "---", protected)
    protected = re.sub(r"([_=~*])\1{5,}", r"\1\1\1", protected)
    protected = re.sub(r"[ \t\f\v]+", " ", protected)
    protected = re.sub(r"\n{3,}", "\n\n", protected)
    for index, url in enumerate(urls):
        protected = protected.replace(f"__TRANSLATE_JA_V2_URL_{index}__", url)
    return protected.strip()


def stash_url(match: re.Match[str], urls: list[str]) -> str:
    """正規化中に URL を壊さないため一時退避する。

    Args:
        match: URL の正規表現 match。
        urls: 退避先の URL 配列。

    Returns:
        後で復元する placeholder。

    Side Effects:
        urls に URL 文字列を追加する。
    """

    urls.append(match.group(0))
    return f"__TRANSLATE_JA_V2_URL_{len(urls) - 1}__"


def normalize_document(
    data: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Docling JSON の本文・表・コードブロックを保守的に整形する。

    Args:
        data: Docling JSON object。

    Returns:
        整形後 JSON と patch 配列。
    """

    result = copy.deepcopy(data)
    patches: list[dict[str, Any]] = []
    normalize_coordinate_order(result, patches)
    for group in ("texts", "tables"):
        values = result.get(group)
        if not isinstance(values, list):
            continue
        for index, item in enumerate(values):
            if not isinstance(item, dict):
                continue
            item = cast(dict[str, Any], item)
            ref = self_ref(item, group, index)
            if group == "texts":
                normalize_text_item(item, ref, patches)
            if group == "tables":
                normalize_table_item(item, ref, patches)
    return result, patches


def normalize_text_item(
    item: dict[str, Any], ref: str, patches: list[dict[str, Any]]
) -> None:
    """Docling text item を整形し、変更を patch として記録する。

    Args:
        item: Docling text item。
        ref: item の JSON pointer。
        patches: 変更記録の追加先。

    Returns:
        なし。

    Side Effects:
        item と patches を更新する。
    """

    text = text_of(item)
    if not text or is_code(item):
        if is_code(item):
            item.setdefault("translate_ja_v2", {})["kind"] = "code"
        return
    cleaned = clean_text(text)
    if cleaned != text:
        item["text"] = cleaned
        patches.append(
            {
                "op": "set_text",
                "ref": ref,
                "reason": "normalize text noise",
                "before": text,
                "after": cleaned,
            }
        )


def normalize_table_item(
    item: dict[str, Any], ref: str, patches: list[dict[str, Any]]
) -> None:
    """Docling table item のセル改行と空白を整形する。

    Args:
        item: Docling table item。
        ref: item の JSON pointer。
        patches: 変更記録の追加先。

    Returns:
        なし。

    Side Effects:
        table cell の text と patches を更新する。
    """

    for cell_ref, cell in iter_table_cells(item, ref):
        before = str(cell.get("text") or cell.get("content") or "")
        after = clean_text(before)
        if before != after:
            if "text" in cell:
                cell["text"] = after
            else:
                cell["content"] = after
            patches.append(
                {
                    "op": "set_table_cell_text",
                    "ref": cell_ref,
                    "reason": "normalize table cell",
                    "before": before,
                    "after": after,
                }
            )


def iter_table_cells(
    item: dict[str, Any], ref: str
) -> list[tuple[str, dict[str, Any]]]:
    """Docling table item からセル dict を列挙する。

    Args:
        item: Docling table item。
        ref: table item の JSON pointer。

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
            elif isinstance(cell, str):
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


def openai_retry_options() -> tuple[int, float, float]:
    """OpenAI 互換 API 呼び出しの retry 設定を環境変数から返す。

    Args:
        なし。

    Returns:
        最大試行回数、初回待機秒数、最大待機秒数。
    """

    attempts = max(1, int(os.environ.get("TRANSLATE_JA_V2_OPENAI_MAX_ATTEMPTS", "6")))
    initial_delay = max(
        0.0, float(os.environ.get("TRANSLATE_JA_V2_OPENAI_RETRY_INITIAL_SECONDS", "5"))
    )
    max_delay = max(
        initial_delay,
        float(os.environ.get("TRANSLATE_JA_V2_OPENAI_RETRY_MAX_SECONDS", "60")),
    )
    return attempts, initial_delay, max_delay


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


def chat_text(
    client: Any, settings: OpenAISettings, messages: list[dict[str, Any]]
) -> str:
    """Chat Completions を呼び出し、本文文字列を返す。

    Args:
        client: OpenAI client。
        settings: OpenAI 互換 API 設定。
        messages: Chat messages。

    Returns:
        応答本文。

    Raises:
        RuntimeError: 応答本文が空の場合。
    """

    max_attempts, initial_delay, max_delay = openai_retry_options()
    for attempt in range(1, max_attempts + 1):
        try:
            completion = client.chat.completions.create(
                model=settings.model, messages=messages, temperature=0.0
            )
            break
        except Exception as exc:
            if attempt >= max_attempts or not is_retryable_openai_error(exc):
                raise
            delay = min(max_delay, initial_delay * (2 ** (attempt - 1)))
            LOGGER.warning(
                "Retrying OpenAI request attempt=%s max_attempts=%s delay=%.1f error=%s",
                attempt,
                max_attempts,
                delay,
                exc,
            )
            time.sleep(delay)
    choices = getattr(completion, "choices", None)
    if not choices:
        raise RuntimeError("OpenAI response has no choices")
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    if not content:
        raise RuntimeError("OpenAI response content is empty")
    return str(content).strip()


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
    system = (
        "あなたはDocling JSONの構造補正を担当するVLM/LLMです。"
        "翻訳、要約、本文の創作は禁止です。"
        "対象はpageとbboxで座標補正済みです。"
        "段組みなど座標だけでは判断しにくい箇所に限定し、"
        "見出しと本文の順序、label、levelだけを保守的に補正してください。"
    )
    user = f"""次の座標補正済みDocling要素を読み、画像とbboxも参照して、明らかに見出し・本文の位置が入れ替わっている箇所だけをpatchで返してください。

対象:
{json.dumps(units, ensure_ascii=False)}

返却JSON:
{{
  "patches": [
    {{"op": "set_label", "ref": "#/texts/0", "label": "section_header", "reason": "理由"}},
    {{"op": "set_level", "ref": "#/texts/0", "level": 2, "reason": "理由"}},
    {{"op": "reorder_texts", "refs": ["#/texts/0", "#/texts/1"], "reason": "理由"}}
  ]
}}

補正不要なら {{"patches":[]}} を返してください。
"""
    content = build_multimodal_content(user, artifacts_dir)
    return [{"role": "system", "content": system}, {"role": "user", "content": content}]


def build_multimodal_content(
    prompt: str, artifacts_dir: Path | None
) -> str | list[dict[str, Any]]:
    """VLM へ渡す text とページ画像 content を作る。

    Args:
        prompt: 構造補正プロンプト本文。
        artifacts_dir: Docling PNG artifacts のディレクトリ。None なら text のみ返す。

    Returns:
        OpenAI Chat Completions content。画像がなければ文字列、あれば multimodal content 配列。
    """

    image_paths = collect_page_image_paths(artifacts_dir)
    if not image_paths:
        return prompt
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for path in image_paths:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{encoded}"},
            }
        )
    return content


def collect_page_image_paths(artifacts_dir: Path | None) -> list[Path]:
    """Docling artifacts から VLM に添付する PNG を選ぶ。

    Args:
        artifacts_dir: Docling PNG artifacts のディレクトリ。None または未存在なら空配列。

    Returns:
        ページ画像らしい PNG パスの昇順配列。
    """

    if artifacts_dir is None or not artifacts_dir.exists():
        return []
    max_images = int(os.environ.get("TRANSLATE_JA_V2_MAX_VLM_IMAGES", "12"))
    pngs = sorted(path for path in artifacts_dir.glob("*.png") if path.is_file())
    page_pngs = [path for path in pngs if "page" in path.name.lower()]
    return (page_pngs or pngs)[:max_images]


def collect_structure_units(data: dict[str, Any]) -> list[dict[str, Any]]:
    """構造補正用に texts の要約 unit を集める。

    Args:
        data: Docling JSON。

    Returns:
        ref、page、bbox、coordinate_order、label、level、text を持つ unit 配列。
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
                "coordinate_order": index,
                "label": item.get("label"),
                "level": item.get("level"),
                "text": text[:500],
            }
        )
    return units


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
        if op in {"set_label", "set_level", "set_text"}:
            applied.append(apply_field_patch(result, patch))
        elif op == "reorder_texts":
            applied.append(apply_reorder_texts(result, patch))
        else:
            applied.append(
                {"op": op, "status": "skipped", "reason": "unsupported operation"}
            )
    return result, applied


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
    """set_label/set_level/set_text patch を適用する。

    Args:
        data: 更新対象 JSON。
        patch: field 更新 patch。

    Returns:
        適用結果。
    """

    field_by_op = {"set_label": "label", "set_level": "level", "set_text": "text"}
    op = str(patch.get("op"))
    ref = str(patch.get("ref") or "")
    field = field_by_op[op]
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


def apply_reorder_texts(data: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """texts 配列と関連 JSON pointer を指定 ref 順へ並べ替える。

    Args:
        data: 更新対象 JSON。
        patch: refs を含む reorder_texts patch。

    Returns:
        適用結果。
    """

    texts = data.get("texts")
    refs = patch.get("refs")
    if not isinstance(texts, list) or not isinstance(refs, list):
        return {
            "op": "reorder_texts",
            "status": "failed",
            "error": "texts or refs is not list",
        }
    wanted = [str(ref) for ref in refs]
    current_refs: list[str] = []
    for index, item in enumerate(texts):
        if not isinstance(item, dict):
            return {
                "op": "reorder_texts",
                "status": "failed",
                "error": "texts contains non-object item",
            }
        current_refs.append(self_ref(cast(dict[str, Any], item), "texts", index))
    if len(wanted) != len(set(wanted)) or any(
        ref not in current_refs for ref in wanted
    ):
        return {"op": "reorder_texts", "status": "failed", "error": "unknown ref"}
    wanted_set = set(wanted)
    ordered_refs = wanted + [ref for ref in current_refs if ref not in wanted_set]
    if not reorder_text_collection(data, ordered_refs):
        return {
            "op": "reorder_texts",
            "status": "failed",
            "error": "could not preserve text references",
        }
    return {
        "op": "reorder_texts",
        "status": "success",
        "refs": wanted,
        "reason": patch.get("reason"),
    }


def structure_document(
    data: dict[str, Any], *, skip_vlm: bool, artifacts_dir: Path | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """VLM/LLM で見出し・本文の構造を補正する。

    Args:
        data: 正規化済み Docling JSON。
        skip_vlm: VLM 呼び出しをスキップするかどうか。
        artifacts_dir: Docling PNG artifacts のディレクトリ。

    Returns:
        補正後 JSON と patch 適用結果。
    """

    if skip_vlm:
        return copy.deepcopy(data), []
    settings = require_openai_settings()
    client = openai_client(settings)
    response = chat_text(
        client, settings, build_structure_messages(data, artifacts_dir)
    )
    payload = parse_json_object(response)
    patches = payload.get("patches")
    if not isinstance(patches, list):
        raise ValueError("structure response must contain patches list")
    return apply_structure_patches(
        data, [patch for patch in patches if isinstance(patch, dict)]
    )


def translate_text(
    client: Any, settings: OpenAISettings, source: str, *, style: str
) -> str:
    """短いテキストを日本語へ翻訳する。

    Args:
        client: OpenAI client。
        settings: OpenAI 互換 API 設定。
        source: 原文。
        style: heading/table/body の翻訳スタイル。

    Returns:
        日本語訳。
    """

    if not source.strip():
        return ""
    system = (
        "あなたは専門文書の日英翻訳者です。原文にない説明、要約、事実追加は禁止です。"
    )
    user = f"""次の{style}を日本語へ翻訳してください。

厳守事項:
- 出力は翻訳文だけにする。
- コード、URL、パス、識別子、コマンドは翻訳しない。
- Markdown記号や表の区切り記号を追加しない。

原文:
{source}
"""
    return chat_text(
        client,
        settings,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
    )


def translate_document(data: dict[str, Any]) -> dict[str, Any]:
    """Docling JSON の各要素へ日本語翻訳フィールドを追加する。

    Args:
        data: 構造補正済み Docling JSON。

    Returns:
        翻訳フィールドを追加した JSON。
    """

    result = copy.deepcopy(data)
    settings = require_openai_settings()
    client = openai_client(settings)
    for group in ("texts", "tables"):
        values = result.get(group)
        if not isinstance(values, list):
            continue
        for index, item in enumerate(values):
            if not isinstance(item, dict):
                continue
            item = cast(dict[str, Any], item)
            if group == "texts":
                translate_text_item(item, client, settings)
            if group == "tables":
                translate_table_item(
                    item, client, settings, self_ref(item, group, index)
                )
    return result


def translate_text_item(
    item: dict[str, Any], client: Any, settings: OpenAISettings
) -> None:
    """Docling text item に翻訳フィールドを追加する。

    Args:
        item: Docling text item。
        client: OpenAI client。
        settings: OpenAI 互換 API 設定。

    Returns:
        なし。

    Side Effects:
        item の translate_ja_v2 フィールドを更新する。
    """

    text = text_of(item).strip()
    meta = item.setdefault("translate_ja_v2", {})
    if not text:
        return
    if is_code(item):
        meta.update({"kind": "code", "render_text": text, "translated": False})
        return
    if is_heading(item):
        ja = translate_text(client, settings, text, style="見出し")
        meta.update(
            {
                "kind": "heading",
                "text_en": text,
                "text_ja": ja,
                "render_text": f"{text} / {ja}",
                "translated": True,
            }
        )
        return
    ja = translate_text(client, settings, text, style="本文")
    meta.update(
        {
            "kind": "body",
            "text_en": text,
            "text_ja": ja,
            "render_text": ja,
            "translated": True,
        }
    )


def translate_table_item(
    item: dict[str, Any], client: Any, settings: OpenAISettings, ref: str
) -> None:
    """Docling table item のタイトルとセルへ翻訳フィールドを追加する。

    Args:
        item: Docling table item。
        client: OpenAI client。
        settings: OpenAI 互換 API 設定。
        ref: table item の JSON pointer。

    Returns:
        なし。

    Side Effects:
        item と cell の translate_ja_v2 フィールドを更新する。
    """

    meta = item.setdefault("translate_ja_v2", {})
    caption = str(item.get("caption") or item.get("title") or "").strip()
    if caption:
        caption_ja = translate_text(client, settings, caption, style="表タイトル")
        meta.update(
            {
                "caption_en": caption,
                "caption_ja": caption_ja,
                "caption_render": f"{caption} / {caption_ja}",
            }
        )
    for _cell_ref, cell in iter_table_cells(item, ref):
        source = str(cell.get("text") or cell.get("content") or "").strip()
        cell_meta = cell.setdefault("translate_ja_v2", {})
        if not source:
            continue
        if looks_protected(source):
            cell_meta.update(
                {"text_en": source, "render_text": source, "translated": False}
            )
            continue
        translated = translate_text(client, settings, source, style="表セル")
        cell_meta.update(
            {
                "text_en": source,
                "text_ja": translated,
                "render_text": translated,
                "translated": True,
            }
        )


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
    return text.replace("\n", " ").strip()


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
    """簡易 manifest に stage event を追記する。

    Args:
        path: manifest path。
        event: 追記する event。

    Returns:
        なし。

    Side Effects:
        manifest JSON を作成または更新する。
    """

    manifest = (
        read_json(path)
        if path.exists()
        else {"schema_version": 1, "run_id": str(uuid.uuid4()), "events": []}
    )
    events = manifest.setdefault("events", [])
    if isinstance(events, list):
        event.setdefault("timestamp", utc_now_iso())
        events.append(event)
    manifest["updated_at"] = utc_now_iso()
    write_json(path, manifest)


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
    if args.force or not paths.docling_json.exists():
        LOGGER.info("Starting Docling conversion input=%s", input_path)
        convert_with_docling(
            input_path,
            paths.docling_json,
            artifacts_dir,
        )
        update_manifest(
            paths.manifest,
            {
                "stage": "docling",
                "output": str(paths.docling_json),
                "sha256": sha256_file(paths.docling_json),
            },
        )
    docling_data = read_json(paths.docling_json)
    normalized, normalize_patches = normalize_document(docling_data)
    write_json(paths.normalized_json, normalized)
    update_manifest(
        paths.manifest,
        {
            "stage": "normalize",
            "output": str(paths.normalized_json),
            "patches": len(normalize_patches),
            "coordinate_patches": sum(
                patch.get("rule") == "bbox_reading_order" for patch in normalize_patches
            ),
        },
    )
    structured, structure_patches = structure_document(
        normalized, skip_vlm=args.skip_vlm, artifacts_dir=artifacts_dir
    )
    write_json(paths.structured_json, structured)
    update_manifest(
        paths.manifest,
        {
            "stage": "structure",
            "output": str(paths.structured_json),
            "patches": len(structure_patches),
        },
    )
    translated = translate_document(structured)
    write_json(paths.translated_json, translated)
    update_manifest(
        paths.manifest,
        {
            "stage": "translate",
            "output": str(paths.translated_json),
            "sha256": sha256_file(paths.translated_json),
        },
    )
    markdown = render_markdown(translated)
    atomic_write_bytes(paths.markdown, markdown.encode("utf-8"))
    update_manifest(
        paths.manifest,
        {
            "stage": "markdown",
            "output": str(paths.markdown),
            "sha256": sha256_file(paths.markdown),
        },
    )
    if not args.skip_docx:
        convert_markdown_to_docx(
            paths.markdown,
            paths.docx,
            args.template.resolve() if args.template else None,
        )
        update_manifest(
            paths.manifest,
            {
                "stage": "docx",
                "output": str(paths.docx),
                "sha256": sha256_file(paths.docx),
            },
        )
    return paths


app = typer.Typer(
    add_completion=False,
    help="translate-ja-v2 document translation pipeline",
)


def execute_pipeline(options: PipelineOptions) -> int:
    """translate-ja-v2 パイプラインを実行して終了コードを返す。

    Args:
        options: CLI オプション。

    Returns:
        プロセス終了コード。

    Side Effects:
        環境変数、ログ、Docling/OpenAI/pandoc、成果物ファイルを扱う。
    """

    load_dotenv_file(options.env)
    configure_logging()
    try:
        paths = run_pipeline(options)
    except KeyboardInterrupt:
        LOGGER.error("translate-ja-v2 was interrupted")
        return 130
    except Exception as exc:
        LOGGER.exception("translate-ja-v2 failed: %s", exc)
        return 1
    LOGGER.info(
        "translate-ja-v2 completed markdown=%s docx=%s", paths.markdown, paths.docx
    )
    return 0


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
    skip_docx: Annotated[bool, typer.Option(help="write Markdown/JSON only")] = False,
    force: Annotated[
        bool,
        typer.Option(help="rerun Docling conversion even if JSON exists"),
    ] = False,
    env: Annotated[Path, typer.Option(help="dotenv path")] = Path(".env"),
) -> None:
    """CLI から translate-ja-v2 パイプラインを実行する。

    Args:
        input: PDF/Word 入力ファイル。
        output_dir: 中間成果物の出力ディレクトリ。
        output: 最終 docx の出力パス。
        template: pandoc reference docx/dotx。
        skip_vlm: VLM による構造補正を省略するかどうか。
        skip_docx: docx 生成を省略するかどうか。
        force: 既存 Docling JSON があっても変換を再実行するかどうか。
        env: dotenv ファイルのパス。

    Returns:
        なし。

    Side Effects:
        パイプラインを実行し、終了コードを Typer へ渡す。
    """

    exit_code = execute_pipeline(
        PipelineOptions(
            input=input,
            output_dir=output_dir,
            output=output,
            template=template,
            skip_vlm=skip_vlm,
            skip_docx=skip_docx,
            force=force,
            env=env,
        )
    )
    raise typer.Exit(code=exit_code)


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
    LOGGER.info("Elapsed time %.3f seconds", perf_counter() - started_at)
    sys.exit(exit_code)
