# 実装工程

Pythonコードやスクリプトを実装・修正するときはこの規約を使う。

## docstringとコメント

- 関数・メソッドには必ず標準形式のdocstringを書く。
- docstringには目的、引数、戻り値を書く。
- 例外や副作用がある場合はdocstringに追記する。
- コメントを書く場合は日本語で、コードの逐語説明ではなく理由や注意点を書く。

## logger

- ログ出力には必ず `logging` の `logger` を使う。
- `print` はCLIの最終結果など、標準出力が外部仕様になっている場合だけに限定する。
- 例外を握りつぶさず、原因が追えるように `logger.exception` または適切なログレベルを使う。

## スクリプト入口

Pythonを1ファイルのスクリプトとして実装する場合は、`main` 関数に引数解析、環境変数読み込み、主要ロジック呼び出し、簡単な例外処理を置く。`if __name__ == "__main__"` には `time.perf_counter` によるad-hocな実行時間計測、`main` 呼び出し、`raise SystemExit(...)` による終了処理を置く。

```python
import argparse
import logging
import time

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


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
        logger.info("running")


def main(argv: list[str] | None = None) -> int:
    """スクリプトを実行する。

    Args:
        argv: コマンドライン引数。Noneの場合はsys.argv由来の引数を使う。

    Returns:
        プロセス終了コード。成功時は0、失敗時は非0を返す。

    Side Effects:
        環境変数を読み込み、ログへ実行状況やエラーを出力する。
    """
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    try:
        run(args.verbose)
    except Exception as exc:
        logger.exception("スクリプトの実行に失敗しました: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    started_at = time.perf_counter()
    exit_code = main()
    elapsed = time.perf_counter() - started_at
    logger.info("elapsed: %.3fs", elapsed)
    raise SystemExit(exit_code)
```
