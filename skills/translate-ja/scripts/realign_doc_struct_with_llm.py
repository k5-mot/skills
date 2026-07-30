"""OpenAI 互換 LLM/VLM で Docling schema JSON の構造補正パッチを適用する。"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import uuid
from pathlib import Path
from time import perf_counter
from typing import Any

from config import build_langfuse_headers, load_dotenv, require_openai_settings
from io_utils import configure_logging, load_existing_json, read_json, sha256_file, utc_now_iso, write_json

LOGGER = logging.getLogger("translate-ja.realign_doc_struct_with_llm")
APP_MAX_RETRIES = 10


def _load_openai_client(settings: Any) -> Any:
    """OpenAI Python クライアントを遅延 import して生成する。"""

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai package is required for realign_doc_struct_with_llm.py") from exc
    return OpenAI(
        base_url=settings.base_url,
        api_key=settings.api_key,
        timeout=settings.timeout_seconds,
        max_retries=settings.max_retries,
    )


def _texts_by_page(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Docling texts をページ単位の補正 unit に分ける。"""

    texts = data.get("texts")
    if not isinstance(texts, list):
        return []
    pages: dict[int, list[dict[str, Any]]] = {}
    for index, item in enumerate(texts):
        if not isinstance(item, dict):
            continue
        page_no = 0
        prov = item.get("prov")
        if isinstance(prov, list) and prov and isinstance(prov[0], dict) and isinstance(prov[0].get("page_no"), int):
            page_no = prov[0]["page_no"]
        pages.setdefault(page_no, []).append(
            {
                "ref": item.get("self_ref") or f"#/texts/{index}",
                "label": item.get("label"),
                "text": item.get("text"),
            }
        )
    return [{"unit_id": f"page-{page_no:04d}", "page_no": page_no, "items": items} for page_no, items in sorted(pages.items())]


def _format_page(page_no: Any) -> str:
    """ログ用にページ番号を表現する。"""

    return str(page_no) if isinstance(page_no, int) else "unknown"


def _manifest_skeleton(input_path: Path, output_path: Path, settings: Any, units: list[dict[str, Any]]) -> dict[str, Any]:
    """構造補正 manifest の初期構造を作る。"""

    now = utc_now_iso()
    return {
        "schema_version": 1,
        "script": "realign_doc_struct_with_llm.py",
        "run_id": str(uuid.uuid4()),
        "started_at": now,
        "updated_at": now,
        "input_path": str(input_path),
        "input_sha256": sha256_file(input_path),
        "output_path": str(output_path),
        "model": settings.model,
        "settings": {
            "temperature": 0.0,
            "timeout_seconds": settings.timeout_seconds,
            "openai_max_retries": settings.max_retries,
            "app_max_retries": APP_MAX_RETRIES,
            "stream": True,
        },
        "units": [
            {
                "unit_id": unit["unit_id"],
                "status": "pending",
                "attempts": 0,
                "updated_at": now,
                "page_no": unit.get("page_no"),
                "patches": [],
                "changes": [],
            }
            for unit in units
        ],
    }


def _pointer_target(data: dict[str, Any], pointer: str) -> tuple[dict[str, Any], str]:
    """JSON pointer の親 dict と末尾 key を返す。"""

    if not pointer.startswith("#/"):
        raise ValueError(f"unsupported pointer: {pointer}")
    current: Any = data
    parts = pointer[2:].split("/")
    for part in parts[:-1]:
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise ValueError(f"invalid pointer: {pointer}")
    key = parts[-1].replace("~1", "/").replace("~0", "~")
    if not isinstance(current, dict):
        raise ValueError(f"pointer parent is not object: {pointer}")
    return current, key


def apply_patches(data: dict[str, Any], patches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """補正パッチを Docling JSON へ適用し、適用結果を返す。"""

    results: list[dict[str, Any]] = []
    for patch in patches:
        op = patch.get("op")
        ref = str(patch.get("ref") or "")
        try:
            if op == "set_text":
                parent, key = _pointer_target(data, f"{ref}/text")
                before = parent.get(key)
                parent[key] = patch["text"]
            elif op == "set_label":
                parent, key = _pointer_target(data, f"{ref}/label")
                before = parent.get(key)
                parent[key] = patch["label"]
            elif op == "set_level":
                parent, key = _pointer_target(data, f"{ref}/level")
                before = parent.get(key)
                parent[key] = patch["level"]
            else:
                raise ValueError(f"unsupported patch op: {op}")
            results.append({"ref": ref, "op": op, "status": "success", "before": before, "after": patch.get("text", patch.get("label", patch.get("level")))})
        except Exception as exc:
            results.append({"ref": ref, "op": op, "status": "failed", "error": str(exc)})
    return results


def _build_messages(unit: dict[str, Any]) -> list[dict[str, str]]:
    """構造補正用の Chat Completions messages を作る。"""

    system = (
        "あなたは Docling schema JSON の構造補正を行うレビュアです。"
        "翻訳、要約、事実追加は禁止です。出力は JSON のみです。"
    )
    user = f"""次の Docling text items を確認し、明らかな構造誤りだけを補正するパッチを返してください。

対象:
{json.dumps(unit, ensure_ascii=False)}

出力 JSON schema:
{{
  "patches": [
    {{
      "op": "set_text | set_label | set_level",
      "ref": "#/texts/0",
      "text": "set_text の場合のみ",
      "label": "set_label の場合のみ",
      "level": 2,
      "reason": "変更理由",
      "confidence": 0.0
    }}
  ]
}}

補正不要なら {{"patches":[]}} を返してください。
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _stream_chat(client: Any, *, settings: Any, messages: list[dict[str, str]], headers: dict[str, str], unit_id: str) -> str:
    """Chat Completions stream を読み切って本文を返す。"""

    request = {
        "model": settings.model,
        "messages": messages,
        "temperature": 0.0,
        "extra_headers": headers or None,
    }
    stream = client.chat.completions.create(
        **request,
        stream=True,
    )
    parts: list[str] = []
    for event in stream:
        content = _completion_choice_content(event)
        if content:
            parts.append(content)
            LOGGER.debug("unit=%s stream_delta=%s", unit_id, content)
    text = "".join(parts).strip()
    if text:
        return text
    LOGGER.warning("stream 応答本文が空でした。非 stream で再取得します unit=%s", unit_id)
    completion = client.chat.completions.create(
        **request,
        stream=False,
    )
    return (_completion_choice_content(completion) or "").strip()


def _get_field(value: Any, name: str) -> Any:
    """dict と OpenAI SDK object の両方から field を読む。"""

    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _completion_choice_content(completion: Any) -> str | None:
    """Chat Completion/Chunk の先頭 choice から本文候補を取り出す。"""

    choices = _get_field(completion, "choices")
    if not choices:
        return None
    choice = choices[0]
    for container_name in ("delta", "message"):
        container = _get_field(choice, container_name)
        content = _get_field(container, "content") if container is not None else None
        if content:
            return str(content)
    text = _get_field(choice, "text")
    if text:
        return str(text)
    return None


def _parse_patch_response(text: str) -> list[dict[str, Any]]:
    """LLM 応答から patches 配列を取り出して検証する。"""

    payload = _load_patch_payload(text)
    if not isinstance(payload, dict) or not isinstance(payload.get("patches"), list):
        raise ValueError("LLM response must be an object with patches array")
    patches = payload["patches"]
    for patch in patches:
        if not isinstance(patch, dict) or patch.get("op") not in {"set_text", "set_label", "set_level"} or not patch.get("ref"):
            raise ValueError("invalid patch object")
    return patches


def _load_patch_payload(text: str) -> Any:
    """LLM 応答から patches object の JSON payload を読む。"""

    stripped = text.strip()
    if not stripped:
        raise ValueError("LLM response was empty")
    decoder = json.JSONDecoder()
    try:
        payload, end = decoder.raw_decode(stripped)
        if not stripped[end:].strip():
            return payload
    except json.JSONDecodeError:
        pass
    for index, char in enumerate(stripped):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "patches" in payload:
            return payload
    preview = stripped.replace("\n", "\\n")[:120]
    raise ValueError(f"LLM response did not contain a patches JSON object: {preview}")


def realign(input_path: Path, output_path: Path, *, force: bool) -> None:
    """Docling JSON の構造補正を行い、silver JSON と manifest を保存する。"""

    data = read_json(input_path)
    if not isinstance(data, dict):
        raise ValueError(f"Docling JSON object expected: {input_path}")
    settings = require_openai_settings()
    client = _load_openai_client(settings)
    units = _texts_by_page(data)
    manifest_path = output_path.parent / "manifest.realign.json"
    if force:
        output_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
    manifest = load_existing_json(manifest_path) or _manifest_skeleton(input_path, output_path, settings, units)
    if manifest.get("input_sha256") != sha256_file(input_path):
        raise RuntimeError("manifest input_sha256 does not match; rerun with --force")
    unit_states = {str(unit["unit_id"]): unit for unit in manifest.get("units", []) if isinstance(unit, dict)}
    working = copy.deepcopy(data)
    for state in manifest.get("units", []):
        if state.get("status") == "success":
            apply_patches(working, state.get("patches") or [])
    source_units = {unit["unit_id"]: unit for unit in units}
    for unit_id, unit in source_units.items():
        state = unit_states[unit_id]
        if state.get("status") == "success":
            LOGGER.info("構造補正をスキップします unit=%s page=%s status=success", unit_id, _format_page(unit.get("page_no")))
            continue
        state["status"] = "running"
        state["updated_at"] = utc_now_iso()
        write_json(manifest_path, manifest)
        last_error = ""
        patches: list[dict[str, Any]] = []
        for attempt in range(int(state.get("attempts", 0)) + 1, APP_MAX_RETRIES + 1):
            state["attempts"] = attempt
            write_json(manifest_path, manifest)
            LOGGER.info("構造補正を開始します unit=%s page=%s attempt=%s", unit_id, _format_page(unit.get("page_no")), attempt)
            try:
                headers = build_langfuse_headers(
                    script_name="realign_doc_struct_with_llm.py",
                    run_id=str(manifest["run_id"]),
                    unit_id=unit_id,
                    attempt=attempt,
                    input_stem=input_path.stem,
                    input_identity=str(input_path),
                )
                response = _stream_chat(client, settings=settings, messages=_build_messages(unit), headers=headers, unit_id=unit_id)
                patches = _parse_patch_response(response)
                last_error = ""
                LOGGER.info("構造補正に成功しました unit=%s page=%s attempt=%s patches=%s", unit_id, _format_page(unit.get("page_no")), attempt, len(patches))
                break
            except Exception as exc:
                last_error = str(exc)[:500]
                LOGGER.warning("構造補正に失敗しました unit=%s page=%s attempt=%s error=%s", unit_id, _format_page(unit.get("page_no")), attempt, last_error)
                if attempt < APP_MAX_RETRIES:
                    LOGGER.info("構造補正をリトライします unit=%s page=%s next_attempt=%s", unit_id, _format_page(unit.get("page_no")), attempt + 1)
        if last_error:
            state.update({"status": "failed", "error": last_error, "updated_at": utc_now_iso()})
            write_json(manifest_path, manifest)
            raise RuntimeError(f"realign failed unit={unit_id}: {last_error}")
        results = apply_patches(working, patches)
        state.update({"status": "success", "patches": patches, "changes": results, "updated_at": utc_now_iso()})
        manifest["updated_at"] = utc_now_iso()
        write_json(manifest_path, manifest)
    write_json(output_path, working)


def main() -> int:
    """CLI 引数を読み、Docling schema JSON の構造補正を実行する。"""

    configure_logging()
    load_dotenv()
    parser = argparse.ArgumentParser(description="Realign Docling schema JSON with LLM patches")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--openai-timeout", type=int)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.openai_timeout is not None:
        os.environ["OPENAI_TIMEOUT_SECONDS"] = str(args.openai_timeout)
    output = Path(args.output)
    if output.exists() and not args.force:
        LOGGER.info("既存出力を再利用します output=%s", output)
        return 0
    realign(Path(args.input), output, force=args.force)
    LOGGER.info("構造補正が完了しました output=%s", output)
    return 0


if __name__ == "__main__":
    started_at = perf_counter()
    try:
        exit_code = main()
    finally:
        LOGGER.info("処理時間 %.3f 秒", perf_counter() - started_at)
    raise SystemExit(exit_code)
