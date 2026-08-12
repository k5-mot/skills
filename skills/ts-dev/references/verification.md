# 検証・CI工程

TypeScriptフロントエンドの検証、pre-commit、E2E、脆弱性更新、GitHub Actions設定ではこの手順を使う。

## pre-commit

`.pre-commit-config.yaml` を追加し、pre-commitでは軽量な `lint`、`format`、`test` を必ず走らせる。huskyは追加しない。

```yaml
repos:
  - repo: local
    hooks:
      - id: ts-lint
        name: ts lint
        entry: pnpm lint
        language: system
        pass_filenames: false
      - id: ts-format
        name: ts format
        entry: pnpm format
        language: system
        pass_filenames: false
      - id: ts-test
        name: ts test
        entry: pnpm test
        language: system
        pass_filenames: false
```

pre-commit自体は、環境に合わせて `uv tool install pre-commit`、`pipx install pre-commit`、またはOSパッケージで導入する。

```bash
pre-commit install
pre-commit run --all-files
```

## UI/E2Eテスト

UI/E2Eは Playwright を使う。

```bash
pnpm add -D @playwright/test
pnpm exec playwright install --with-deps
```

`playwright.config.ts` は対象ブラウザ、baseURL、webServerを明示する。E2Eは `pnpm test:e2e` で実行し、pre-commitには含めない。

## CLI実行経路

Vite+プロジェクトでは `vp check` と `vp test` を優先する。Vite+を使わない、または個別CLIの結果を明示する必要がある場合は次を使う。

```bash
pnpm exec oxlint .
pnpm exec oxfmt --check .
pnpm exec tsgo --noEmit
pnpm exec vitest run
```

`package.json` scriptsでは `lint` に `oxlint . && tsgo --noEmit`、`format` に `oxfmt --check .`、`format:fix` に `oxfmt --write .` を置く。

## 脆弱性が見つかった依存の更新

1. `pnpm audit` で脆弱性、影響パッケージ、修正バージョンを確認する。
2. 直接依存は `pnpm update <package> --latest` または `pnpm add <package>@<fixed-version>` で更新する。
3. 間接依存は `pnpm why <package>` で経路を確認し、必要なら `pnpm.overrides` で一時固定する。
4. 複数更新が必要なら `pnpm exec ncu` または `pnpm exec ncu -u` を使い、変更範囲を確認してから `pnpm install` する。
5. 更新後に `pnpm audit`、`pnpm lint`、`pnpm format`、`pnpm test` を実行する。
6. UIやブラウザ依存の更新では `pnpm test:e2e` もCIまたは明示コマンドで実行する。

## GitHub Actions

`.github/workflows/ci.yml` には、軽量チェックと重いテストの両方を置く。pre-commitと違い、CIではE2Eを含める。

```yaml
name: CI

on:
  pull_request:
  push:
    branches: [main]

jobs:
  frontend:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: pnpm/action-setup@v4
      - uses: actions/setup-node@v4
        with:
          node-version-file: .node-version
          cache: pnpm
      - run: pnpm install --frozen-lockfile
      - run: pnpm lint
      - run: pnpm format
      - run: pnpm exec tsgo --noEmit
      - run: pnpm test
      - run: pnpm audit
      - run: pnpm exec playwright install --with-deps
      - run: pnpm test:e2e
```

プロジェクトに `.node-version` がない場合は、サポートするNode.jsバージョンを明示してからCIへ入れる。
