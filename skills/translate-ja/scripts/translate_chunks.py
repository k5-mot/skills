"""Chunk JSONL を OpenAI 互換 API で日本語へ翻訳する。"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from time import perf_counter
from typing import Any

from config import build_langfuse_headers, load_dotenv, require_openai_settings
from io_utils import (
    configure_logging,
    load_existing_json,
    read_jsonl,
    sha256_path,
    utc_now_iso,
    write_json,
    write_jsonl,
)
from translate_ja import (
    build_translation_messages,
    load_dictionary_csv,
    select_glossary_entries,
)

LOGGER = logging.getLogger("translate-ja.translate_chunks")
APP_MAX_RETRIES = 10


def _format_pages(pages: Any) -> str:
    """ログ用に chunk のページ番号リストを短く表現する。"""

    if not isinstance(pages, list):
        return "unknown"
    ordered = sorted({page for page in pages if isinstance(page, int)})
    if not ordered:
        return "unknown"
    ranges: list[str] = []
    start = previous = ordered[0]
    for page in ordered[1:]:
        if page == previous + 1:
            previous = page
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = page
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def _resolve_input(path: str | Path) -> Path:
    """入力パスがディレクトリなら chunks.source.jsonl を補完する。"""

    target = Path(path)
    if target.is_dir():
        return target / "chunks.source.jsonl"
    return target


def _load_openai_client(settings: Any) -> Any:
    """OpenAI Python クライアントを遅延 import して生成する。"""

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "openai package is required for translate_chunks.py"
        ) from exc
    return OpenAI(
        base_url=settings.base_url,
        api_key=settings.api_key,
        timeout=settings.timeout_seconds,
        max_retries=settings.max_retries,
    )


def _stream_chat(
    client: Any,
    *,
    settings: Any,
    messages: list[dict[str, str]],
    extra_headers: dict[str, str],
    chunk_id: str,
) -> str:
    """Chat Completions stream を読み切って本文を返す。"""

    stream = client.chat.completions.create(
        model=settings.model,
        messages=messages,
        temperature=0.0,
        stream=True,
        extra_headers=extra_headers or None,
    )
    parts: list[str] = []
    for event in stream:
        choice = event.choices[0] if getattr(event, "choices", None) else None
        delta = getattr(choice, "delta", None) if choice else None
        content = getattr(delta, "content", None) if delta else None
        if content:
            parts.append(content)
            LOGGER.debug("chunk=%s stream_delta=%s", chunk_id, content)
    return "".join(parts).strip()


def _validate_translation(source: str, translated: str, *, kind: str) -> None:
    """翻訳後 Markdown の壊れやすい構造を検証する。"""

    if not translated.strip():
        raise ValueError("translated text is empty")
    if source.count("```") != translated.count("```"):
        raise ValueError("code fence count changed")
    if kind == "table":
        source_rows = [
            line for line in source.splitlines() if line.strip().startswith("|")
        ]
        translated_rows = [
            line for line in translated.splitlines() if line.strip().startswith("|")
        ]
        if len(source_rows) != len(translated_rows):
            raise ValueError("markdown table row count changed")
        for source_row, translated_row in zip(
            source_rows, translated_rows, strict=True
        ):
            if source_row.count("|") != translated_row.count("|"):
                raise ValueError("markdown table column count changed")


def _manifest_skeleton(
    *,
    input_path: Path,
    output_dir: Path,
    settings: Any,
    chunks: list[dict[str, Any]],
    dictionary_meta: dict[str, Any],
) -> dict[str, Any]:
    """翻訳 manifest の初期構造を作る。"""

    now = utc_now_iso()
    return {
        "schema_version": 1,
        "script": "translate_chunks.py",
        "run_id": str(uuid.uuid4()),
        "started_at": now,
        "updated_at": now,
        "input_path": str(input_path),
        "input_sha256": sha256_path(input_path),
        "output_path": str(output_dir),
        "model": settings.model,
        "settings": {
            "target_lang": "ja",
            "temperature": 0.0,
            "timeout_seconds": settings.timeout_seconds,
            "openai_max_retries": settings.max_retries,
            "app_max_retries": APP_MAX_RETRIES,
            "stream": True,
            "langfuse_enabled": bool(
                os.environ.get("LANGFUSE_PUBLIC_KEY")
                or os.environ.get("LANGFUSE_SECRET_KEY")
                or os.environ.get("LANGFUSE_OTEL_HOST")
            ),
            "dictionary": dictionary_meta,
        },
        "units": [
            {
                "unit_id": str(chunk["chunk_id"]),
                "status": "pending",
                "attempts": 0,
                "updated_at": now,
                "output_ref": None,
                "changes": [],
            }
            for chunk in chunks
        ],
    }


def _unit_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """manifest units を unit_id で引ける dict にする。"""

    return {
        str(unit["unit_id"]): unit
        for unit in manifest.get("units", [])
        if isinstance(unit, dict) and "unit_id" in unit
    }


def translate_chunks(
    input_path: Path, output_dir: Path, *, dictionary_csv: str | None, force: bool
) -> None:
    """Chunk JSONL を翻訳し、出力 JSONL と manifest を更新する。"""

    chunks = read_jsonl(input_path)
    glossary = load_dictionary_csv(
        dictionary_csv or os.environ.get("TRANSLATE_JA_DICTIONARY_CSV")
    )
    dictionary_meta = {
        "path": glossary.path,
        "sha256": glossary.sha256,
        "raw_count": glossary.raw_count,
        "effective_count": glossary.effective_count,
    }
    settings = require_openai_settings()
    client = _load_openai_client(settings)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "chunks.ja.jsonl"
    manifest_path = output_dir / "manifest.translate.json"
    if force:
        output_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
    manifest = load_existing_json(manifest_path) or _manifest_skeleton(
        input_path=input_path,
        output_dir=output_dir,
        settings=settings,
        chunks=chunks,
        dictionary_meta=dictionary_meta,
    )
    if manifest.get("input_sha256") != sha256_path(input_path):
        raise RuntimeError("manifest input_sha256 does not match; rerun with --force")
    existing_rows = (
        {str(row.get("chunk_id")): row for row in read_jsonl(output_path)}
        if output_path.exists()
        else {}
    )
    units = _unit_map(manifest)
    output_rows: list[dict[str, Any]] = []
    for chunk in chunks:
        chunk_id = str(chunk["chunk_id"])
        unit = units[chunk_id]
        pages = _format_pages(chunk.get("page_numbers"))
        status = str(unit.get("status"))
        if (
            status in {"success", "skipped", "fallback_source"}
            and chunk_id in existing_rows
        ):
            LOGGER.info(
                "翻訳済み chunk をスキップします chunk=%s pages=%s status=%s",
                chunk_id,
                pages,
                status,
            )
            output_rows.append(existing_rows[chunk_id])
            continue
        unit["status"] = "running"
        unit["updated_at"] = utc_now_iso()
        write_json(manifest_path, manifest)
        if not chunk.get("translatable", True) or chunk.get("kind") == "code":
            translated = dict(chunk)
            translated.update(
                {
                    "translated_text": chunk.get("source_text", ""),
                    "model": settings.model,
                    "status": "skipped",
                }
            )
            unit.update(
                {
                    "status": "skipped",
                    "output_ref": f"chunks.ja.jsonl#{chunk_id}",
                    "updated_at": utc_now_iso(),
                }
            )
            output_rows.append(translated)
            write_jsonl(output_path, output_rows)
            write_json(manifest_path, manifest)
            LOGGER.info(
                "翻訳対象外 chunk をスキップしました chunk=%s pages=%s kind=%s",
                chunk_id,
                pages,
                chunk.get("kind"),
            )
            continue
        source_text = str(chunk.get("source_text") or "")
        last_error = ""
        translated_text = ""
        for attempt in range(int(unit.get("attempts", 0)) + 1, APP_MAX_RETRIES + 1):
            unit["attempts"] = attempt
            unit["updated_at"] = utc_now_iso()
            write_json(manifest_path, manifest)
            LOGGER.info(
                "翻訳を開始します chunk=%s pages=%s attempt=%s kind=%s chars=%s",
                chunk_id,
                pages,
                attempt,
                chunk.get("kind"),
                chunk.get("char_count"),
            )
            try:
                entries = select_glossary_entries(source_text, glossary)
                messages = build_translation_messages(
                    source_text, glossary_entries=entries
                )
                headers = build_langfuse_headers(
                    script_name="translate_chunks.py",
                    run_id=str(manifest["run_id"]),
                    unit_id=chunk_id,
                    attempt=attempt,
                    input_stem=input_path.stem,
                    input_identity=str(input_path),
                )
                translated_text = _stream_chat(
                    client,
                    settings=settings,
                    messages=messages,
                    extra_headers=headers,
                    chunk_id=chunk_id,
                )
                _validate_translation(
                    source_text, translated_text, kind=str(chunk.get("kind") or "text")
                )
                last_error = ""
                LOGGER.info(
                    "翻訳に成功しました chunk=%s pages=%s attempt=%s chars=%s",
                    chunk_id,
                    pages,
                    attempt,
                    len(translated_text),
                )
                break
            except Exception as exc:
                last_error = str(exc)[:500]
                LOGGER.warning(
                    "翻訳に失敗しました chunk=%s pages=%s attempt=%s error=%s",
                    chunk_id,
                    pages,
                    attempt,
                    last_error,
                )
                if attempt < APP_MAX_RETRIES:
                    LOGGER.info(
                        "翻訳をリトライします chunk=%s pages=%s next_attempt=%s",
                        chunk_id,
                        pages,
                        attempt + 1,
                    )
                    time.sleep(min(2**attempt, 30))
        row = dict(chunk)
        row["model"] = settings.model
        if last_error:
            row.update(
                {
                    "translated_text": source_text,
                    "status": "fallback_source",
                    "error": last_error,
                }
            )
            unit["status"] = "fallback_source"
            LOGGER.warning(
                "翻訳を原文 fallback にしました chunk=%s pages=%s attempts=%s error=%s",
                chunk_id,
                pages,
                unit.get("attempts"),
                last_error,
            )
        else:
            row.update({"translated_text": translated_text, "status": "success"})
            unit["status"] = "success"
        unit["output_ref"] = f"chunks.ja.jsonl#{chunk_id}"
        unit["updated_at"] = utc_now_iso()
        output_rows.append(row)
        write_jsonl(output_path, output_rows)
        manifest["updated_at"] = utc_now_iso()
        write_json(manifest_path, manifest)


def main() -> int:
    """CLI 引数を読み、Chunk JSONL を日本語へ翻訳する。"""

    configure_logging()
    load_dotenv()
    parser = argparse.ArgumentParser(description="Translate Chunk JSONL into Japanese")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--dictionary-csv")
    parser.add_argument("--openai-timeout", type=int)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.openai_timeout is not None:
        os.environ["OPENAI_TIMEOUT_SECONDS"] = str(args.openai_timeout)
    translate_chunks(
        _resolve_input(args.input),
        Path(args.output),
        dictionary_csv=args.dictionary_csv,
        force=args.force,
    )
    LOGGER.info("翻訳が完了しました output=%s", Path(args.output) / "chunks.ja.jsonl")
    return 0


if __name__ == "__main__":
    started_at = perf_counter()
    try:
        exit_code = main()
    finally:
        LOGGER.info("処理時間 %.3f 秒", perf_counter() - started_at)
    sys.exit(exit_code)
