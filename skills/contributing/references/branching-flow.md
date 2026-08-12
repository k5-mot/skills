# Branching Flow

このプロジェクトでは GitHub Flow を採用する。

## 採用: GitHub Flow

GitHub Flow は、`main` を常にdeploy可能に保ち、変更ごとに短命ブランチを作り、Pull Request 経由でmergeする運用である。

採用理由:

- agent skill リポジトリは変更単位が小さく、長期ブランチを必要としない。
- Pull Request レビューとCIを必須化しやすい。
- `main` を常に利用可能な状態に保ちやすい。
- release tag を `main` の特定commitへ付けやすい。

必須ルール:

- `main` へ直接pushしない。
- 1ブランチは1目的にする。
- ブランチは小さく短命に保つ。
- Pull Request では変更理由、検証結果、影響範囲を書く。

## 比較したflow

### GitLab Flow

GitLab Flow は環境ブランチやリリースブランチと相性がよい。複数環境への段階deployが主目的のプロダクトでは有効だが、このリポジトリでは環境別ブランチの運用コストが利益を上回るため採用しない。

### git-flow

git-flow は `develop`、`release/*`、`hotfix/*` などの長期ブランチを使う。定期大型リリースには向くが、変更単位の小さいskill更新ではブランチ管理が重くなるため採用しない。

### Trunk-Based Development

Trunk-Based Development は短命ブランチまたは直接trunk更新で高速に統合する。成熟したCIとfeature flag運用がある場合は強いが、このリポジトリでは Pull Request レビューを明確に残すため GitHub Flow を採用する。

## Branch naming

ブランチ名は次を推奨する。

```text
<type>/<short-description>
```

`type` はコミットtypeと揃え、`feat`、`fix`、`docs`、`test`、`refactor`、`chore`、`perf`、`release` を使う。
