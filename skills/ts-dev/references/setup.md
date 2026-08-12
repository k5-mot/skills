# セットアップ工程

TypeScriptフロントエンドの新規作成、依存追加、UI基盤選定ではこの手順を使う。既存プロジェクトでは既存のUIライブラリ、ビルド設定、パッケージ管理を優先する。

## 基本ツール

新規フロントエンドでは Node.js 20 以上、npm 10 以上を前提にする。まずバージョンを確認する。

```bash
node --version
npm --version
```

Vite Plusとpnpmを用意する。

```bash
npm i --global pnpm vite-plus @voidzero-dev/vite-plus-core@latest
curl -fsSL https://vite.plus | bash
vp env off
pnpm --version
vp create
vp install
```

## UI基盤

UIライブラリはユーザーまたはプロジェクトの既存選定に従う。未決定なら実装前に選択肢を確認する。

Serendie Design Systemを使う場合の基本依存は以下を起点にする。

```bash
pnpm add react react-dom react-router-dom @serendie/design-token @serendie/symbols @serendie/ui
pnpm add -D vite typescript @pandacss/dev @types/node @types/react @types/react-dom @vitejs/plugin-react
pnpm add -D oxlint prettier vitest @testing-library/react @testing-library/user-event jsdom @playwright/test npm-check-updates
```

Cloudflare Kumo、Park UI、shadcn/uiなどを使う場合は、選んだUI基盤に必要な依存だけを追加する。

## 開発コマンド

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

Vite+ プロジェクトでは `vp dev`、`vp build`、`vp check`、`vp test` を優先し、詳細は [vite-plus.md](vite-plus.md) に従う。
