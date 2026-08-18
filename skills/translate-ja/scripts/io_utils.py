"""translate-ja のファイル入出力とログ補助を提供する。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)(x-api-key:\s*)[^\s]+"),
)


def utc_now_iso() -> str:
    """UTC の ISO 8601 文字列を返す。"""

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def configure_logging(level_name: str | None = None) -> None:
    """標準 logger の出力形式とレベルを設定する。"""

    level = getattr(
        logging,
        (level_name or os.environ.get("LOG_LEVEL") or "INFO").upper(),
        logging.INFO,
    )
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )


def mask_secret(value: Any) -> str:
    """ログに出す文字列から秘密値らしい断片をマスクする。"""

    text = str(value)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(
            lambda match: match.group(1) + "***" if match.groups() else "***", text
        )
    return text


def ensure_parent(path: str | Path) -> None:
    """指定パスの親ディレクトリを作成する。"""

    Path(path).parent.mkdir(parents=True, exist_ok=True)


def ensure_dir(path: str | Path) -> Path:
    """指定ディレクトリを作成して Path を返す。"""

    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def read_json(path: str | Path) -> Any:
    """UTF-8 JSON ファイルを読み込む。"""

    with Path(path).open("r", encoding="utf-8") as file:
        return json.load(file)


def write_json(path: str | Path, data: Any, *, atomic: bool = True) -> None:
    """UTF-8 JSON ファイルを書き込む。"""

    ensure_parent(path)
    target = Path(path)
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    if atomic:
        atomic_write_text(target, text)
        return
    target.write_text(text, encoding="utf-8")


def atomic_write_text(path: str | Path, text: str) -> None:
    """一時ファイルへ書いてから rename する atomic write を行う。"""

    target = Path(path)
    ensure_parent(target)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """JSONL を読み込み、空行を無視して dict の配列を返す。"""

    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as file:
        for line_no, line in enumerate(file, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"JSONL must contain objects: {path}:{line_no}")
            rows.append(row)
    return rows


def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> None:
    """JSONL を atomic に書き込む。"""

    text = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=False) + "\n" for row in rows
    )
    atomic_write_text(path, text)


def append_jsonl(path: str | Path, row: dict[str, Any]) -> None:
    """JSONL に 1 行追記する。"""

    ensure_parent(path)
    with Path(path).open("a", encoding="utf-8") as file:
        file.write(json.dumps(row, ensure_ascii=False, sort_keys=False) + "\n")


def sha256_file(path: str | Path) -> str:
    """ファイルの sha256 を返す。"""

    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    """文字列の sha256 を返す。"""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_path(path: str | Path) -> str:
    """ファイルまたはディレクトリの内容に基づく sha256 を返す。"""

    target = Path(path)
    if target.is_file():
        return sha256_file(target)
    digest = hashlib.sha256()
    for child in sorted(p for p in target.rglob("*") if p.is_file()):
        digest.update(str(child.relative_to(target)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(child).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def load_existing_json(path: str | Path) -> dict[str, Any] | None:
    """存在する JSON を読み込み、なければ None を返す。"""

    target = Path(path)
    if not target.exists():
        return None
    data = read_json(target)
    if not isinstance(data, dict):
        raise ValueError(f"JSON object expected: {target}")
    return data


def log_jsonl(log_path: str | Path, event: dict[str, Any]) -> None:
    """運用ログ JSONL に 1 イベントを追記する。"""

    safe_event = json.loads(json.dumps(event, ensure_ascii=False, default=str))
    for key, value in list(safe_event.items()):
        if isinstance(value, str):
            safe_event[key] = mask_secret(value)
    safe_event.setdefault("timestamp", utc_now_iso())
    append_jsonl(log_path, safe_event)
