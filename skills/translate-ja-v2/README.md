# 📄 translate-ja-v2

PDF/Word 文書を Docling Serve と OpenAI 互換 API で解析・日本語翻訳し、JSON、Markdown、Word docx を生成するスキルです。

## 🚀 Quick Start

### 📦 1. 依存関係を用意する

リポジトリルートで Python 依存関係を同期します。

```bash
uv sync
```

docx まで生成する場合は `pandoc` も必要です。Markdown/JSON だけ検証する場合は `--skip-docx` を使えます。

### 🔐 2. `.env` を用意する

リポジトリルートの `.env` に次のキーを設定します。

```dotenv
DOCLING_SERVER_URL=https://docling.example.test
DOCLING_API_KEY=your-docling-api-key
OPENAI_BASE_URL=https://openai-compatible.example.test/v1
OPENAI_API_KEY=your-openai-api-key
OPENAI_MODEL=your-model
```

必要に応じて timeout も設定できます。

```dotenv
DOCLING_TIMEOUT_SECONDS=21600
OPENAI_TIMEOUT_SECONDS=1800
```

OpenAI 互換 API が 429 や一時的な 5xx を返す場合は、Chat Completions 呼び出しを指数バックオフで再試行します。必要に応じて次の値を調整できます。

```dotenv
TRANSLATE_JA_V2_OPENAI_MAX_ATTEMPTS=6
TRANSLATE_JA_V2_OPENAI_RETRY_INITIAL_SECONDS=5
TRANSLATE_JA_V2_OPENAI_RETRY_MAX_SECONDS=60
```

### ▶️ 3. 翻訳パイプラインを実行する

`translate.py` はユーザーが直接実行できます。

```bash
uv run python skills/translate-ja-v2/scripts/translate.py \
  --input ./sample.pdf \
  --output-dir ./output-v2/sample \
  --template ./skills/translate-ja/template.dotx
```

VLM 構造補正や docx 生成を省いて軽く検証する場合は次のようにします。

```bash
uv run python skills/translate-ja-v2/scripts/translate.py \
  --input ./sample.pdf \
  --output-dir ./output-v2/sample \
  --skip-vlm \
  --skip-docx
```

CLI オプションを確認する場合は `--help` を使います。

```bash
uv run python skills/translate-ja-v2/scripts/translate.py --help
```

## 📦 Outputs

出力先には次のファイルが作られます。

```text
output-v2/sample/
├── sample.docling.json
├── sample.normalized.json
├── sample.structured.json
├── sample.translated.json
├── sample.ja.md
├── sample.ja.docx
├── artifacts/
└── manifest.json
```

`--skip-docx` を指定した場合、`sample.ja.docx` は生成されません。

## 🧪 Verification

単体テストは外部 API を fake client で置き換えて実行できます。

```bash
uv run pytest skills/translate-ja-v2/tests/test_translate_pipeline.py
```

静的解析と format 確認は次を実行します。

```bash
uv run ruff check skills/translate-ja-v2
uv run ruff format --check skills/translate-ja-v2
uv run ty check skills/translate-ja-v2
```

## 🧰 Tech Stack

- [Python 3.12+](https://docs.python.org/3/)
- [Typer](https://typer.tiangolo.com/)
- [Pydantic](https://docs.pydantic.dev/)
- [HTTPX](https://www.python-httpx.org/)
- [python-dotenv](https://github.com/theskumar/python-dotenv)
- [OpenAI Python SDK](https://github.com/openai/openai-python)
- [Docling Serve](https://github.com/docling-project/docling-serve)
- [pandoc](https://pandoc.org/)
- [pytest](https://docs.pytest.org/)
- [Ruff](https://docs.astral.sh/ruff/)
- [ty](https://docs.astral.sh/ty/)

## 👤 Author

k5-mot

## 📜 License

MIT
