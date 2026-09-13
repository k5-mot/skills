"""translate-ja-v5 testで共有する最小fixtureを提供する。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from src.config import Settings  # noqa: E402


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """外部接続先をdummy化した完全な設定を返す。

    Args:
        tmp_path: pytest一時directory。

    Returns:
        unit test向けSettings。
    """

    templates = tmp_path / "templates"
    templates.mkdir()
    for name in ("structure", "translation", "review"):
        (templates / f"{name}-rules.md").write_text(name, encoding="utf-8")
    (templates / "template.docx").write_bytes(b"template")
    return Settings(
        docling_url="http://docling",
        docling_api_key=None,
        openai_base_url="http://litellm/v1",
        openai_api_key="key",
        structure_model="structure",
        translation_model="translation",
        review_model="review",
        embedding_model="embedding",
        context_tokens=50_000,
        output_tokens=8_192,
        image_tokens=4_096,
        libretranslate_url="http://libre",
        libretranslate_api_key=None,
        langfuse_public_key=None,
        langfuse_secret_key=None,
        langfuse_base_url=None,
        qdrant_url=None,
        qdrant_api_key=None,
        qdrant_collection=None,
        templates_dir=templates,
    )
