"""日本語 Markdown を pandoc で Word docx に変換する。"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from time import perf_counter

from io_utils import configure_logging, log_jsonl

LOGGER = logging.getLogger("translate-ja.convert_md_to_docx")


def convert_markdown_to_docx(
    input_path: Path, output_path: Path, template_path: Path
) -> None:
    """pandoc を subprocess.run で実行し、docx を生成する。"""

    if not input_path.exists():
        raise FileNotFoundError(f"input Markdown not found: {input_path}")
    if not template_path.exists():
        raise FileNotFoundError(f"template.dotx not found: {template_path}")
    if shutil.which("pandoc") is None:
        raise RuntimeError("pandoc is not installed; Markdown output remains available")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "pandoc",
        str(input_path),
        "--from",
        "markdown",
        "--to",
        "docx",
        "--reference-doc",
        str(template_path),
        "--output",
        str(output_path),
    ]
    LOGGER.info(
        "Word 変換を開始します input=%s output=%s template=%s",
        input_path,
        output_path,
        template_path,
    )
    try:
        result = subprocess.run(command, check=True, text=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        LOGGER.error(
            "Word 変換に失敗しました input=%s output=%s returncode=%s stderr=%s",
            input_path,
            output_path,
            exc.returncode,
            exc.stderr,
        )
        raise
    log_jsonl(
        output_path.parent / "logs" / "run.jsonl",
        {
            "event": "pandoc",
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "command": [
                "pandoc",
                input_path.name,
                "--from",
                "markdown",
                "--to",
                "docx",
                "--reference-doc",
                template_path.name,
            ],
        },
    )


def main() -> int:
    """CLI 引数を読み、Markdown から Word docx へ変換する。"""

    configure_logging()
    parser = argparse.ArgumentParser(
        description="Convert Markdown to Word docx with pandoc"
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--template", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    output = Path(args.output)
    if output.exists() and not args.force:
        LOGGER.info("既存出力を再利用します output=%s", output)
        return 0
    convert_markdown_to_docx(Path(args.input), output, Path(args.template))
    LOGGER.info("Word 変換が完了しました output=%s", output)
    return 0


if __name__ == "__main__":
    started_at = perf_counter()
    try:
        exit_code = main()
    finally:
        LOGGER.info("処理時間 %.3f 秒", perf_counter() - started_at)
    sys.exit(exit_code)
