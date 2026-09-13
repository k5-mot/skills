"""小さいcontext windowへ収める保守的な予算計算を提供する。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContextBudget:
    """LLM要求で使用可能な概算token予算を表す。"""

    total: int = 50_000
    output: int = 8_192
    image: int = 4_096

    def input_limit(self, has_image: bool = False) -> int:
        """固定予約を除いた入力上限を返す。

        Args:
            has_image: 画像messageを含むかどうか。

        Returns:
            入力に利用できる概算token数。
        """

        return self.total - self.output - (self.image if has_image else 0)


def approximate_tokens(*values: str) -> int:
    """文字数を保守的な概算token数として合計する。

    Args:
        values: promptを構成する文字列。

    Returns:
        Unicode code point数の合計。
    """

    return sum(len(value) for value in values)


def split_units(
    units: list[tuple[str, str]], limit: int, fixed_tokens: int = 0
) -> list[list[tuple[str, str]]]:
    """安定ID付き要素を上限内の連続chunkへ分ける。

    Args:
        units: IDと本文の組。
        limit: 一要求の入力上限。
        fixed_tokens: 全chunkに加算されるprompt等の概算量。

    Returns:
        文書順を保ったchunk列。

    Raises:
        ValueError: 単一要素が上限へ収まらない場合。
    """

    capacity = limit - fixed_tokens
    if capacity < 1:
        raise ValueError("fixed prompt exceeds context budget")
    chunks: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    used = 0
    for item_id, text in units:
        size = approximate_tokens(item_id, text)
        if size > capacity:
            raise ValueError(f"indivisible element exceeds context budget: {item_id}")
        if current and used + size > capacity:
            chunks.append(current)
            current = []
            used = 0
        current.append((item_id, text))
        used += size
    if current:
        chunks.append(current)
    return chunks


def fit_neighbor_context(
    previous: str, following: str, remaining: int
) -> tuple[str, str]:
    """前後ページ文脈を同じ比率で入力残量へ切り詰める。

    Args:
        previous: 前ページ原文。
        following: 次ページ原文。
        remaining: 文脈に利用できる概算token数。

    Returns:
        上限内へ切り詰めた前後文脈。
    """

    if remaining <= 0:
        return "", ""
    half = remaining // 2
    previous_part = previous[-half:] if half else ""
    following_part = following[: remaining - len(previous_part)]
    unused = remaining - len(previous_part) - len(following_part)
    if unused > 0:
        previous_part = previous[-(len(previous_part) + unused) :]
    return previous_part, following_part
