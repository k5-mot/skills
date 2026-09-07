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
QDRANT_URI=https://qdrant.example.test
QDRANT_API_KEY=your-qdrant-api-key
# QDRANT_COLLECTION=domain-documents
```

既定のTranslate backendは `docker.io/libretranslate/libretranslate/v1.9.6` 互換のLibreTranslateです。StructureとReviewはOpenAI互換APIを使います。

```bash
uv run python skills/translate-ja-v2/scripts/translate.py \
  --translator default \
  --context-chars 50000 \
  --batch-chars 20000 \
  --max-batch-elements 0 \
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
  --max-batch-elements 0 \
  --input ./inputs/sample.pdf \
  --output-dir ./outputs/sample \
  --template ./skills/translate-ja-v2/examples/template.dotx \
  --glossary ./skills/translate-ja-v2/examples/glossary.csv \
  --translation-rules ./skills/translate-ja-v2/examples/translation-rules.md
```

Qdrantのドメイン根拠を使う複数Agent Reviewは次のoptionを追加します。`QDRANT_COLLECTION` 未指定時は、Qdrant上にcollectionが1件だけなら自動選択します。

```bash
  --review-mode multi \
  --review-rag
```

検索対象collectionは `QDRANT_EMBEDDING_MODEL` と同じQdrant inference modelで作成されている必要があります。named vectorを使うcollectionでは `QDRANT_VECTOR_NAME` も設定してください。

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
| `--max-batch-elements INTEGER` | いいえ | `0` | Translate（両backend）とReviewの1バッチの要素数上限。`0` は固定件数で制限せず、文字数・推定応答量から動的に分割します。正数ならその件数も上限になります。Structureには適用しません。 |
| `--translator default\|llm` | いいえ | `default` | Translate backendを選びます。`default` はLibreTranslate、`llm` はOpenAI互換APIです。StructureとReviewには影響しません。 |
| `--review-mode single\|multi` | いいえ | `single` | Review構成を選びます。`multi` はFidelityとTerminologyの独立Reviewerを実行し、不一致要素だけAdjudicatorで裁定します。 |
| `--review-rag` | いいえ | 無効 | `multi` ReviewでQdrantからドメイン根拠を検索します。`QDRANT_URI` と `QDRANT_API_KEY` が必要です。 |
| `--env PATH` | いいえ | `.env` | 読み込むdotenvファイルを指定します。既存の環境変数は上書きしません。 |
| `--force` | いいえ | 無効 | 完了済みParse成果物があってもDocling変換から再実行します。 |
| `--skip-vlm` | いいえ | 無効 | StructureのVLM補正だけを省略します。Normalizeは実行します。 |
| `--skip-review` | いいえ | 無効 | 翻訳Reviewを省略し、Translate成果物からMarkdownを生成します。 |
| `--skip-docx` | いいえ | 無効 | pandocによるdocx生成を省略し、JSONとMarkdownまで生成します。 |
| `--help` | いいえ | なし | 利用可能な引数とhelpを表示して終了します。 |

`--context-chars 50000 --max-batch-elements 0` では、`--batch-chars 20000`〜`30000` が呼び出し回数と安定性の実用的な範囲です。`50000` も指定できますが、推定応答12,000文字と完成LLM prompt 50,000文字の制限で再分割されるため、`30000` からの削減効果は小さくなります。`0` は文字数制限も無効にする指定ではありません。バッチ文字数・件数上限を変えるとTranslateとReviewの設定hashが変わり、同じ出力先でも両工程を再実行します。

LLMを使うTranslate・Reviewでは、一時的なAPI障害やタイムアウトが通常の再試行後も続く場合、失敗したバッチを二分して再実行します。入力容量超過、空応答、不正JSON、ID不一致でも二分し、必要なら1要素まで縮小します。例えば8要素なら `8 → 4 + 4 → 2 + 2 …` と分割します。1要素でもAPI障害が続けば未完了のまま停止し、認証・設定エラーは分割せず停止します。Reviewの単一要素の生成不全では既存仕様どおり原訳を保持します。縮小は失敗したバッチだけに適用し、後続バッチの上限は変更しません。LibreTranslateとStructureにはこの二分フォールバックを適用しません。

同じコマンドを再実行すると、`manifest.json` と成果物hashを検証して続きからResumeします。詳細は [workflow.md](references/workflow.md)、実装仕様は [spec.md](references/spec.md)、検証手順は [test.md](references/test.md)、DOTX仕様は [template-format.md](references/template-format.md) を参照してください。

## 👤 Author

k5-mot

## 📜 License

MIT
