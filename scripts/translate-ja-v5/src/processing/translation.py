"""ページ単位の翻訳と翻訳層の適用を提供する。"""

from __future__ import annotations

import json
from typing import Any, Literal

from src.adapters.langfuse import observed, update_current
from src.adapters.libretranslate import protect_text, restore_text, translate_texts
from src.adapters.llm import ContextLengthError, structured_chat
from src.budget import (
    ContextBudget,
    approximate_tokens,
    fit_neighbor_context,
    split_units,
)
from src.config import Backend, Settings
from src.model import Inline, Page
from src.processing.quality import GlossaryEntry

TRANSLATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}


def _iter_inline_groups(page: Page) -> list[tuple[str, list[Inline]]]:
    """翻訳可能なInline containerを安定ID付きで列挙する。

    Args:
        page: 対象ページ。

    Returns:
        container種別IDと原文Inline列の組。
    """

    groups: list[tuple[str, list[Inline]]] = []
    for block in page.blocks:
        if block.kind not in {"code", "formula", "horizontal_rule"} and block.source:
            groups.append((block.id, block.source))
        if block.caption:
            groups.append((f"{block.id}/caption", block.caption))
        for cell in block.cells:
            groups.append((f"{block.id}/cell/{cell.row}/{cell.column}", cell.source))
    return groups


def translation_units(page: Page) -> list[tuple[str, str]]:
    """ページから翻訳対象の通常text/link Inlineだけを集める。

    Args:
        page: 対象ページ。

    Returns:
        Inline IDと原文の組。
    """

    return [
        (item.id, item.text)
        for _container_id, values in _iter_inline_groups(page)
        for item in values
        if item.kind in {"text", "link"} and item.text.strip()
    ]


def _translated_values(source: list[Inline], mapping: dict[str, str]) -> list[Inline]:
    """原文Inlineの構造を保って翻訳文字列を適用する。

    Args:
        source: 原文Inline列。
        mapping: Inline IDと翻訳文字列の対応。

    Returns:
        marks、URL、保護Inlineを維持した翻訳Inline列。
    """

    values = [item.model_copy(deep=True) for item in source]
    for item in values:
        if item.id in mapping:
            item.text = mapping[item.id]
    return values


def apply_layer(
    page: Page, mapping: dict[str, str], layer: Literal["translated", "reviewed"]
) -> Page:
    """Inline ID対応をページの翻訳またはReview層へ適用する。

    Args:
        page: 対象ページ。
        mapping: Inline IDと新しい文字列の対応。
        layer: 更新する翻訳状態。

    Returns:
        deep copyした更新済みページ。

    Raises:
        ValueError: mappingに対象ページ外IDがある場合。
    """

    result = page.model_copy(deep=True)
    known = {item_id for item_id, _text in translation_units(page)}
    unknown = set(mapping) - known
    if unknown:
        raise ValueError(f"translation returned foreign ids: {sorted(unknown)}")
    for block in result.blocks:
        if block.kind not in {"code", "formula", "horizontal_rule"} and block.source:
            base = (
                block.translated
                if layer == "reviewed" and block.translated is not None
                else block.source
            )
            setattr(block, layer, _translated_values(base, mapping))
        if block.caption:
            base_caption = (
                block.translated_caption
                if layer == "reviewed" and block.translated_caption is not None
                else block.caption
            )
            setattr(
                block, f"{layer}_caption", _translated_values(base_caption, mapping)
            )
        for cell in block.cells:
            base_cell = (
                cell.translated
                if layer == "reviewed" and cell.translated is not None
                else cell.source
            )
            setattr(cell, layer, _translated_values(base_cell, mapping))
    return result


def _translation_prompt(
    rules: str,
    glossary: list[GlossaryEntry],
    units: list[tuple[str, str]],
    previous: str,
    following: str,
) -> str:
    """対象だけを出力する翻訳promptを組み立てる。

    Args:
        rules: Translation専用Rules。
        glossary: 適用用語集。
        units: 対象Inline IDと本文。
        previous: 前ページ原文context。
        following: 次ページ原文context。

    Returns:
        JSON文字列を含むuser prompt。
    """

    return (
        f"Translationルール:\n{rules}\n\n用語集:\n{json.dumps([item.model_dump() for item in glossary], ensure_ascii=False)}"
        f"\n\n前ページ参照（出力禁止）:\n{previous}\n\n次ページ参照（出力禁止）:\n{following}"
        f"\n\n翻訳対象JSON:\n{json.dumps([{'id': item_id, 'text': text} for item_id, text in units], ensure_ascii=False)}"
    )


def _openai_translate_chunk(
    settings: Settings,
    units: list[tuple[str, str]],
    rules: str,
    glossary: list[GlossaryEntry],
    previous: str,
    following: str,
    allow_bisect: bool = True,
) -> dict[str, str]:
    """一つの安全なchunkをOpenAI互換APIで翻訳する。

    Args:
        settings: Translation model設定。
        units: 対象Inline列。
        rules: Translation Rules。
        glossary: 用語集。
        previous: 前ページcontext。
        following: 次ページcontext。
        allow_bisect: context error時の一回二分を許すか。

    Returns:
        IDと日本語訳の対応。

    Raises:
        ValueError: response ID集合や訳文が不正な場合。
    """

    if not settings.translation_model:
        raise ValueError("OPENAI_TRANSLATION_MODEL is required")
    prepared = {item_id: protect_text(text) for item_id, text in units}
    protected_units = [(item_id, prepared[item_id][0]) for item_id, _text in units]
    try:
        response = structured_chat(
            settings,
            settings.translation_model,
            "英語を正確な日本語へ翻訳してください。JSONには対象IDだけを一度ずつ含め、コード、URL、パス、識別子を変更しないでください。",
            _translation_prompt(rules, glossary, protected_units, previous, following),
            "translations",
            TRANSLATION_SCHEMA,
        )
    except ContextLengthError:
        if not allow_bisect or len(units) < 2:
            raise
        middle = len(units) // 2
        return {
            **_openai_translate_chunk(
                settings, units[:middle], rules, glossary, previous, "", False
            ),
            **_openai_translate_chunk(
                settings, units[middle:], rules, glossary, "", following, False
            ),
        }
    values = response.get("translations")
    if not isinstance(values, list):
        raise ValueError("translation response must contain translations")
    mapping = {
        str(item.get("id")): str(item.get("text", "")).strip()
        for item in values
        if isinstance(item, dict)
    }
    expected = {item_id for item_id, _text in units}
    if set(mapping) != expected or any(not text for text in mapping.values()):
        raise ValueError("translation response IDs must exactly match non-empty inputs")
    return {
        item_id: restore_text(text, prepared[item_id][1])
        for item_id, text in mapping.items()
    }


@observed("translate-page", capture_input=False)
def translate_page(
    page: Page,
    previous: str,
    following: str,
    rules: str,
    glossary: list[GlossaryEntry],
    settings: Settings,
    backend: Backend,
) -> Page:
    """前後contextを参照しながら対象ページだけを翻訳する。

    Args:
        page: 翻訳対象ページ。
        previous: 前ページ原文。
        following: 次ページ原文。
        rules: Translation Rules。
        glossary: 用語集。
        settings: backendとcontext設定。
        backend: openaiまたはlibretranslate。

    Returns:
        translated層を持つページ。
    """

    update_current(
        input={
            "page": page.model_dump(),
            "previous": previous,
            "following": following,
            "rules": rules,
            "glossary": [item.model_dump() for item in glossary],
        }
    )
    units = translation_units(page)
    if not units:
        result = apply_layer(page, {}, "translated")
        update_current(output=result.model_dump())
        return result
    budget = ContextBudget(
        settings.context_tokens, settings.output_tokens, settings.image_tokens
    )
    fixed = (
        approximate_tokens(
            rules,
            json.dumps([item.model_dump() for item in glossary], ensure_ascii=False),
        )
        + 1_000
    )
    chunks = split_units(units, budget.input_limit(), fixed)
    mapping: dict[str, str] = {}
    for chunk in chunks:
        current_size = sum(approximate_tokens(item_id, text) for item_id, text in chunk)
        prev_part, next_part = fit_neighbor_context(
            previous, following, budget.input_limit() - fixed - current_size
        )
        if backend == "libretranslate":
            if not settings.libretranslate_url:
                raise ValueError("LIBRETRANSLATE_URL is required")
            translated = translate_texts(
                settings.libretranslate_url,
                settings.libretranslate_api_key,
                [text for _item_id, text in chunk],
            )
            mapping.update(
                {
                    item_id: text
                    for (item_id, _source), text in zip(chunk, translated, strict=True)
                }
            )
        else:
            mapping.update(
                _openai_translate_chunk(
                    settings, chunk, rules, glossary, prev_part, next_part
                )
            )
    result = apply_layer(page, mapping, "translated")
    update_current(output=result.model_dump())
    return result
