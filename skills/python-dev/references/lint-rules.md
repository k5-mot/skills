# Ruff推奨ルール

Ruff設定を作成・変更するときはこのreferenceを読む。既存設定がある場合は上書きせず、既存ルールの意図を確認してから差分を反映する。

## 採用方針

- `ruff format` と衝突しやすいスタイル規約は避け、バグ検出、import整理、近代化、logger強制、セキュリティ寄りのルールを優先する。
- `select = ["ALL"]` は新規導入時のノイズが大きいため、初期標準にはしない。
- `D` はdocstring文化を維持するために入れる。ただしモジュール・パッケージ・特殊メソッドのdocstring強制は初期標準では緩める。
- `T20` で `print` を検出し、標準出力仕様が明確なCLIだけ `noqa` または `per-file-ignores` で例外にする。
- `LOG` を入れ、logger利用をlintでも後押しする。

## pyproject.toml例

```toml
[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.lint]
select = [
  "E",
  "F",
  "W",
  "I",
  "N",
  "UP",
  "B",
  "A",
  "C4",
  "DTZ",
  "T20",
  "LOG",
  "G",
  "PIE",
  "PT",
  "RET",
  "S",
  "SIM",
  "TID",
  "ARG",
  "PTH",
  "ERA",
  "PL",
  "RUF",
  "D",
]
ignore = [
  "D100",
  "D104",
  "D105",
  "D107",
  "E501",
  "PLR0912",
  "PLR0913",
  "PLR0915",
]

[tool.ruff.lint.pydocstyle]
convention = "google"

[tool.ruff.lint.per-file-ignores]
"tests/**" = [
  "S101",
  "S106",
  "ARG001",
  "PLR2004",
]
```

## 既存設定への反映

1. 既存の `select` / `extend-select` / `ignore` / `per-file-ignores` を確認する。
2. 既存プロジェクトに強い理由がなければ、上記の `select` を基準に不足カテゴリを追加する。
3. `T20` を入れたら、正当なCLI標準出力だけ例外化する。通常ログは `logger` へ寄せる。
4. `D` を入れたら、日本語docstringでもルールが通るように書式を整える。
5. 追加後に `uv run ruff check .` と `uv run ruff format --check .` を実行する。

## 参考

- Ruff configuration: https://docs.astral.sh/ruff/configuration/
- Ruff linter: https://docs.astral.sh/ruff/linter/
- Ruff settings: https://docs.astral.sh/ruff/settings/
