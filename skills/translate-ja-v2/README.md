# 📄 translate-ja-v2

PDF/Word文書をDocling ServeとOpenAI互換APIで解析・日本語翻訳し、段階別JSON、Markdown、Word docxを生成するスキルです。各工程と要素の進捗を `manifest.json` に記録し、中断後のResumeに対応します。

## 🚀 Quick Start

リポジトリルートで依存関係を同期します。docxまで生成する場合は、PATHから実行できるpandocも用意してください。

```bash
uv sync
```

`.env` に接続情報を設定します。

```dotenv
DOCLING_SERVER_URL=https://docling.example.test
DOCLING_API_KEY=your-docling-api-key
OPENAI_BASE_URL=https://openai-compatible.example.test/v1
OPENAI_API_KEY=your-openai-api-key
OPENAI_MODEL=your-model
```

全ステージを実行します。

```bash
uv run python skills/translate-ja-v2/scripts/translate.py \
  --context-chars 50000 \
  --batch-chars 5000 \
  --input ./inputs/sample.pdf \
  --output-dir ./outputs/sample \
  --template ./skills/translate-ja-v2/examples/template.dotx \
  --glossary ./skills/translate-ja-v2/examples/glossary.csv \
  --translation-rules ./skills/translate-ja-v2/examples/translation-rules.md
```

同じコマンドを再実行すると、`manifest.json` と成果物hashを検証して続きからResumeします。詳細は [workflow.md](references/workflow.md)、実装仕様は [spec.md](references/spec.md)、検証手順は [test.md](references/test.md)、DOTX仕様は [template-format.md](references/template-format.md) を参照してください。

## 👤 Author

k5-mot

## 📜 License

MIT
