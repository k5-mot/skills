"""NormalizeStageで座標順とDocling参照を補正する。"""

from __future__ import annotations

import copy
from typing import Any, cast

from ..config import PipelineState, state_paths
from ..io import (
    LOGGER,
    hash_file,
    hash_json,
    read_json,
    record_stage,
    stage_cached,
    write_json,
)


def _position(item: dict[str, Any], index: int) -> tuple[int, float, float, int] | None:
    """text要素の読み順sort keyを返す。

    Args:
        item: Docling text要素。
        index: 元の配列index。

    Returns:
        page、上端、左端、元index。座標がなければNone。
    """

    prov = item.get("prov")
    if not isinstance(prov, list) or not prov or not isinstance(prov[0], dict):
        return None
    entry = prov[0]
    bbox = entry.get("bbox")
    if not isinstance(bbox, dict):
        return None
    page = int(entry.get("page_no", 10**9))
    top = float(bbox.get("t", bbox.get("top", 0)))
    bottom = float(bbox.get("b", bbox.get("bottom", 0)))
    left = float(bbox.get("l", bbox.get("left", 0)))
    origin = str(bbox.get("coord_origin", "BOTTOMLEFT")).upper()
    vertical = min(top, bottom) if origin == "TOPLEFT" else -max(top, bottom)
    return page, vertical, left, index


def _replace_refs(value: Any, mapping: dict[str, str]) -> None:
    """JSON内のself_refと$refを新しいindexへ再帰更新する。

    Args:
        value: 更新対象JSON値。
        mapping: 旧refから新refへの対応。

    Returns:
        なし。

    Side Effects:
        渡されたJSON objectを直接更新する。
    """

    if isinstance(value, dict):
        for key, child in value.items():
            if key == "$ref" and isinstance(child, str):
                value[key] = mapping.get(child, child)
            else:
                _replace_refs(child, mapping)
    elif isinstance(value, list):
        for child in value:
            _replace_refs(child, mapping)


def normalize(document: dict[str, Any]) -> dict[str, Any]:
    """座標を持つtextだけを元slot内で読み順へ並べ替える。

    Args:
        document: ParseStageのDocling JSON。

    Returns:
        参照整合済みの正規化JSON。
    """

    result = copy.deepcopy(document)
    texts = result.get("texts")
    if not isinstance(texts, list):
        return result
    located: list[tuple[int, dict[str, Any]]] = []
    for index, value in enumerate(texts):
        item = cast(dict[str, Any], value) if isinstance(value, dict) else None
        if isinstance(item, dict) and _position(item, index):
            located.append((index, item))
    ordered = [
        item
        for index, item in sorted(
            located,
            key=lambda pair: _position(pair[1], pair[0]) or (10**9, 0, 0, pair[0]),
        )
    ]
    slots = [index for index, _item in located]
    previous_refs = {
        id(item): str(item.get("self_ref", f"#/texts/{index}"))
        for index, item in enumerate(texts)
        if isinstance(item, dict)
    }
    for slot, item in zip(slots, ordered, strict=True):
        texts[slot] = item
    mapping: dict[str, str] = {}
    for index, value in enumerate(texts):
        item: Any = value
        if isinstance(item, dict):
            old = previous_refs[id(item)]
            new = f"#/texts/{index}"
            item["self_ref"] = new
            mapping[old] = new
    _replace_refs(result, mapping)
    return result


def normalize_stage(state: PipelineState) -> PipelineState:
    """Docling JSONを座標順へ正規化するLangGraph node。

    Args:
        state: 直前成果物を含むgraph state。

    Returns:
        Normalize成果物パスを設定した部分state。
    """

    paths = state_paths(state)
    source = paths.document_json
    input_hash, config_hash = hash_file(source), hash_json({"version": 1})
    if stage_cached(
        paths.manifest, "normalize", input_hash, config_hash, paths.normalized_json
    ):
        LOGGER.info("Resumed NormalizeStage output=%s", paths.normalized_json)
    else:
        record_stage(
            paths.manifest,
            "normalize",
            "running",
            input_hash,
            config_hash,
            paths.normalized_json,
        )
        write_json(paths.normalized_json, normalize(read_json(source)))
        record_stage(
            paths.manifest,
            "normalize",
            "completed",
            input_hash,
            config_hash,
            paths.normalized_json,
        )
    return {"current_path": str(paths.normalized_json), "completed_stage": "normalize"}
