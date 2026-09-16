"""Resume状態と成果物を安全に永続化する。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from types import TracebackType
from typing import Any

import portalocker

STAGES = ("parse", "normalize", "structure", "translate", "review", "markdown", "docx")


def sha256_file(path: Path) -> str:
    """file内容のSHA-256を計算する。

    Args:
        path: 読み込むfile。

    Returns:
        16進SHA-256文字列。
    """

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: Path, value: str) -> None:
    """文字列を同一directory内でatomicに置換保存する。

    Args:
        path: 保存先。
        value: UTF-8で保存する文字列。

    Returns:
        なし。

    Side Effects:
        親directoryを作成し、既存fileをatomicに置換する。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    # os.replaceのatomic性を保つため、一時fileは必ず置換先と同じdirectoryへ作る。
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            # stateだけ先に見えて成果物が未永続化、というResume時の不整合を避ける。
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    """JSON値を整形してatomic保存する。

    Args:
        path: 保存先。
        value: JSON化可能な値。

    Returns:
        なし。
    """

    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def load_json(path: Path, default: Any = None) -> Any:
    """JSON fileを読み、存在しなければ既定値を返す。

    Args:
        path: 読み込むfile。
        default: fileが存在しない場合の値。

    Returns:
        復号したJSON値または既定値。
    """

    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


class OutputLock:
    """同じ作業directoryへの重複実行をdirectory lockで防ぐ。"""

    def __init__(self, path: Path) -> None:
        """lock先を初期化する。

        Args:
            path: lockする作業directory。

        Returns:
            なし。
        """

        self.path = path
        self._lock: portalocker.Lock | None = None

    def __enter__(self) -> OutputLock:
        """non-blocking排他lockを取得する。

        Returns:
            lock取得済みinstance。

        Raises:
            RuntimeError: 他processが同じ出力先を使用中の場合。
        """

        self.path.mkdir(parents=True, exist_ok=True)
        lock = portalocker.Lock(self.path / "run.lock", mode="a+b", timeout=0)
        try:
            lock.acquire()
        except portalocker.AlreadyLocked as error:
            raise RuntimeError(f"output is already in use: {self.path}") from error
        self._lock = lock
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """OS lockとfile descriptorを解放する。

        Args:
            exc_type: context内例外の型。
            exc_value: context内例外。
            traceback: context内例外のtraceback。

        Returns:
            なし。
        """

        if self._lock is not None:
            self._lock.release()
            self._lock = None


def new_state(
    source_hash: str, page_numbers: list[int], config: dict[str, str | None]
) -> dict[str, Any]:
    """新しい翻訳状態を作る。

    Args:
        source_hash: 入力PDFのSHA-256。
        page_numbers: 翻訳本文となるページ番号。
        config: Resume判定へ使うmodelとbackend。

    Returns:
        初期`state.json`値。
    """

    return {
        "schema_version": 1,
        "source_hash": source_hash,
        "config": config,
        "stages": {stage: "pending" for stage in STAGES},
        "pages": {
            str(page): {
                stage: "pending" for stage in ("structure", "translate", "review")
            }
            for page in page_numbers
        },
        "last_error": None,
    }


def invalidate_state(
    state: dict[str, Any], source_hash: str, config: dict[str, str | None], force: bool
) -> dict[str, Any]:
    """入力とmodel差分に基づいて再実行範囲を状態へ反映する。

    Args:
        state: 既存状態。
        source_hash: 現在の入力SHA-256。
        config: 現在のmodelとbackend。
        force: 全工程を無効化するかどうか。

    Returns:
        更新した状態。

    Raises:
        ValueError: 入力が変わりforceがない場合。
    """

    # 異なるPDFへ過去のページ成果物を流用することは、明示的なforce時だけ許す。
    if state.get("source_hash") != source_hash:
        if not force:
            raise ValueError("input PDF changed; use --force to rebuild")
        for stage in STAGES:
            state["stages"][stage] = "pending"
        for page in state.get("pages", {}).values():
            for stage in ("structure", "translate", "review"):
                page[stage] = "pending"
        state["source_hash"] = source_hash
    # 複数設定が変わった場合は、最も上流の変更点から後続をまとめて無効化する。
    starts: list[int] = []
    old = state.get("config", {})
    if force:
        starts.append(0)
    if old.get("structure_model") != config.get("structure_model"):
        starts.append(STAGES.index("structure"))
    if old.get("translation_model") != config.get("translation_model") or old.get(
        "backend"
    ) != config.get("backend"):
        starts.append(STAGES.index("translate"))
    if old.get("glossary_hash") != config.get("glossary_hash"):
        starts.append(STAGES.index("translate"))
    if old.get("review_model") != config.get("review_model"):
        starts.append(STAGES.index("review"))
    if starts:
        start = min(starts)
        for stage in STAGES[start:]:
            state["stages"][stage] = "pending"
        for page in state.get("pages", {}).values():
            for stage in ("structure", "translate", "review"):
                if STAGES.index(stage) >= start:
                    page[stage] = "pending"
    state["config"] = config
    state["last_error"] = None
    return state


def compact_ranges(values: list[int]) -> str:
    """整数列を連続範囲の短い表示へ変換する。

    Args:
        values: ページ番号列。

    Returns:
        `2-4,7`形式の文字列。空なら`-`。
    """

    if not values:
        return "-"
    numbers = sorted(set(values))
    ranges: list[str] = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def progress_lines(state: dict[str, Any]) -> list[str]:
    """状態からStage集計と再利用・実行・再試行範囲を作る。

    Args:
        state: 表示対象の翻訳状態。

    Returns:
        CLIへ一行ずつ表示する文字列列。
    """

    pages = state.get("pages", {})
    lines = ["Stage       Done Pending Failed"]
    for stage in ("structure", "translate", "review"):
        statuses = [value.get(stage, "pending") for value in pages.values()]
        lines.append(
            f"{stage:<11} {statuses.count('done'):>4} {statuses.count('pending'):>7} "
            f"{statuses.count('failed'):>6}"
        )
        reuse = [
            int(page) for page, value in pages.items() if value.get(stage) == "done"
        ]
        run = [
            int(page) for page, value in pages.items() if value.get(stage) == "pending"
        ]
        retry = [
            int(page) for page, value in pages.items() if value.get(stage) == "failed"
        ]
        lines.append(
            f"  Reuse {compact_ranges(reuse)} / Run {compact_ranges(run)} / "
            f"Retry {compact_ranges(retry)}"
        )
    return lines
