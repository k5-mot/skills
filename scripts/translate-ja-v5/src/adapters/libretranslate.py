"""LibreTranslateで保護文字列を維持した翻訳を行う。"""

from __future__ import annotations

from typing import Any

import httpx

from src.adapters.llm import retry_call
from src.adapters.langfuse import observed, update_current
from src.processing.quality import protected_fragments


def protect_text(text: str) -> tuple[str, dict[str, str]]:
    """翻訳禁止断片を衝突しにくいplaceholderへ置換する。

    Args:
        text: 翻訳対象文字列。

    Returns:
        保護済み文字列とplaceholder mapping。
    """

    mapping: dict[str, str] = {}
    protected = text
    for index, value in enumerate(dict.fromkeys(protected_fragments(text))):
        placeholder = f"__V5_PROTECTED_{index}__"
        mapping[placeholder] = value
        protected = protected.replace(value, placeholder)
    return protected, mapping


def restore_text(text: str, mapping: dict[str, str]) -> str:
    """LibreTranslate応答のplaceholderを原文字列へ戻す。

    Args:
        text: 翻訳済み文字列。
        mapping: placeholderと原文字列の対応。

    Returns:
        保護断片を復元した文字列。

    Raises:
        ValueError: placeholderが欠落または未知の場合。
    """

    restored = text
    for placeholder, value in mapping.items():
        if placeholder not in restored:
            raise ValueError(
                f"LibreTranslate removed protected fragment: {placeholder}"
            )
        restored = restored.replace(placeholder, value)
    if "__V5_PROTECTED_" in restored:
        raise ValueError("LibreTranslate returned an unknown protected fragment")
    return restored


@observed("libretranslate", capture_input=False)
def translate_texts(url: str, api_key: str | None, texts: list[str]) -> list[str]:
    """文字列列をLibreTranslateの一回の配列requestで日本語へ翻訳する。

    Args:
        url: LibreTranslate base URL。
        api_key: 任意API key。
        texts: 翻訳対象文字列列。

    Returns:
        入力順の日本語訳。

    Raises:
        ValueError: response件数または保護断片が不正な場合。
    """

    update_current(input={"url": url, "texts": texts})
    prepared = [protect_text(text) for text in texts]
    payload: dict[str, Any] = {
        "q": [text for text, _mapping in prepared],
        "source": "en",
        "target": "ja",
        "format": "text",
    }
    if api_key:
        payload["api_key"] = api_key

    def invoke() -> httpx.Response:
        """LibreTranslateへ一回のHTTP requestを送る。

        Returns:
            成功したHTTP response。
        """

        response = httpx.post(f"{url.rstrip('/')}/translate", json=payload, timeout=300)
        response.raise_for_status()
        return response

    body = retry_call(invoke).json()
    values = body.get("translatedText") if isinstance(body, dict) else None
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list) or len(values) != len(texts):
        raise ValueError("LibreTranslate response count does not match input")
    result = [
        restore_text(str(value), mapping)
        for value, (_source, mapping) in zip(values, prepared, strict=True)
    ]
    update_current(output={"translations": result})
    return result
