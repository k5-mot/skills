---
name: ts-dev
description: TypeScriptフロントエンド開発の標準手順。Use when Codex creates, changes, reviews, or sets up TypeScript frontend projects, including Vite/Vite Plus, React UI choices, lint/format/test configuration, pre-commit hooks, GitHub Actions CI, Playwright UI/E2E tests, and vulnerable package update workflows.
---

# TS Dev

TypeScriptフロントエンドを実装・修正・初期化するときは、このSkillをプロジェクト標準として使う。既存プロジェクトでは既存のUIライブラリ、ビルド設定、CI、テスト構成を優先し、不足している標準だけを足す。

## 基本方針

- パッケージ管理は原則 `pnpm` を使う。
- Vite Plusを使う新規プロジェクトでは `vite-plus` の流儀に従う。
- UIライブラリはユーザーまたはプロジェクトの既存選定に従う。未決定なら実装前に選択肢を確認する。
- 関数・コンポーネント・公開ユーティリティにはJSDoc/TSDocを書く。目的、引数、戻り値を説明し、例外や副作用がある場合も書く。
- コメントを書く場合は日本語で、コードの逐語説明ではなく理由や注意点を書く。
- public utilityの振る舞いを変えた場合は `docs/` に利用方法や変更点を残す。
- UI/E2Eテストは Playwright を使う。
- Git hookは `pre-commit` を使い、huskyは使わない。

## 初期セットアップ

新規フロントエンドでは、まずVite Plusとpnpmを用意する。

```bash
npm i --global pnpm vite-plus
curl -fsSL https://vite.plus | bash
vp env off
vp create
vp install
```

Serendie Design Systemを使う場合の基本依存は以下を起点にする。

```bash
pnpm add react react-dom react-router-dom @serendie/design-token @serendie/symbols @serendie/ui
pnpm add -D vite typescript @pandacss/dev @types/node @types/react @types/react-dom @vitejs/plugin-react
pnpm add -D oxlint prettier vitest @testing-library/react @testing-library/user-event jsdom @playwright/test npm-check-updates
```

Cloudflare Kumo、Park UI、shadcn/uiなどを使う場合は、選んだUI基盤に必要な依存だけを追加する。

## 品質チェック

`package.json` には少なくとも以下のscriptsを用意する。既存プロジェクトでは既存名に合わせてもよいが、lint/format/testの意味は保つ。

```json
{
  "scripts": {
    "lint": "oxlint . && tsc --noEmit",
    "format": "prettier --check .",
    "format:fix": "prettier --write .",
    "test": "vitest run",
    "test:e2e": "playwright test",
    "audit": "pnpm audit",
    "dev": "vite"
  }
}
```

E2E、全ブラウザテスト、画像比較などの重いテストは `test:e2e` に分ける。通常の `test` はpre-commitで毎回実行できる軽さを保つ。

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

pre-commit自体は、環境に合わせて `uv tool install pre-commit`、`pipx install pre-commit`、またはOSパッケージで導入する。セットアップ後に以下を実行する。

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
      - run: pnpm test
      - run: pnpm audit
      - run: pnpm exec playwright install --with-deps
      - run: pnpm test:e2e
```

プロジェクトに `.node-version` がない場合は、サポートするNode.jsバージョンを明示してからCIへ入れる。
