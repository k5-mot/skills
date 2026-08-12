---
name: python-dev
description: Pythonバックエンド開発の標準手順。Use when Codex creates, changes, reviews, or sets up Python backend projects, including uv setup, FastAPI/SQLAlchemy-style services, lint/format/test configuration, pre-commit hooks, GitHub Actions CI, Playwright UI/E2E tests, and vulnerable package update workflows.
---

# Python Dev

Pythonバックエンドを実装・修正・初期化するときは、このSkillをプロジェクト標準として使う。既存プロジェクトでは既存の設計、パッケージ管理、CI、テスト構成を優先し、不足している標準だけを足す。

## 基本方針

- パッケージ管理と実行は原則 `uv` を使う。
- 関数・メソッドには必ず標準形式のdocstringを書く。目的、引数、戻り値を説明し、例外や副作用がある場合も書く。
- コメントを書く場合は日本語で、コードの逐語説明ではなく理由や注意点を書く。
- public utilityの振る舞いを変えた場合は `docs/` に利用方法や変更点を残す。
- UI/E2Eテストが必要な場合は Playwright を使う。
- Git hookは `pre-commit` を使い、huskyは使わない。

## 初期セットアップ

新規Pythonバックエンドでは、まず `uv` を用意してプロジェクトを作る。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv init
uv add python-dotenv requests
uv add -D pytest ruff ty pip-audit pre-commit
```

FastAPI、SQLAlchemy、Alembic、Pydanticなどは、実際に使うアーキテクチャが決まってから追加する。不要なフレームワークを先に入れない。

## 品質チェック

`pyproject.toml` には少なくとも以下の用途のコマンドを用意する。既存のタスクランナーがある場合はそれに合わせる。

- `lint`: `ruff check .` と `ty check` を実行する。
- `format`: `ruff format --check .` を実行する。自動修正用に `format:fix` 相当も用意してよい。
- `test`: 軽量な単体・統合テストを `pytest` で実行する。
- `audit`: `pip-audit` で依存関係の脆弱性を確認する。
- `dev`: アプリケーションをローカル起動する。

E2Eや外部サービスを使う重いテストは、通常の `test` から分け、`test:e2e` や `test:heavy` のような明示コマンドにする。

## pre-commit

`.pre-commit-config.yaml` を追加し、pre-commitでは軽量な `lint`、`format`、`test` を必ず走らせる。E2E、全ブラウザテスト、長時間の負荷テスト、外部環境依存テストは含めない。

```yaml
repos:
  - repo: local
    hooks:
      - id: python-lint
        name: python lint
        entry: uv run ruff check .
        language: system
        pass_filenames: false
      - id: python-format
        name: python format
        entry: uv run ruff format --check .
        language: system
        pass_filenames: false
      - id: python-typecheck
        name: python typecheck
        entry: uv run ty check
        language: system
        pass_filenames: false
      - id: python-test
        name: python test
        entry: uv run pytest
        language: system
        pass_filenames: false
```

セットアップ後に以下を実行する。

```bash
uv run pre-commit install
uv run pre-commit run --all-files
```

## UI/E2Eテスト

UIやブラウザ操作のE2Eが必要な場合は Playwright を使う。Pythonプロジェクトでは次を追加する。

```bash
uv add -D pytest-playwright
uv run playwright install --with-deps
```

CIでは `uv run pytest tests/e2e` のようにE2Eを明示的に分離する。pre-commitには含めない。

## 脆弱性が見つかった依存の更新

1. `uv run pip-audit` でCVE、影響パッケージ、修正バージョンを確認する。
2. 直接依存なら `uv add '<package>>=<fixed-version>'`、間接依存なら `uv lock --upgrade-package <package>` を使う。
3. 更新後に `uv sync`、`uv run pip-audit`、`uv run ruff check .`、`uv run ruff format --check .`、`uv run ty check`、`uv run pytest` を実行する。
4. 破壊的変更が疑われる場合はリリースノートを確認し、影響する public utility は `docs/` に移行・互換性メモを書く。
5. E2Eが関係する更新ではCIまたは明示コマンドで Playwright テストも実行する。

## GitHub Actions

`.github/workflows/ci.yml` には、軽量チェックと重いテストの両方を置く。pre-commitと違い、CIではE2Eを含める。

```yaml
name: CI

on:
  pull_request:
  push:
    branches: [main]

jobs:
  python:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - uses: actions/setup-python@v5
        with:
          python-version-file: .python-version
      - run: uv sync --all-extras --dev
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run ty check
      - run: uv run pytest
      - run: uv run pip-audit
      - run: uv run playwright install --with-deps
        if: hashFiles('tests/e2e/**') != ''
      - run: uv run pytest tests/e2e
        if: hashFiles('tests/e2e/**') != ''
```

プロジェクトに `.python-version` がない場合は、サポートするPythonバージョンを明示してからCIへ入れる。
