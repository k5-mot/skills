"""StructureStageをLangChain structured outputで実装する。"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, model_validator

from ..config import PipelineOptions, PipelineState, state_options, state_paths
from ..io import (
    LOGGER,
    hash_file,
    hash_json,
    read_json,
    read_rules,
    record_stage,
    self_ref,
    stage_cached,
    stage_partial,
    text_of,
    write_json,
)
from ..llm import image_messages, structured_model


class StructurePatch(BaseModel):
    """VLMが返す構造補正候補を表す。"""

    op: str
    ref: str | None = None
    refs: list[str] = Field(default_factory=list)
    label: str | None = None
    level: int | None = None
    code_spans: list[str] = Field(default_factory=list)
    reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize_shorthand(cls, value: Any) -> Any:
        """操作名をkeyにしたVLM応答を正規patch形式へ変換する。

        Args:
            value: Pydantic検証前のpatch候補。

        Returns:
            単一の既知操作を正規化した値。対象外なら元の値。
        """

        if not isinstance(value, dict) or value.get("op"):
            return value
        fields = {
            "set_label": "label",
            "set_heading_level": "level",
            "merge_texts": "refs",
            "set_table_cell_inline_code": "code_spans",
        }
        operations = [operation for operation in fields if operation in value]
        if len(operations) != 1:
            return value
        operation = operations[0]
        normalized = dict(value)
        operation_value = normalized.pop(operation)
        normalized["op"] = operation
        normalized.setdefault(fields[operation], operation_value)
        return normalized


class StructureResponse(BaseModel):
    """StructureStageのstructured output schema。"""

    patches: list[StructurePatch] = Field(default_factory=list)


def _page_no(item: dict[str, Any]) -> int | None:
    """Docling要素の先頭ページ番号を返す。

    Args:
        item: textまたはtable要素。

    Returns:
        ページ番号。存在しなければNone。
    """

    prov = item.get("prov")
    if isinstance(prov, list) and prov and isinstance(prov[0], dict):
        value = prov[0].get("page_no")
        return value if isinstance(value, int) else None
    return None


def _page_payload(
    document: dict[str, Any], page: int | None
) -> dict[str, list[dict[str, Any]]]:
    """1ページ分のtextとtable cellをcompact JSONへ変換する。

    Args:
        document: NormalizeStage成果物。
        page: 対象ページ番号。

    Returns:
        textsとcellsを持つprompt payload。
    """

    texts: list[dict[str, Any]] = []
    for index, item in enumerate(document.get("texts", [])):
        if isinstance(item, dict) and _page_no(item) == page:
            texts.append(
                {
                    "ref": self_ref(item, "texts", index),
                    "label": item.get("label"),
                    "level": item.get("level"),
                    "text": text_of(item)[:500],
                    "bbox": (item.get("prov") or [{}])[0].get("bbox"),
                }
            )
    cells: list[dict[str, Any]] = []
    for table_index, table in enumerate(document.get("tables", [])):
        if not isinstance(table, dict) or _page_no(table) != page:
            continue
        grid = table.get("data", {}).get("grid", [])
        for row_index, row in enumerate(grid if isinstance(grid, list) else []):
            for column_index, cell in enumerate(row if isinstance(row, list) else []):
                if isinstance(cell, dict):
                    cells.append(
                        {
                            "ref": f"#/tables/{table_index}/data/grid/{row_index}/{column_index}",
                            "text": text_of(cell)[:500],
                            "bbox": cell.get("bbox"),
                        }
                    )
    return {"texts": texts, "cells": cells}


def _page_image(
    document: dict[str, Any], output_dir: Path, page: int | None
) -> Path | None:
    """Docling page URIを安全なローカルパスへ解決する。

    Args:
        document: page metadataを持つDocling JSON。
        output_dir: JSONとartifactsを置くdirectory。
        page: 対象ページ番号。

    Returns:
        存在する画像パス。安全に解決できなければNone。
    """

    data = document.get("pages", {}).get(str(page), {}) if page is not None else {}
    uri = data.get("image", {}).get("uri") if isinstance(data, dict) else None
    if not isinstance(uri, str) or urlparse(uri).scheme or Path(uri).is_absolute():
        return None
    pure = PurePosixPath(uri)
    if ".." in pure.parts:
        return None
    candidate = output_dir.joinpath(*pure.parts).resolve()
    try:
        candidate.relative_to(output_dir.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _request(
    options: PipelineOptions,
    payload: dict[str, list[dict[str, Any]]],
    rules: str,
    image: Path | None,
) -> list[StructurePatch]:
    """1つのpayloadをVLMへ送り構造patchを得る。

    Args:
        options: LLMとcontext設定。
        payload: 対象textとtable cell。
        rules: Structure専用外部ルール。
        image: 対応するページ画像。

    Returns:
        Pydantic検証済みpatch配列。
    """

    system = (
        "Docling JSONの構造を補正し、patches配列を持つobjectで回答してください。"
        "各patchはopと対象値を別fieldで返してください。"
    )
    prompt = f"""次の要素と画像を確認し、必要なpatchだけをJSONで返してください。

patch形式:
- set_label: {{"op":"set_label","ref":"...","label":"code|caption"}}
- set_heading_level: {{"op":"set_heading_level","ref":"...","level":1}}
- merge_texts: {{"op":"merge_texts","refs":["...","..."]}}
- set_table_cell_inline_code: {{"op":"set_table_cell_inline_code","ref":"...","code_spans":["..."]}}

外部ルール:
{rules or "なし"}

要素JSON:
{json.dumps(payload, ensure_ascii=False)}
"""
    if len(prompt) > options.context_chars:
        raise ValueError("Structure prompt exceeds context_chars")
    runnable = structured_model(options, StructureResponse, max_tokens=4096)
    return runnable.invoke(image_messages(system, prompt, image)).patches


def _target(document: dict[str, Any], ref: str) -> dict[str, Any] | None:
    """許可されたJSON pointerから対象objectを返す。

    Args:
        document: 更新対象Docling JSON。
        ref: textまたはtable cellのJSON pointer。

    Returns:
        対象object。解決できなければNone。
    """

    parts = ref.removeprefix("#/").split("/")
    value: Any = document
    try:
        for part in parts:
            value = value[int(part)] if isinstance(value, list) else value[part]
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _apply(document: dict[str, Any], patches: list[StructurePatch]) -> int:
    """検証済みpatchを追加のローカル制約付きで適用する。

    Args:
        document: 更新対象Docling JSON。
        patches: VLMのpatch候補。

    Returns:
        適用したpatch数。

    Side Effects:
        documentを直接更新する。
    """

    applied = 0
    for patch in patches:
        target = _target(document, patch.ref or "")
        if patch.op == "set_label" and target and patch.label in {"code", "caption"}:
            if patch.label == "code" or target.get("label") in {
                "title",
                "section_header",
                "heading",
                "header",
            }:
                target["label"] = patch.label
                applied += 1
        elif (
            patch.op == "set_heading_level"
            and target
            and patch.level is not None
            and 1 <= patch.level <= 6
            and target.get("label") in {"title", "section_header", "heading", "header"}
        ):
            target["level"] = patch.level
            applied += 1
        elif patch.op == "set_table_cell_inline_code" and target:
            source = text_of(target)
            spans = [span for span in patch.code_spans if span and span in source]
            target.setdefault("structure_ja_v3", {})["inline_code_spans"] = spans
            applied += 1
        elif patch.op == "merge_texts" and len(patch.refs) == 2:
            left, right = (_target(document, ref) for ref in patch.refs)
            texts = document.get("texts", [])
            if (
                left is not None
                and right is not None
                and isinstance(texts, list)
                and left in texts
                and right in texts
                and texts.index(right) == texts.index(left) + 1
            ):
                left["text"] = f"{text_of(left)}\n{text_of(right)}"
                left["label"] = "code"
                right["text"] = ""
                right.setdefault("structure_ja_v3", {})["merged_into"] = self_ref(
                    left, "texts", texts.index(left)
                )
                applied += 1
    return applied


def _batch_refs(payload: dict[str, list[dict[str, Any]]]) -> set[str]:
    """Structure payloadに含まれる要素IDを返す。

    Args:
        payload: textsとcellsを持つprompt payload。

    Returns:
        payload内のref集合。
    """

    return {
        str(item["ref"])
        for group in ("texts", "cells")
        for item in payload[group]
        if item.get("ref")
    }


def _request_with_fallback(
    options: PipelineOptions,
    payload: dict[str, list[dict[str, Any]]],
    rules: str,
    image: Path | None,
) -> Iterator[tuple[dict[str, list[dict[str, Any]]], list[StructurePatch]]]:
    """VLM失敗時に要素数を半減し、成功したpatchを順次返す。

    Args:
        options: LLMとcontext設定。
        payload: textsとcellsを持つStructure対象。
        rules: Structure外部ルール。
        image: 対応するページ画像。

    Yields:
        成功したsub-payloadとpatch配列。

    Raises:
        Exception: 1要素でもVLM呼び出しが失敗した場合。
    """

    try:
        yield payload, _request(options, payload, rules, image)
    except Exception:
        tagged = [
            (group, item) for group in ("texts", "cells") for item in payload[group]
        ]
        if len(tagged) == 1:
            raise
        middle = len(tagged) // 2
        LOGGER.warning(
            "StructureStage batch failed; retrying with smaller batches elements=%s->%s",
            len(tagged),
            middle,
        )
        for half in (tagged[:middle], tagged[middle:]):
            smaller = {"texts": [], "cells": []}
            for group, item in half:
                smaller[group].append(item)
            yield from _request_with_fallback(options, smaller, rules, image)


def _payload_batches(
    payload: dict[str, list[dict[str, Any]]], rules: str, limit: int
) -> list[dict[str, list[dict[str, Any]]]]:
    """Structure payloadをcontext文字数以内の要素境界で分割する。

    Args:
        payload: 1ページ分のtextsとcells。
        rules: promptへ含める外部ルール。
        limit: request文字数上限。

    Returns:
        順序を維持したpayload配列。
    """

    budget = max(1, limit - len(rules) - 2500)
    batches: list[dict[str, list[dict[str, Any]]]] = []
    current = {"texts": [], "cells": []}
    for group in ("texts", "cells"):
        for item in payload[group]:
            candidate = {
                "texts": list(current["texts"]),
                "cells": list(current["cells"]),
            }
            candidate[group].append(item)
            if sum(
                len(json.dumps(value, ensure_ascii=False))
                for value in candidate.values()
            ) > budget and any(current.values()):
                batches.append(current)
                current = {"texts": [], "cells": []}
            current[group].append(item)
    if any(current.values()):
        batches.append(current)
    return batches or [{"texts": [], "cells": []}]


def structure_stage(state: PipelineState) -> PipelineState:
    """VLMで文書構造を補正するLangGraph node。

    Args:
        state: Normalize成果物を含むgraph state。

    Returns:
        Structure成果物パスを設定した部分state。
    """

    options, paths = state_options(state), state_paths(state)
    rules = "" if options.skip_vlm else read_rules(options.structure_rules)
    input_hash = hash_file(paths.normalized_json)
    config_hash = hash_json(
        {
            "version": 1,
            "skip": options.skip_vlm,
            "rules": rules,
            "context_chars": options.context_chars,
            "model": None if options.skip_vlm else os.getenv("OPENAI_MODEL"),
        }
    )
    if stage_cached(
        paths.manifest, "structure", input_hash, config_hash, paths.structured_json
    ):
        LOGGER.info("Resumed StructureStage output=%s", paths.structured_json)
        return {
            "current_path": str(paths.structured_json),
            "completed_stage": "structure",
        }
    source = read_json(paths.normalized_json)
    partial = stage_partial(
        paths.manifest,
        "structure",
        input_hash,
        config_hash,
        paths.structured_json,
    )
    document = read_json(paths.structured_json) if partial else copy.deepcopy(source)
    stage_state = (
        read_json(paths.manifest).get("stages", {}).get("structure", {})
        if partial
        else {}
    )
    completed = set(stage_state.get("completed_elements", []))
    applied = int(stage_state.get("patches", 0))
    all_payloads: list[tuple[int | None, dict[str, list[dict[str, Any]]]]] = []
    if not options.skip_vlm:
        elements = [*source.get("texts", []), *source.get("tables", [])]
        pages = list(
            dict.fromkeys(_page_no(item) for item in elements if isinstance(item, dict))
        )
        for page in pages:
            payload = _page_payload(source, page)
            for group in ("texts", "cells"):
                payload[group] = [
                    item for item in payload[group] if item.get("ref") not in completed
                ]
            all_payloads.extend(
                (page, batch)
                for batch in _payload_batches(payload, rules, options.context_chars)
                if any(batch.values())
            )
    total = len(completed) + sum(
        len(_batch_refs(batch)) for _page, batch in all_payloads
    )
    record_stage(
        paths.manifest,
        "structure",
        "running",
        input_hash,
        config_hash,
        paths.structured_json,
        {
            "total": total,
            "completed": len(completed),
            "completed_elements": sorted(completed),
            "patches": applied,
        },
    )
    if not options.skip_vlm:
        for page, batch in all_payloads:
            image = _page_image(document, paths.output_dir, page)
            for completed_batch, patches in _request_with_fallback(
                options, batch, rules, image
            ):
                applied += _apply(document, patches)
                completed.update(_batch_refs(completed_batch))
                write_json(paths.structured_json, document)
                record_stage(
                    paths.manifest,
                    "structure",
                    "running",
                    input_hash,
                    config_hash,
                    paths.structured_json,
                    {
                        "total": total,
                        "completed": len(completed),
                        "completed_elements": sorted(completed),
                        "patches": applied,
                    },
                )
    write_json(paths.structured_json, document)
    record_stage(
        paths.manifest,
        "structure",
        "completed",
        input_hash,
        config_hash,
        paths.structured_json,
        {
            "total": total,
            "completed": len(completed),
            "completed_elements": sorted(completed),
            "patches": applied,
            "vlm_skipped": options.skip_vlm,
        },
    )
    return {"current_path": str(paths.structured_json), "completed_stage": "structure"}
