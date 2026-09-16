"""公開CLI、環境設定、同梱templateを検証する。"""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from src.config import Settings, load_settings, read_rules

CLI_PATH = Path(__file__).parents[1] / "translate.py"
CLI_SPEC = importlib.util.spec_from_file_location("translate_ja_v5_cli", CLI_PATH)
if CLI_SPEC is None or CLI_SPEC.loader is None:
    raise RuntimeError(f"CLI moduleを読めません: {CLI_PATH}")
CLI_MODULE = importlib.util.module_from_spec(CLI_SPEC)
CLI_SPEC.loader.exec_module(CLI_MODULE)
app = CLI_MODULE.app

RUNNER = CliRunner()


def _environment() -> dict[str, str]:
    """全commandを構成できる最小環境変数を返す。

    Returns:
        test用環境変数mapping。
    """

    return {
        "DOCLING_SERVER_URL": "http://docling",
        "OPENAI_BASE_URL": "http://litellm/v1",
        "OPENAI_API_KEY": "key",
        "OPENAI_STRUCTURE_MODEL": "structure",
        "OPENAI_TRANSLATION_MODEL": "translation",
        "OPENAI_REVIEW_MODEL": "review",
        "OPENAI_EMBEDDING_MODEL": "embedding",
        "LIBRETRANSLATE_URL": "http://libre",
        "QDRANT_URI": "http://qdrant",
        "QDRANT_COLLECTION": "review",
    }


def test_cli_exposes_only_three_subcommands() -> None:
    """helpに公開目的の三commandだけが現れることを確認する。

    Returns:
        なし。
    """

    result = RUNNER.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert all(name in result.stdout for name in ("translate", "review", "register"))
    assert "normalize" not in result.stdout


def test_cli_rejects_unknown_command() -> None:
    """内部Stage名をsubcommandとして受け付けないことを確認する。

    Returns:
        なし。
    """

    assert RUNNER.invoke(app, ["parse"]).exit_code != 0


def test_translate_cli_requires_source() -> None:
    """translateでsource省略がCLI errorになることを確認する。

    Returns:
        なし。
    """

    assert RUNNER.invoke(app, ["translate"]).exit_code != 0


def test_register_cli_requires_exactly_one_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """registerがfileとdirectoryの排他指定を検証することを確認する。

    Args:
        monkeypatch: 環境とworkflowを差し替えるfixture。

    Returns:
        なし。
    """

    for key, value in _environment().items():
        monkeypatch.setenv(key, value)
    result = RUNNER.invoke(app, ["register"])
    assert result.exit_code != 0
    assert "exactly one" in str(result.exception)


def test_settings_accept_optional_services_absent() -> None:
    """LangfuseとQdrant未設定でも翻訳設定を構築できることを確認する。

    Returns:
        なし。
    """

    env = _environment()
    for key in ("QDRANT_URI", "QDRANT_COLLECTION", "OPENAI_EMBEDDING_MODEL"):
        env.pop(key)
    settings = load_settings("translate", env=env)
    assert not settings.langfuse_enabled
    assert not settings.qdrant_enabled


def test_settings_do_not_accept_legacy_service_aliases() -> None:
    """旧Docling・Qdrant変数を正規変数の代用として扱わない。

    Returns:
        なし。
    """

    env = _environment()
    env["DOCLING_URL"] = env.pop("DOCLING_SERVER_URL")
    with pytest.raises(ValueError, match="DOCLING_SERVER_URL"):
        load_settings("translate", env=env)

    env = _environment()
    env["QDRANT_URL"] = env.pop("QDRANT_URI")
    with pytest.raises(ValueError, match="QDRANT_URI"):
        load_settings("register", env=env)


def test_settings_reject_partial_langfuse() -> None:
    """Langfuse認証情報の片方だけを開始前に拒否することを確認する。

    Returns:
        なし。
    """

    env = {**_environment(), "LANGFUSE_PUBLIC_KEY": "public"}
    with pytest.raises(ValueError, match="LANGFUSE_SECRET_KEY"):
        load_settings("translate", env=env)


def test_settings_switches_translation_backend() -> None:
    """LibreTranslate選択時もStructureとReview設定を要求することを確認する。

    Returns:
        なし。
    """

    env = _environment()
    env.pop("OPENAI_TRANSLATION_MODEL")
    settings = load_settings("translate", "libretranslate", env)
    assert settings.libretranslate_url == "http://libre"
    assert settings.structure_model and settings.review_model


def test_review_settings_do_not_require_unused_docling_or_structure() -> None:
    """standalone Reviewが利用しないDoclingとStructure設定を要求しない。

    Returns:
        なし。
    """

    settings = load_settings(
        "review",
        env={
            "OPENAI_BASE_URL": "http://litellm/v1",
            "OPENAI_API_KEY": "key",
            "OPENAI_REVIEW_MODEL": "review",
        },
    )
    assert settings.docling_url is None
    assert settings.structure_model is None


def test_rules_are_fully_separated(settings: Settings) -> None:
    """各工程が自身のRules fileだけを読むことを確認する。

    Args:
        settings: 共通Settings fixture。

    Returns:
        なし。
    """

    assert read_rules(settings, "structure") == "structure"
    assert read_rules(settings, "translation") == "translation"
    assert read_rules(settings, "review") == "review"


def test_bundled_template_is_valid_docx() -> None:
    """同梱templateがWord packageとして読めることを確認する。

    Returns:
        なし。
    """

    template = Path(__file__).parents[1] / "templates" / "template.docx"
    with zipfile.ZipFile(template) as archive:
        assert "word/styles.xml" in archive.namelist()
