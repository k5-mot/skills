"""atomic状態更新、排他、無効化、進捗表示を検証する。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.state import (
    OutputLock,
    atomic_write_json,
    compact_ranges,
    invalidate_state,
    new_state,
    progress_lines,
)


def _config(**updates: str) -> dict[str, str | None]:
    """無効化test用のmodel設定を作る。

    Args:
        updates: 上書きする設定。

    Returns:
        既定値へ更新を適用した設定。
    """

    value: dict[str, str | None] = {
        "structure_model": "s",
        "translation_model": "t",
        "review_model": "r",
        "embedding_model": "e",
        "backend": "openai",
    }
    value.update(updates)
    return value


def _done_state() -> dict[str, object]:
    """全ページ完了状態を作る。

    Returns:
        無効化test用状態。
    """

    state = new_state("hash", [2, 3], _config())
    state["stages"] = {key: "done" for key in state["stages"]}
    for page in state["pages"].values():
        page.update({"structure": "done", "translate": "done", "review": "done"})
    return state


def test_atomic_write_json_replaces_complete_document(tmp_path: Path) -> None:
    """atomic保存後に完全なJSONだけが読めることを確認する。

    Args:
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    path = tmp_path / "state.json"
    atomic_write_json(path, {"value": 1})
    atomic_write_json(path, {"value": 2})
    assert json.loads(path.read_text()) == {"value": 2}
    assert not list(tmp_path.glob(".state.json.*"))


def test_output_lock_rejects_second_holder(tmp_path: Path) -> None:
    """同じ出力先の二重lockを即時拒否することを確認する。

    Args:
        tmp_path: pytest一時directory。

    Returns:
        なし。
    """

    path = tmp_path / "work"
    with OutputLock(path):
        assert (path / "run.lock").is_file()
        with pytest.raises(RuntimeError, match="already in use"):
            with OutputLock(path):
                pass
    with OutputLock(path):
        pass


@pytest.mark.parametrize(
    ("updates", "pending"),
    [
        (
            {"structure_model": "s2"},
            {"structure", "translate", "review", "markdown", "docx"},
        ),
        ({"translation_model": "t2"}, {"translate", "review", "markdown", "docx"}),
        ({"review_model": "r2"}, {"review", "markdown", "docx"}),
        ({"embedding_model": "e2"}, set()),
        ({"backend": "libretranslate"}, {"translate", "review", "markdown", "docx"}),
    ],
)
def test_model_changes_invalidate_fixed_range(
    updates: dict[str, str], pending: set[str]
) -> None:
    """modelとbackend変更が固定した後続工程だけを無効化することを確認する。

    Args:
        updates: 変更する設定。
        pending: pendingになるべきStage集合。

    Returns:
        なし。
    """

    state = invalidate_state(_done_state(), "hash", _config(**updates), False)
    assert {
        name for name, status in state["stages"].items() if status == "pending"
    } == pending


def test_input_change_requires_force() -> None:
    """入力hash変更は通常拒否しforce時だけ全無効化することを確認する。

    Returns:
        なし。
    """

    with pytest.raises(ValueError, match="input PDF changed"):
        invalidate_state(_done_state(), "new", _config(), False)
    forced = invalidate_state(_done_state(), "new", _config(), True)
    assert set(forced["stages"].values()) == {"pending"}


def test_progress_uses_compact_ranges() -> None:
    """進捗表示が全ページ列挙でなく連続範囲を使うことを確認する。

    Returns:
        なし。
    """

    state = new_state("hash", [2, 3, 4, 7], _config())
    for number in (2, 3, 4):
        state["pages"][str(number)]["structure"] = "done"
    assert compact_ranges([2, 3, 4, 7]) == "2-4,7"
    assert any("Reuse 2-4" in line for line in progress_lines(state))
