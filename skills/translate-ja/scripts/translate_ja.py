"""日本語翻訳プロンプトと用語辞書を扱う。"""

from __future__ import annotations

import argparse
import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from io_utils import configure_logging, sha256_file

LOGGER = logging.getLogger("translate-ja.translate_ja")


@dataclass(frozen=True)
class GlossaryEntry:
    """翻訳辞書の 1 エントリを表す。"""

    english: str
    japanese: str
    genre: str
    description: str


@dataclass(frozen=True)
class Glossary:
    """翻訳辞書とメタデータを保持する。"""

    path: str | None
    sha256: str | None
    raw_count: int
    effective_count: int
    entries: tuple[GlossaryEntry, ...]


REQUIRED_COLUMNS = ("english", "japanese", "genre", "description")


def load_dictionary_csv(path: str | Path | None) -> Glossary:
    """UTF-8 CSV の用語辞書を読み込み、同一 english は後勝ちで正規化する。"""

    if not path:
        return Glossary(path=None, sha256=None, raw_count=0, effective_count=0, entries=())
    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"dictionary CSV not found: {csv_path}")

    by_english: dict[str, GlossaryEntry] = {}
    raw_count = 0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file, skipinitialspace=True)
        if reader.fieldnames is None:
            raise ValueError(f"dictionary CSV has no header: {csv_path}")
        normalized_fields = [field.strip().strip('"') for field in reader.fieldnames]
        missing = [column for column in REQUIRED_COLUMNS if column not in normalized_fields]
        if missing:
            raise ValueError(f"dictionary CSV missing columns {missing}: {csv_path}")
        reader.fieldnames = normalized_fields
        for row in reader:
            normalized = {key.strip(): (value or "").strip() for key, value in row.items() if key is not None}
            if not any(normalized.values()):
                continue
            english = normalized.get("english", "")
            japanese = normalized.get("japanese", "")
            if not english or not japanese:
                raise ValueError(f"dictionary CSV requires english and japanese: {csv_path}")
            raw_count += 1
            by_english[english] = GlossaryEntry(
                english=english,
                japanese=japanese,
                genre=normalized.get("genre", ""),
                description=normalized.get("description", ""),
            )

    entries = tuple(by_english[key] for key in sorted(by_english))
    return Glossary(
        path=str(csv_path),
        sha256=sha256_file(csv_path),
        raw_count=raw_count,
        effective_count=len(entries),
        entries=entries,
    )


def select_glossary_entries(source_text: str, glossary: Glossary, *, limit: int = 80) -> list[GlossaryEntry]:
    """チャンク本文に現れる辞書項目を優先して返す。"""

    if not glossary.entries:
        return []
    lowered = source_text.lower()
    matched = [entry for entry in glossary.entries if entry.english.lower() in lowered]
    if len(matched) >= limit:
        return matched[:limit]
    seen = {entry.english for entry in matched}
    rest = [entry for entry in glossary.entries if entry.english not in seen]
    return (matched + rest)[:limit]


def format_glossary_for_prompt(entries: list[GlossaryEntry]) -> str:
    """プロンプトに埋め込む辞書テキストを作る。"""

    if not entries:
        return "なし"
    lines = []
    for entry in entries:
        detail = f" / {entry.genre}" if entry.genre else ""
        description = f" / {entry.description}" if entry.description else ""
        lines.append(f"- {entry.english} => {entry.japanese}{detail}{description}")
    return "\n".join(lines)


def build_translation_messages(source_text: str, *, glossary_entries: list[GlossaryEntry]) -> list[dict[str, str]]:
    """構造維持翻訳用の Chat Completions messages を作る。"""

    glossary_text = format_glossary_for_prompt(glossary_entries)
    system = (
        "あなたは軍事・政策文書に強い日英翻訳者です。"
        "原文の意味を保持し、Markdown 構造を壊さず、日本語へ翻訳してください。"
        "原文にない説明、要約、事実追加は禁止です。"
    )
    user = f"""次の Markdown 断片を日本語へ翻訳してください。

厳守事項:
- 見出し記号、箇条書き、表、リンク、画像参照、コード fence を維持する。
- コード、URL、環境変数、ファイルパス、コマンドは翻訳しない。
- 表の列数、区切り行、セル数を維持する。
- 辞書は訳語統一のためだけに使い、原文にない語を追加しない。
- 出力は翻訳済み Markdown 本文だけにする。

用語辞書:
{glossary_text}

原文:
{source_text}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def main() -> int:
    """辞書 CSV を検証する CLI エントリポイント。"""

    configure_logging()
    parser = argparse.ArgumentParser(description="translate-ja glossary utility")
    parser.add_argument("--dictionary-csv", required=True)
    args = parser.parse_args()
    glossary = load_dictionary_csv(args.dictionary_csv)
    LOGGER.info("辞書を読み込みました path=%s raw=%s effective=%s", glossary.path, glossary.raw_count, glossary.effective_count)
    return 0


if __name__ == "__main__":
    started_at = perf_counter()
    try:
        exit_code = main()
    finally:
        LOGGER.info("処理時間 %.3f 秒", perf_counter() - started_at)
    raise SystemExit(exit_code)
