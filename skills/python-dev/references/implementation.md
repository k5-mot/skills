# 実装工程

Pythonコードやスクリプトを実装・修正するときはこの規約を使う。

## 推奨ライブラリ

- CLI引数解析は `argparse` より `typer` を優先する。
- 構造化データや設定値は `dataclass` より `pydantic.BaseModel` を優先する。
- HTTP client は `requests` より `httpx` を優先する。
- DataFrame処理は `pandas` より `polars` を優先する。
- notebook形式の実験・共有は Jupyter Notebook より `marimo` を優先する。
- ブラウザ自動化は Selenium より Playwright を優先する。

## docstringとコメント

- 関数・メソッドには必ず標準形式のdocstringを書く。
- docstringには目的、引数、戻り値を書く。
- 例外や副作用がある場合はdocstringに追記する。
- コメントを書く場合は日本語で、コードの逐語説明ではなく理由や注意点を書く。

## logger

- ログ出力には必ず `logging` の `logger` を使う。
- loggerで出力するログメッセージは必ず英語にする。
- `logging.basicConfig` などのformatには `%(pathname)s`、`%(funcName)s`、`%(lineno)d` を含め、対象ファイル、対象関数、対象行を追跡できるようにする。
- `print` はCLIの最終結果など、標準出力が外部仕様になっている場合だけに限定する。
- 例外を握りつぶさず、原因が追えるように `logger.exception` または適切なログレベルを使う。
- CLI の終了処理は `raise SystemExit(...)` を直接書かず、`sys.exit(...)` を使う。`os._exit` は cleanup を飛ばすため、通常のCLI終了には使わない。

## スクリプト入口

Pythonを1ファイルのスクリプトとして実装する場合は、`main` 関数に引数解析、環境変数読み込み、主要ロジック呼び出し、簡単な例外処理を置く。`if __name__ == "__main__"` には `time.perf_counter` によるad-hocな実行時間計測、`main` 呼び出し、`sys.exit(...)` による終了処理を置く。

```python
import logging
import sys
import time
from typing import Annotated

import typer
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


class RunOptions(BaseModel):
    """スクリプト実行オプションを保持する。

    Args:
        verbose: 詳細ログを出力するかどうか。

    Returns:
        実行時に使うオプション。
    """

    model_config = ConfigDict(frozen=True)

    verbose: bool = False


def run(verbose: bool) -> None:
    """主要ロジックを実行する。

    Args:
        verbose: 詳細ログを出力するかどうか。

    Returns:
        なし。

    Side Effects:
        必要に応じてログへ処理状況を出力する。
    """
    if verbose:
        logger.info("Running")


app = typer.Typer(add_completion=False)


@app.command()
def cli(
    verbose: Annotated[bool, typer.Option(help="詳細ログを出力する")] = False,
) -> None:
    """CLIから主要ロジックを実行する。

    Args:
        verbose: 詳細ログを出力するかどうか。

    Returns:
        なし。

    Side Effects:
        環境変数を読み込み、ログへ実行状況やエラーを出力する。
    """

    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(levelname)s:%(name)s:"
            "file=%(pathname)s:function=%(funcName)s:line=%(lineno)d:%(message)s"
        ),
    )
    try:
        options = RunOptions(verbose=verbose)
        run(options.verbose)
    except Exception as exc:
        logger.exception("Script execution failed: %s", exc)
        raise typer.Exit(1) from exc
    raise typer.Exit(0)


def main(argv: list[str] | None = None) -> int:
    """スクリプトを実行する。

    Args:
        argv: コマンドライン引数。Noneの場合はsys.argv由来の引数を使う。

    Returns:
        プロセス終了コード。成功時は0、失敗時は非0を返す。

    Side Effects:
        環境変数を読み込み、ログへ実行状況やエラーを出力する。
    """
    try:
        app(args=argv, standalone_mode=False)
    except typer.Exit as exc:
        return int(exc.exit_code or 0)
    return 0


if __name__ == "__main__":
    started_at = time.perf_counter()
    exit_code = main()
    elapsed = time.perf_counter() - started_at
    logger.info("Elapsed time: %.3fs", elapsed)
    sys.exit(exit_code)
```
