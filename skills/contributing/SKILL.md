---
name: contributing
description: Git運用、ブランチ、コミット、Pull Request、リリースタグの標準手順。Use when Codex creates commits, branches, PR preparation notes, release tags, or contribution guidance, including Japanese Conventional Commits with limited gitmoji/type/scope, GitHub Flow, and semantic versioning.
---

# Contributing

プロジェクトへの変更を整理し、ブランチ、コミット、Pull Request、リリースタグを作るときに使う。コミットメッセージは日本語で、Conventional Commits と gitmoji を組み合わせる。

## References

- GitHub Flow と他flow比較: [branching-flow.md](references/branching-flow.md)
- リリースタグ戦略: [release-tags.md](references/release-tags.md)

## コミットメッセージ

形式:

```text
<gitmoji> <type>(<scope>): <subject>
```

例:

```text
✨ feat(web): ログイン画面を追加
```

ルール:

- `<subject>` と body は日本語で書く。
- `<subject>` は50文字以内を目安にし、句点「。」で終えない。
- body には「なぜ変更したか」を書く。何を変えたかだけの説明にしない。
- 1コミットは1つの論理変更にする。
- 無関係な変更を混ぜない。

## 許可するtype/gitmoji

よく使うものに限定する。

| gitmoji | type | 用途 |
| --- | --- | --- |
| ✨ | feat | 新機能 |
| 🐛 | fix | バグ修正 |
| ♻️ | refactor | 振る舞いを変えない整理 |
| 📝 | docs | ドキュメント |
| ✅ | test | テスト追加・修正 |
| 🔧 | chore | 設定・ツール・雑務 |
| ⚡️ | perf | 性能改善 |
| 🚀 | release | リリース準備 |
| 🎉 | initial | initial commit のみ |

`initial` は initial commit だけで使う。通常の機能追加や設定追加には使わない。

## 許可するscope

scope は次に限定する。該当しない場合だけ省略してよい。

- `api`
- `web`
- `ci`
- `infra`
- `docs`
- `test`
- `deps`
- `config`
- `skills`
- `release`

## Branch / PR

GitHub Flow を採用する。詳細と比較根拠は [branching-flow.md](references/branching-flow.md) を読む。

- `main` は常にdeploy可能に保つ。
- `main` へ直接コミットしない。
- 変更ごとに短命ブランチを作る。
- Pull Request を作り、レビュー後に `main` へmergeする。
- ブランチ名は `<type>/<short-description>` を推奨する。

例:

```text
feat/user-login
fix/api-timeout
docs/update-skill-guide
```

## Release

リリースタグは SemVer を基本にする。詳細は [release-tags.md](references/release-tags.md) を読む。
