"""CleanStageで非コード本文の連続記号を校正する。"""

from __future__ import annotations

import copy
import re
from typing import Any

from ..config import PipelineState, state_paths
from ..document import iter_table_cells
from ..io import (
    LOGGER,
    hash_file,
    hash_json,
    read_json,
    record_stage,
    stage_cached,
    write_json,
)

CODE_LABELS = {"code", "program_listing"}
HEADING_LABELS = {"title", "section_header", "heading", "header"}


def _collapse(text: str, protected: list[str]) -> str:
    """保護spanを維持して連続するdotと中黒を3文字へ縮める。

    Args:
        text: 校正する本文。
        protected: 変更しないexact span。

    Returns:
        校正済み本文。
    """

    placeholders: dict[str, str] = {}
    for index, span in enumerate(sorted(set(protected), key=len, reverse=True)):
        marker = f"\x00{index}\x00"
        if span:
            text = text.replace(span, marker)
            placeholders[marker] = span
    text = re.sub(r"\.{3,}", "...", text)
    text = re.sub(r"・{3,}", "・・・", text)
    for marker, span in placeholders.items():
        text = text.replace(marker, span)
    return text


def clean(document: dict[str, Any]) -> dict[str, Any]:
    """本文と表セルへ決定論的な記号校正を適用する。

    Args:
        document: StructureStage成果物。

    Returns:
        Clean済みDocling JSON。
    """

    result = copy.deepcopy(document)
    for item in result.get("texts", []):
        if (
            not isinstance(item, dict)
            or item.get("label") in CODE_LABELS | HEADING_LABELS
        ):
            continue
        if isinstance(item.get("text"), str):
            item["text"] = _collapse(item["text"], [])
    for table_index, table in enumerate(result.get("tables", [])):
        if not isinstance(table, dict):
            continue
        for _ref, _path, cell, _row, _column in iter_table_cells(table, table_index):
            spans = cell.get("structure_ja_v4", {}).get("inline_code_spans", [])
            for key in ("text", "content"):
                if isinstance(cell.get(key), str):
                    cell[key] = _collapse(
                        cell[key], spans if isinstance(spans, list) else []
                    )
    return result


def clean_stage(state: PipelineState) -> PipelineState:
    """連続記号を校正するLangGraph node。

    Args:
        state: 直前成果物を含むgraph state。

    Returns:
        Clean成果物パスを設定した部分state。
    """

    paths = state_paths(state)
    input_hash = hash_file(paths.structured_json)
    config_hash = hash_json({"version": 1, "patterns": [".{3,}", "・{3,}"]})
    if stage_cached(
        paths.manifest, "clean", input_hash, config_hash, paths.cleaned_json
    ):
        LOGGER.info("Resumed CleanStage output=%s", paths.cleaned_json)
    else:
        record_stage(
            paths.manifest,
            "clean",
            "running",
            input_hash,
            config_hash,
            paths.cleaned_json,
        )
        write_json(paths.cleaned_json, clean(read_json(paths.structured_json)))
        record_stage(
            paths.manifest,
            "clean",
            "completed",
            input_hash,
            config_hash,
            paths.cleaned_json,
        )
    return {"current_path": str(paths.cleaned_json), "completed_stage": "clean"}
