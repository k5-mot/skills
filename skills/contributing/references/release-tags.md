# Release Tags

リリースタグは Semantic Versioning を基本にする。

## タグ形式

```text
v<major>.<minor>.<patch>
```

例:

```text
v1.4.2
```

## バージョン更新基準

- `major`: 後方互換性のない変更。既存のskill利用方法や生成物構造が壊れる場合。
- `minor`: 後方互換性のある機能追加。新スキル、既存スキルの新しい工程、optionalな設定追加など。
- `patch`: 後方互換性のある修正。誤記修正、説明改善、バグ修正、小さな設定修正など。

## プレリリース

必要な場合だけ prerelease tag を使う。

```text
v1.5.0-rc.1
v1.5.0-beta.1
```

## タグ作成手順

1. `main` が最新でCIに通っていることを確認する。
2. release commit が必要な場合は `🚀 release(release): vX.Y.Zを準備` の形式で作る。
3. 注釈付きタグを作る。

```bash
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin vX.Y.Z
```

## 注意

- 同じバージョン番号のタグを再利用しない。
- 既に公開したタグを移動しない。
- 破壊的変更がある場合は、release note に移行手順を書く。
