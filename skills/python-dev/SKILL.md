---
name: python-dev
description: Pythonバックエンド開発の標準手順。Use when Codex creates, changes, reviews, or sets up Python backend projects, CLI tools, or single-file scripts, including uv setup, .gitignore creation, FastAPI/SQLAlchemy-style services, script entrypoints with Typer/python-dotenv/time.perf_counter/logger, Ruff rules, lint/format/test configuration, pre-commit hooks, GitHub Actions CI, Playwright UI/E2E tests, and vulnerable package update workflows.
---

# Python Dev

Pythonバックエンドを実装・修正・初期化するときは、このSkillをプロジェクト標準として使う。既存プロジェクトでは既存の設計、パッケージ管理、CI、テスト構成を優先し、不足している標準だけを足す。

## 工程別リファレンス

作業内容に応じて、必要なreferenceを先に読む。

- 新規作成、依存追加、開発環境整備: [setup.md](references/setup.md)
- Python向けフル版 `.gitignore` テンプレート: [full.gitignore](references/full.gitignore)
- 実装、スクリプト入口、docstring、logger: [implementation.md](references/implementation.md)
- Ruff設定、推奨lintルール、既存設定への反映: [lint-rules.md](references/lint-rules.md)
- pre-commit、テスト、Playwright、脆弱性更新、GitHub Actions: [verification.md](references/verification.md)

今後ルールを増やす場合は、詳細を工程別referenceへ追加し、`SKILL.md` には参照先と適用タイミングだけを書く。

## 常時適用

- 関数・メソッドには必ず標準形式のdocstringを書く。目的、引数、戻り値を説明し、例外や副作用がある場合も書く。
- コメントを書く場合は日本語で、コードの逐語説明ではなく理由や注意点を書く。
- ログ出力には必ず `logging` の `logger` を使い、`print` はCLIの最終結果など明確な標準出力仕様がある場合だけに限定する。
- loggerで出力するログメッセージは必ず英語にし、ログformatには対象ファイル、対象関数、対象行を含める。
- public utilityの振る舞いを変えた場合は `docs/` に利用方法や変更点を残す。
- CLI引数解析は `argparse` より `typer` を優先する。
- 構造化データや設定値は `dataclass` より `pydantic.BaseModel` を優先する。
- ブラウザ自動化は Selenium より Playwright を優先する。
- DataFrame処理は pandas より Polars を優先する。
- HTTP client は requests より HTTPX を優先する。
- notebook形式の実験・共有は Jupyter Notebook より marimo を優先する。
- UI/E2Eテストが必要な場合は Playwright を使う。
- Git hookは `pre-commit` を使い、huskyは使わない。

## 反映手順

1. 該当工程のreferenceを読む。
2. 既存プロジェクトの設定と衝突する場合は、既存の意図を保ったうえで差分導入する。
3. Ruff設定を作成・変更する場合は、必ず [lint-rules.md](references/lint-rules.md) の推奨ルールを確認し、採用・見送りの理由が分かる形で設定へ反映する。
4. 変更後は [verification.md](references/verification.md) の軽量チェックを実行し、実行できないものは理由を残す。
