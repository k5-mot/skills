"""任意のLangfuse計装を主処理から分離する。"""

from __future__ import annotations

import logging
import os
from functools import wraps
from pathlib import Path
from typing import Any, Callable, TypeVar

from langfuse import LangfuseMedia, get_client, observe

LOGGER = logging.getLogger(__name__)
T = TypeVar("T")


def observed(
    name: str, capture_input: bool = True, capture_output: bool = True
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """関数の入出力をLangfuse spanへ記録するdecoratorを返す。

    Args:
        name: span名。
        capture_input: 関数引数を自動記録するかどうか。
        capture_output: 戻り値を自動記録するかどうか。

    Returns:
        Langfuseのobserve decorator。
    """

    def decorate(function: Callable[..., T]) -> Callable[..., T]:
        """認証情報がある呼出しだけをLangfuse decoratorへ渡す。

        Args:
            function: 観測候補の関数。

        Returns:
            未設定時に副作用を起こさないwrapper。
        """

        traced = observe(
            name=name, capture_input=capture_input, capture_output=capture_output
        )(function)

        @wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            """Langfuse設定の有無で元関数または観測関数を呼ぶ。

            Args:
                args: 元関数の位置引数。
                kwargs: 元関数のkeyword引数。

            Returns:
                元関数の戻り値。
            """

            target = traced if _enabled() else function
            return target(*args, **kwargs)

        return wrapper

    return decorate


def _enabled() -> bool:
    """process環境に完全なLangfuse認証情報があるか判定する。

    Returns:
        public keyとsecret keyが両方ある場合はTrue。
    """

    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def media(path: Path) -> LangfuseMedia:
    """ページ画像をLangfuseで記録可能なmedia値へ変換する。

    Args:
        path: 記録する画像file。

    Returns:
        遅延uploadされるLangfuseMedia。
    """

    return LangfuseMedia(file_path=str(path))


def flush_safely() -> None:
    """Langfuse送信をflushし、障害時は警告だけを残す。

    Returns:
        なし。
    """

    if not _enabled():
        return
    try:
        get_client().flush()
    except Exception as error:  # noqa: BLE001
        LOGGER.warning("Langfuseへの送信に失敗しました: %s", error)


def update_current(*, input: Any = None, output: Any = None) -> None:
    """現在のspanへ明示的な安全済み入出力を記録する。

    Args:
        input: 記録する入力値。
        output: 記録する出力値。

    Returns:
        なし。
    """

    if not _enabled():
        return
    try:
        get_client().update_current_span(input=input, output=output)
    except Exception as error:  # noqa: BLE001
        LOGGER.warning("Langfuse spanの更新に失敗しました: %s", error)


def redact_credentials(value: dict[str, Any]) -> dict[str, Any]:
    """trace用mappingから認証情報を再帰的に除外する。

    Args:
        value: 記録候補mapping。

    Returns:
        key名にsecret、token、password、api_keyを含まないcopy。
    """

    blocked = ("secret", "token", "password", "api_key")
    return {
        key: redact_credentials(item) if isinstance(item, dict) else item
        for key, item in value.items()
        if not any(word in key.casefold() for word in blocked)
    }
