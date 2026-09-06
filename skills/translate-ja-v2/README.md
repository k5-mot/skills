# 📄 translate-ja-v2

PDF/Word文書をDocling Serveで解析し、LibreTranslateまたはOpenAI互換APIで日本語翻訳して、段階別JSON、Markdown、Word docxを生成するスキルです。各工程と要素の進捗を `manifest.json` に記録し、中断後のResumeに対応します。

## 🚀 Quick Start

リポジトリルートで依存関係を同期します。docxまで生成する場合は、PATHから実行できるpandocも用意してください。

```bash
uv sync
```

`.env` に接続情報を設定します。

```dotenv
DOCLING_SERVER_URL=https://docling.example.test
DOCLING_API_KEY=your-docling-api-key
LIBRETRANSLATE_URL=http://localhost:5000
# LIBRETRANSLATE_API_KEY=your-libretranslate-api-key
OPENAI_BASE_URL=https://openai-compatible.example.test/v1
OPENAI_API_KEY=your-openai-api-key
OPENAI_MODEL=your-model
```

既定のTranslate backendは `docker.io/libretranslate/libretranslate/v1.9.6` 互換のLibreTranslateです。StructureとReviewはOpenAI互換APIを使います。

```bash
uv run python skills/translate-ja-v2/scripts/translate.py \
  --translator default \
  --context-chars 50000 \
  --batch-chars 20000 \
  --input ./inputs/sample.pdf \
  --output-dir ./outputs/sample \
  --template ./skills/translate-ja-v2/examples/template.dotx
```

TranslateにもLLMを使って全ステージを実行する場合は、次のように指定します。

```bash
uv run python skills/translate-ja-v2/scripts/translate.py \
  --translator llm \
  --context-chars 50000 \
  --batch-chars 20000 \
  --input ./inputs/sample.pdf \
  --output-dir ./outputs/sample \
  --template ./skills/translate-ja-v2/examples/template.dotx \
  --glossary ./skills/translate-ja-v2/examples/glossary.csv \
  --translation-rules ./skills/translate-ja-v2/examples/translation-rules.md
```

### 引数

| 引数 | 必須 | 既定値 | 説明 |
| --- | --- | --- | --- |
| `--input PATH` | はい | なし | 翻訳するPDFまたはWord文書を指定します。 |
| `--output-dir PATH` | いいえ | `./outputs/<入力stem>` | 段階別JSON、Markdown、docx、artifacts、manifestの出力先を指定します。 |
| `--output PATH` | いいえ | `<output-dir>/document.ja.docx` | 最終docxだけを別のパスへ出力します。 |
| `--template PATH` | いいえ | なし | pandocへ渡すreference DOCX/DOTXを指定します。 |
| `--glossary PATH` | いいえ | なし | Translate（LLM）とReviewで使う、`english-short,english-long,japanse-short,japanese-long,kind,description,note` 列を持つUTF-8 CSV用語集を指定します。 |
| `--translation-rules PATH` | いいえ | 組み込みルール | LLM TranslateとReviewへ渡すUTF-8のルール文書を指定します。 |
| `--context-chars INTEGER` | いいえ | `50000` | 1回のOpenAI互換API requestへ含めるテキストの最大文字数を指定します。 |
| `--batch-chars INTEGER` | いいえ | `1500` | TranslateとReviewで1回のbatchへ詰める原文・訳文の最大文字数を指定します。 |
| `--translator default\|llm` | いいえ | `default` | Translate backendを選びます。`default` はLibreTranslate、`llm` はOpenAI互換APIです。StructureとReviewには影響しません。 |
| `--env PATH` | いいえ | `.env` | 読み込むdotenvファイルを指定します。既存の環境変数は上書きしません。 |
| `--force` | いいえ | 無効 | 完了済みParse成果物があってもDocling変換から再実行します。 |
| `--skip-vlm` | いいえ | 無効 | StructureのVLM補正だけを省略します。Normalizeは実行します。 |
| `--skip-review` | いいえ | 無効 | 翻訳Reviewを省略し、Translate成果物からMarkdownを生成します。 |
| `--skip-docx` | いいえ | 無効 | pandocによるdocx生成を省略し、JSONとMarkdownまで生成します。 |
| `--help` | いいえ | なし | 利用可能な引数とhelpを表示して終了します。 |

`--context-chars 50000` では、`--batch-chars 20000`〜`30000` が呼び出し回数と安定性の実用的な範囲です。`50000` も指定できますが、最大20要素、推定応答12,000文字、完成LLM prompt 50,000文字の制限で再分割されるため、`30000` からの削減効果は小さくなります。値を変えるとTranslateとReviewの設定hashが変わり、同じ出力先でも両工程を再実行します。

同じコマンドを再実行すると、`manifest.json` と成果物hashを検証して続きからResumeします。詳細は [workflow.md](references/workflow.md)、実装仕様は [spec.md](references/spec.md)、検証手順は [test.md](references/test.md)、DOTX仕様は [template-format.md](references/template-format.md) を参照してください。

## 👤 Author

k5-mot

## 📜 License

MIT
