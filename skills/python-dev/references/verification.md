# 検証・CI工程

Pythonバックエンドの検証、pre-commit、E2E、脆弱性更新、GitHub Actions設定ではこの手順を使う。

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
