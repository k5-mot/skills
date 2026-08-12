# Inspection

## Files To Inspect

- `.devcontainer/devcontainer.json`
- `.devcontainer/postCreateCommand.sh` または `postCreateCommand` が参照するscript
- `.python-version`
- `pyproject.toml`
- `uv.lock`
- `package.json`
- `pnpm-workspace.yaml`
- `pnpm-lock.yaml`
- `vite.config.ts`
- `vitest.config.*`
- `playwright.config.*`
- `.pre-commit-config.yaml`

## Python Stack Checklist

- Python feature または image が対象バージョンを提供している。
- uv feature、uv install、または同等の導入手順がある。
- `.python-version` を読む場合、`uv python install` などで反映される。
- `uv sync --dev` など、dev dependency を含めた同期手順がある。
- FastAPIはproject dependencyとして入る前提か、devcontainerでsample/runtimeを明示的に入れる前提かを区別する。
- RuffはCLI dependencyとVS Code拡張の両方を確認する。
- tyはCLI dependencyとして入るかを確認する。VS Code組み込みTypeScriptとは別物として扱う。
- pytestはCLI dependencyとVS Code testing設定を確認する。

## TypeScript Stack Checklist

- Node.js feature または image が対象バージョンを提供している。
- pnpmを使う場合、Corepack有効化またはpnpm導入が明示されている。
- ReactとZodはproject dependencyとして扱い、devcontainerで固定導入しない。
- Vite+はVS Code拡張だけでなく、CLIやshell envの導入経路を確認する。
- Oxlint/OxfmtはVS Code拡張とCLI実行経路を分けて確認する。
- tsgoはTypeScript本体やVS Code内蔵TSとは別に、CLI導入と利用コマンドを確認する。
- VitestはCLI dependency、VS Code拡張、test scriptの有無を確認する。

## Monorepo Checklist

- workspace root のlockfileやworkspace定義を優先してinstallする。
- workspace配下packageの個別installを避ける。
- 探索時に `.git`、`node_modules`、`.venv`、`dist`、`build`、`.next`、`.turbo`、cacheディレクトリを除外する。
- 除外パターンが1階層だけに限定されていないか確認する。
- root以外のPython projectやNode projectも検出できるか確認する。
