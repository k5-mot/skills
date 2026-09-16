"""ページ画像を根拠に限定的な構造補正を適用する。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.adapters.llm import structured_chat
from src.adapters.langfuse import observed, update_current
from src.config import Settings
from src.model import Page, page_text

STRUCTURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "patches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "kind": {
                        "type": ["string", "null"],
                        "enum": [
                            "paragraph",
                            "heading",
                            "blockquote",
                            "alert",
                            "code",
                            None,
                        ],
                    },
                    "level": {"type": ["integer", "null"], "minimum": 1, "maximum": 6},
                    "alert_kind": {
                        "type": ["string", "null"],
                        "enum": [
                            "note",
                            "tip",
                            "important",
                            "warning",
                            "caution",
                            None,
                        ],
                    },
                },
                "required": ["id", "kind", "level", "alert_kind"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["patches"],
    "additionalProperties": False,
}


def _fix_heading_jumps(page: Page) -> None:
    """見出し階層が先頭または直前から飛ばないよう決定的に丸める。

    Args:
        page: Structure patch適用後のページ。

    Returns:
        なし。

    Side Effects:
        page内Headingのlevelを文書順に補正する。
    """

    previous = 0
    for block in sorted(page.blocks, key=lambda item: item.order):
        if block.kind != "heading":
            continue
        allowed = 1 if previous == 0 else min(previous + 1, 6)
        level = block.level if block.level is not None else allowed
        block.level = max(1, min(level, allowed))
        previous = block.level


@observed("structure-page", capture_input=False)
def structure_page(page: Page, image: Path, rules: str, settings: Settings) -> Page:
    """LLMの限定patchでページ構造を補正する。

    Args:
        page: 正規化済みページ。
        image: 同じPDFページの画像。
        rules: Structure専用Rules。
        settings: Structure modelとAPI設定。

    Returns:
        許可されたpatchだけを適用したページcopy。

    Raises:
        ValueError: modelが未知IDを返す場合。
    """

    update_current(
        input={"page": page.model_dump(), "image": str(image), "rules": rules}
    )
    if not settings.structure_model:
        raise ValueError("OPENAI_STRUCTURE_MODEL is required")
    summary = [
        {
            "id": block.id,
            "kind": block.kind,
            "level": block.level,
            "bbox": block.bbox,
            "text": page_text(Page(number=page.number, blocks=[block]), False),
        }
        for block in page.blocks
    ]
    response = structured_chat(
        settings,
        settings.structure_model,
        "文書画像と抽出要素を比較し、必要な構造補正だけをJSONで返してください。要素の追加・削除・本文変更は禁止です。kindがheadingでなければlevelをnull、kindがalertでなければalert_kindをnullにしてください。",
        f"Structureルール:\n{rules}\n\nページ要素:\n{json.dumps(summary, ensure_ascii=False)}",
        "structure_patches",
        STRUCTURE_SCHEMA,
        image,
    )
    result = page.model_copy(deep=True)
    by_id = {block.id: block for block in result.blocks}
    for patch in response.get("patches", []):
        if not isinstance(patch, dict) or patch.get("id") not in by_id:
            raise ValueError(f"Structure returned unknown id: {patch!r}")
        block = by_id[str(patch["id"])]
        kind = patch.get("kind")
        if kind is not None:
            block.kind = kind
        level = patch.get("level")
        if block.kind == "heading" and level is not None:
            block.level = int(level)
        elif block.kind != "heading":
            block.level = None
        alert_kind = patch.get("alert_kind")
        if block.kind == "alert" and alert_kind is not None:
            block.alert_kind = alert_kind
        elif block.kind != "alert":
            block.alert_kind = None
    _fix_heading_jumps(result)
    update_current(output=result.model_dump())
    return result
