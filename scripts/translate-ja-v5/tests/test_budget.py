"""context予算と安全な分割を検証する。"""

from __future__ import annotations

import pytest

from src.budget import (
    ContextBudget,
    approximate_tokens,
    fit_neighbor_context,
    split_units,
)


def test_budget_reserves_output_and_optional_image() -> None:
    """既定50,000から出力と画像予約が正しく引かれることを確認する。

    Returns:
        なし。
    """

    budget = ContextBudget()
    assert budget.input_limit() == 41_808
    assert budget.input_limit(True) == 37_712
    assert approximate_tokens("日本", "abc") == 5


def test_split_units_keeps_element_boundaries() -> None:
    """複数要素が上限内の連続chunkへ分かれることを確認する。

    Returns:
        なし。
    """

    chunks = split_units([("a", "1234"), ("b", "5678"), ("c", "90")], 10)
    assert chunks == [[("a", "1234"), ("b", "5678")], [("c", "90")]]


def test_split_units_rejects_indivisible_element() -> None:
    """単一要素が入力上限を超える場合にID付きで失敗することを確認する。

    Returns:
        なし。
    """

    with pytest.raises(ValueError, match="oversized"):
        split_units([("oversized", "x" * 20)], 10)


def test_fit_neighbor_context_stays_within_remaining() -> None:
    """前後文脈の合計が残予算を超えないことを確認する。

    Returns:
        なし。
    """

    previous, following = fit_neighbor_context("a" * 20, "b" * 20, 11)
    assert len(previous) + len(following) <= 11
    assert previous.endswith("a") and following.startswith("b")
