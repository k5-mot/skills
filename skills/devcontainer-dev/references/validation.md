# Validation

## Lightweight Checks

```bash
bash -n .devcontainer/postCreateCommand.sh
shellcheck .devcontainer/postCreateCommand.sh
```

JSON commentsを含む `devcontainer.json` は、コメント対応のparserかVS Code Dev Containersの検証機能で確認する。

## Dependency Checks

Python project:

```bash
uv sync --dev
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```

TypeScript project:

```bash
pnpm install
pnpm exec oxlint .
pnpm exec oxfmt --check .
pnpm exec tsgo --noEmit
pnpm exec vitest run
```

## E2E Checks

UI/E2EテストはPlaywrightを使う。ローカルのpre-commitでは重いE2Eを省略してよいが、GitHub Actionsには含める。

```bash
pnpm exec playwright install --with-deps
pnpm exec playwright test
```

## Devcontainer Checks

- `Dev Containers: Rebuild Container` または `devcontainer up` で作成できる。
- post-create scriptのログ出力先が分かる。
- auto port forwardingでFastAPI、Vite、preview serverへアクセスできる。明示port設定は必要な場合だけ確認する。
- workspace rootとpackage配下のinstallが二重実行されない。
- cache mountがrootless userで書き込める。
