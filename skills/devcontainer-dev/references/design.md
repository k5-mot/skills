# Design

## Responsibility Split

- devcontainer: OS image、言語runtime、共通CLI、cache mount、VS Code拡張、auto port forwarding、post-create orchestrationを持つ。
- project dependencies: FastAPI、React、Zod、Ruff、ty、pytest、Oxlint、Oxfmt、tsgo、Vitestなど、projectごとにversionを固定すべきツールを持つ。
- CI: lint、format、unit test、E2E test、security auditなど、再現性のある検証を持つ。

## Recommended Defaults

- Pythonは `uv sync --dev` を基本にする。
- TypeScriptは `pnpm install` を基本にする。
- Node featureでは `pnpmVersion` を明示し、pnpm導入をdevcontainer featureの責務に寄せる。
- port forwardingはエディタやdevcontainer runtimeの自動転送を前提にし、明示的に必要なproject以外では `forwardPorts` を追加しない。
- UI/E2Eテストは Playwright を使う。
- Git hooksは pre-commit を使い、Huskyは使わない。
- pre-commitにはlint、format、testを含める。重いE2Eや長時間テストは含めなくてよい。
- GitHub Actionsではlint、format、unit testに加え、E2Eなど重いテストも実行する。

## Common Missing Items

- Node featureの `pnpmVersion` が明示されていない。
- Vite+ CLIや `$HOME/.vite-plus/env` の作成手順がない。
- tsgo CLIの導入と実行コマンドがない。
- Oxfmt CLIの導入と実行コマンドがない。
- Ruff、pytest、VitestのVS Code settingsがない。
- monorepo探索のprune条件が深い階層の `node_modules` や `.venv` に弱い。

## Proposal Style

- 「必須」「推奨」「任意」に分けて提案する。
- 必須は再現性や起動失敗に直結するものに限定する。
- 推奨は開発体験や標準化に効くものにする。
- 任意はチームの好みやproject方針に依存するものにする。
