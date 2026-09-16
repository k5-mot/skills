"""translate各世代の環境変数契約を固定する。"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[3]
COMMON_ENVIRONMENT = {
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_EMBEDDING_MODEL",
    "DOCLING_SERVER_URL",
    "DOCLING_API_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_OTEL_HOST",
    "LIBRETRANSLATE_URL",
    "LIBRETRANSLATE_API_KEY",
    "QDRANT_API_KEY",
    "QDRANT_URI",
    "QDRANT_COLLECTION",
}
LEGACY_ROOTS = (
    REPOSITORY / "skills/translate-ja/scripts",
    REPOSITORY / "skills/translate-ja-v2/scripts",
    REPOSITORY / "skills/translate-ja-v3/scripts",
    REPOSITORY / "skills/translate-ja-v4/scripts",
)
V5_ROOT = REPOSITORY / "scripts/translate-ja-v5"
ENVIRONMENT_NAME = re.compile(
    r"\b(?:OPENAI|DOCLING|LANGFUSE|LIBRETRANSLATE|QDRANT|LLM|TRANSLATE_JA)_[A-Z0-9_]+\b"
    r"|\b(?:LOG_LEVEL|PYTHON_BIN)\b"
)


def test_python_environment_names_are_whitelisted() -> None:
    """production文字列に契約外の環境変数名がないことを確認する。

    Returns:
        なし。
    """

    failures: list[str] = []
    targets = [
        *((root, COMMON_ENVIRONMENT | {"OPENAI_MODEL"}) for root in LEGACY_ROOTS),
        (
            V5_ROOT / "src",
            COMMON_ENVIRONMENT
            | {
                "OPENAI_STRUCTURE_MODEL",
                "OPENAI_TRANSLATION_MODEL",
                "OPENAI_REVIEW_MODEL",
            },
        ),
    ]
    for root, allowed in targets:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            names = {
                name
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                for name in ENVIRONMENT_NAME.findall(node.value)
            }
            unexpected = sorted(names - allowed)
            if unexpected:
                failures.append(f"{path.relative_to(REPOSITORY)}: {unexpected}")
    assert not failures, "\n".join(failures)


def test_shell_does_not_accept_python_binary_environment_override() -> None:
    """一括実行scriptが追加のPython選択環境変数を受理しないことを確認する。

    Returns:
        なし。
    """

    script = (REPOSITORY / "skills/translate-ja/run.sh").read_text(encoding="utf-8")
    assert "${PYTHON_BIN:-" not in script
