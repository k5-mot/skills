"""共有I/O、hash、manifest、ログ処理を提供する。"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv

LOGGER = logging.getLogger("translate-ja-v3")
GLOSSARY_FIELDS = (
    "english-short",
    "english-long",
    "japanse-short",
    "japanese-long",
    "kind",
    "description",
    "note",
    "reference",
)


class ColorFormatter(logging.Formatter):
    """level名だけをANSI色で表示するformatter。"""

    COLORS = {
        logging.DEBUG: "\033[36m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[35m",
    }

    def format(self, record: logging.LogRecord) -> str:
        """level名を着色してログ文字列を作る。

        Args:
            record: loggingが生成したrecord。

        Returns:
            ANSI色付きのログ文字列。
        """

        original = record.levelname
        color = self.COLORS.get(record.levelno, "")
        record.levelname = f"{color}{original}\033[0m" if color else original
        try:
            return super().format(record)
        finally:
            record.levelname = original


def configure_logging() -> None:
    """環境変数LOG_LEVELを使って英語ログを設定する。

    Returns:
        なし。

    Side Effects:
        root loggerのhandlerとlevelを更新する。
    """

    level = getattr(logging, os.getenv("LOG_LEVEL", "DEBUG").upper(), logging.DEBUG)
    handler = logging.StreamHandler()
    handler.setFormatter(
        ColorFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logging.basicConfig(level=level, handlers=[handler], force=True)
    for noisy_logger in ("httpcore", "httpx", "openai"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)


def load_environment(path: Path) -> None:
    """dotenvを既存環境変数を上書きせず読み込む。

    Args:
        path: 読み込むdotenvファイル。

    Returns:
        なし。
    """

    load_dotenv(path, override=False)


def read_json(path: Path) -> dict[str, Any]:
    """JSON objectをUTF-8で読み込む。

    Args:
        path: 読み込むJSONファイル。

    Returns:
        JSON object。

    Raises:
        ValueError: rootがobjectでない場合。
    """

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def write_bytes(path: Path, value: bytes) -> None:
    """bytesを同一directory内の一時ファイル経由で保存する。

    Args:
        path: 保存先。
        value: 保存するbytes。

    Returns:
        なし。

    Side Effects:
        保存先をatomicに置換する。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path: Path, value: dict[str, Any]) -> None:
    """JSON objectを整形してatomic保存する。

    Args:
        path: 保存先。
        value: 保存するJSON object。

    Returns:
        なし。
    """

    write_bytes(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def hash_bytes(value: bytes) -> str:
    """bytesのSHA-256を返す。

    Args:
        value: hash対象。

    Returns:
        16進SHA-256文字列。
    """

    return hashlib.sha256(value).hexdigest()


def hash_file(path: Path) -> str:
    """ファイルのSHA-256を返す。

    Args:
        path: hash対象ファイル。

    Returns:
        16進SHA-256文字列。
    """

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_json(value: Any) -> str:
    """JSON互換値のcanonical SHA-256を返す。

    Args:
        value: hash対象。

    Returns:
        16進SHA-256文字列。
    """

    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hash_bytes(payload.encode())


def hash_paths(paths: Iterable[Path]) -> str:
    """複数ファイルのパスと内容からSHA-256を作る。

    Args:
        paths: hash対象ファイル群。

    Returns:
        16進SHA-256文字列。
    """

    values = [(str(path), hash_file(path)) for path in paths if path.is_file()]
    return hash_json(values)


def read_rules(path: Path | None, default: str = "") -> str:
    """任意の外部ルールを読み込む。

    Args:
        path: UTF-8ルールファイル。Noneならdefaultを使う。
        default: 外部ファイル未指定時の値。

    Returns:
        前後空白を除いたルール本文。
    """

    return default if path is None else path.read_text(encoding="utf-8").strip()


def read_glossary(path: Path | None) -> list[dict[str, str]]:
    """現行schemaのCSV用語集を読み込む。

    Args:
        path: CSVパス。Noneなら空配列。

    Returns:
        有効な用語行。

    Raises:
        ValueError: 必須列が不足する場合。
    """

    if path is None:
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        missing = set(GLOSSARY_FIELDS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"glossary is missing columns: {sorted(missing)}")
        rows = [
            {field: str(row.get(field) or "").strip() for field in GLOSSARY_FIELDS}
            for row in reader
        ]
    return [
        row
        for row in rows
        if (row["english-short"] or row["english-long"])
        and (row["japanse-short"] or row["japanese-long"])
    ]


def glossary_matches(text: str, glossary: list[dict[str, str]]) -> list[dict[str, str]]:
    """原文に一致する用語だけをprompt用schemaで返す。

    Args:
        text: 検索対象の原文。
        glossary: 全用語行。

    Returns:
        noteとreferenceを除いた一致用語行。
    """

    lowered = text.casefold()
    return [
        {key: value for key, value in row.items() if key not in {"note", "reference"}}
        for row in glossary
        if any(
            term and term.casefold() in lowered
            for term in (row["english-short"], row["english-long"])
        )
    ]


def stage_cached(
    manifest_path: Path,
    stage: str,
    input_hash: str,
    config_hash: str,
    output_path: Path,
) -> bool:
    """manifestと成果物hashからStageを再利用できるか判定する。

    Args:
        manifest_path: manifest.json。
        stage: Stage識別子。
        input_hash: 現在の入力hash。
        config_hash: 現在の設定hash。
        output_path: 期待する成果物。

    Returns:
        完了成果物を安全に再利用できる場合はTrue。
    """

    if not manifest_path.is_file() or not output_path.is_file():
        return False
    state = read_json(manifest_path).get("stages", {}).get(stage, {})
    return bool(
        state.get("status") == "completed"
        and state.get("input_sha256") == input_hash
        and state.get("config_sha256") == config_hash
        and state.get("output_sha256") == hash_file(output_path)
    )


def stage_partial(
    manifest_path: Path,
    stage: str,
    input_hash: str,
    config_hash: str,
    output_path: Path,
) -> bool:
    """同じ入力と設定で保存された実行中成果物か判定する。

    Args:
        manifest_path: manifest.json。
        stage: Stage識別子。
        input_hash: 現在の入力hash。
        config_hash: 現在の設定hash。
        output_path: 部分成果物。

    Returns:
        安全に要素単位Resumeできる場合はTrue。
    """

    if not manifest_path.is_file() or not output_path.is_file():
        return False
    value = read_json(manifest_path).get("stages", {}).get(stage, {})
    return bool(
        value.get("status") == "running"
        and value.get("input_sha256") == input_hash
        and value.get("config_sha256") == config_hash
    )


def record_stage(
    manifest_path: Path,
    stage: str,
    status: str,
    input_hash: str,
    config_hash: str,
    output_path: Path,
    details: dict[str, Any] | None = None,
) -> None:
    """Stage状態をmanifestへ保存する。

    Args:
        manifest_path: manifest.json。
        stage: Stage識別子。
        status: running、completed、skippedのいずれか。
        input_hash: 入力hash。
        config_hash: 設定hash。
        output_path: Stage成果物。
        details: 追加の監査情報。

    Returns:
        なし。
    """

    manifest = read_json(manifest_path) if manifest_path.is_file() else {}
    manifest.setdefault("schema_version", 3)
    stages = manifest.setdefault("stages", {})
    value: dict[str, Any] = {
        "status": status,
        "input_sha256": input_hash,
        "config_sha256": config_hash,
        "output": str(output_path),
    }
    if output_path.is_file():
        value["output_sha256"] = hash_file(output_path)
    if details:
        value.update(details)
    stages[stage] = value
    manifest["stage"] = stage
    write_json(manifest_path, manifest)
    log = LOGGER.info if status in {"completed", "skipped"} else LOGGER.debug
    log("Stage %s stage=%s output=%s", status, stage, output_path)


def text_of(item: dict[str, Any]) -> str:
    """Docling要素から代表textを返す。

    Args:
        item: Docling JSON要素。

    Returns:
        text、content、origの最初の文字列値。
    """

    for key in ("text", "content", "orig"):
        value = item.get(key)
        if isinstance(value, str):
            return value
    return ""


def self_ref(item: dict[str, Any], group: str, index: int) -> str:
    """Docling要素のrefを安定して返す。

    Args:
        item: Docling JSON要素。
        group: collection名。
        index: collection内index。

    Returns:
        既存self_refまたは生成したJSON pointer。
    """

    value = item.get("self_ref")
    return value if isinstance(value, str) else f"#/{group}/{index}"
