---
name: devcontainer-dev
description: devcontainer構成の設計、レビュー、改善提案、ひな型作成を支援する。Use when Codex needs to inspect or author .devcontainer/devcontainer.json, postCreateCommand scripts, VS Code devcontainer customizations, or monorepo-ready development containers for Python/uv/FastAPI/Ruff/ty/pytest and TypeScript/React/Zod/Vite+/Oxlint/Oxfmt/tsgo/Vitest stacks.
---

# Devcontainer Dev

## Workflow

1. 現状調査では `references/inspection.md` を読み、`.devcontainer/devcontainer.json`、post-create scripts、言語別manifest、lockfile、workspace設定を確認する。
2. 設計または改善提案では `references/design.md` を読み、ランタイム、CLI、エディタ拡張、port、cache、monorepo検出、project dependenciesの責務を切り分ける。
3. 変更後の検証では `references/validation.md` を読み、構文チェック、軽量セットアップ確認、重い処理を避けた検証方針を選ぶ。

## Operating Rules

- ユーザーが「提案して」と依頼した場合は、設定変更を直接適用する前に不足点と選択肢を提示する。
- devcontainer側で全ツールをグローバルインストールする前に、プロジェクト依存で解決すべきものか確認する。
- UI/E2Eテストの環境確認では Playwright を前提にする。
- Node.js系は pnpm を優先し、pre-commit hooks は pre-commit を使う。Husky は採用しない。
- monorepoでは root workspace と package単体の二重installを避ける。
- ユーザーが明示しない限り、レビューやひな型作成だけでコミットしない。

## Expected Output

- 対応済み、未対応、任意改善を分けて報告する。
- 未対応項目は「devcontainerに入れるべきもの」と「プロジェクト依存に入れるべきもの」を明確にする。
- 変更した場合は、触ったファイルと実行した検証を簡潔に報告する。
