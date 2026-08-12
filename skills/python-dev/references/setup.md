# セットアップ工程

Pythonバックエンドの新規作成、依存追加、開発環境整備ではこの手順を使う。既存プロジェクトでは既存のパッケージ管理を優先し、`uv` へ移行する場合はユーザーの意図を確認する。

## 基本ツール

新規Pythonバックエンドでは Python 3.12 以上と `uv` を前提にする。まずバージョンを確認する。

```bash
python3 --version
uv --version
```

`uv` を用意してプロジェクトを作る。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv init
uv add python-dotenv requests
uv add -D pytest ruff ty pip-audit pre-commit taskipy
```

FastAPI、SQLAlchemy、Alembic、Pydanticなどは、実際に使うアーキテクチャが決まってから追加する。不要なフレームワークを先に入れない。

## .gitignore

新規Pythonプロジェクトでは `references/full.gitignore` のフル版テンプレートを使って `.gitignore` を作成する。既存ファイルがある場合は上書きせず、Python、uv、pytest、Ruff、OS/editor、dotenv、devcontainer cacheのセクションを不足分だけmergeする。

secretやlocal-only設定は必ず除外し、共有が必要な環境変数は `.env.example` にキー名だけを書く。`.python-version`、`uv.lock`、`pyproject.toml` は再現性に関わるため、原則としてignoreしない。

## 開発コマンド

`pyproject.toml` には少なくとも以下の用途のコマンドを用意する。既存のタスクランナーがある場合はそれに合わせる。

- `lint`: `ruff check .` と `ty check` を実行する。
- `format`: `ruff format --check .` を実行する。自動修正用に `format:fix` 相当も用意してよい。
- `test`: 軽量な単体・統合テストを `pytest` で実行する。
- `audit`: `pip-audit` で依存関係の脆弱性を確認する。
- `dev`: アプリケーションをローカル起動する。

E2Eや外部サービスを使う重いテストは、通常の `test` から分け、`test:e2e` や `test:heavy` のような明示コマンドにする。
