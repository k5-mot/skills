---
name: init-project
description: プロジェクト初期化用の標準手順。Use when Codex initializes a repository or developer environment with standard agent skills and project tools, including k5-mot skills, Matt Pocock Agent Skills, graphify, and OpenSpec.
---

# Init Project

プロジェクト初期化時に、標準のagent skillsと補助ツールを導入する。ユーザーが別順序を指定しない限り、k5-motスキル、Matt Pocock Agent Skills、graphify、OpenSpecの順で実行する。

## 前提

- プロジェクトルートで実行する。
- `pnpm` と `uv` が利用できることを確認する。
- 既存の設定ファイルがある場合は、上書きや再初期化の影響を確認してから進める。
- コマンドが失敗した場合は次へ進まず、失敗原因と再実行手順を整理する。

## 導入手順

1. k5-motスキルをインストールする。

```bash
pnpm dlx skills@latest add k5-mot/skills python-dev ts-dev init-project --agent universal -y
```

2. Matt Pocock Agent Skillsをインストールする。

```bash
pnpm dlx skills@latest add mattpocock/skills code-review codebase-design diagnosing-bugs domain-modeling grill-me grill-with-docs grilling handoff implement improve-codebase-architecture prototype research resolving-merge-conflicts setup-matt-pocock-skills tdd teach to-spec to-tickets triage wayfinder writing-great-skills --agent universal -y
```

3. graphifyをプロジェクトへインストールする。

```bash
uvx --from graphifyy graphify install --project --platform agents
```

4. OpenSpecを初期化する。

```bash
pnpm dlx @fission-ai/openspec@latest openspec init --tools agents --profile core --force --no-animation
```

## 検証

導入後に、生成・変更されたファイルを確認する。

```bash
git status --short
```

確認観点:

- `python-dev`、`ts-dev`、`init-project` が利用可能なagent skillとして導入されている。
- Matt Pocock Agent Skillsの指定スキルが導入されている。
- graphifyのagents向け設定がプロジェクトに追加されている。
- OpenSpecの初期化ファイルが作成または更新されている。

## 注意

- `--force` を含むOpenSpec初期化は既存設定を更新する可能性がある。既存OpenSpec構成がある場合は、実行前後の差分を必ず確認する。
- すでに導入済みのskillがある場合でも、コマンドは再実行可能な前提で扱う。ただし生成物の差分は確認する。
- 初期化後、プロジェクト種別に応じて `python-dev` または `ts-dev` を使い、lint/format/test/pre-commit/CI設定を整える。
