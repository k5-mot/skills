# 📄 translate-ja-v3

LangChain/LangGraphでPDF/Wordを解析し、構造補正、日本語翻訳、複数agent Review、Markdown、Word docx生成までを再開可能なStageとして実行します。

## 🚀 Quick Start

依存関係を同期し、`.env.sample` を参考に `.env` を設定します。docxを生成する場合はpandocもPATHへ追加してください。

```bash
uv sync
cp skills/translate-ja-v3/.env.sample .env
```

既定のTranslate backendは `docker.io/libretranslate/libretranslate/v1.9.6` 互換APIです。StructureとReviewはOpenAI互換APIを使います。

```bash
uv run python skills/translate-ja-v3/scripts/run_pipeline.py \
  --translator default \
  --context-chars 50000 \
  --batch-chars 20000 \
  --max-batch-elements 0 \
  --input ./inputs/sample.pdf \
  --output-dir ./outputs/sample \
  --template ./skills/translate-ja-v3/examples/template.docx \
  --glossary ./skills/translate-ja-v3/examples/glossary.csv \
  --structure-rules ./skills/translate-ja-v3/examples/structure-rules.md \
  --review-rules ./skills/translate-ja-v3/examples/review-rules.md
```

TranslateにもLLMを使う場合は `--translator llm` と翻訳ルールを指定します。

```bash
  --translator llm \
  --translation-rules ./skills/translate-ja-v3/examples/translation-rules.md
```

主な引数は次のとおりです。

| 引数 | 既定値 | 説明 |
| --- | --- | --- |
| `--input PATH` | 必須 | PDFまたはWord入力。 |
| `--output-dir PATH` | `outputs/<stem>` | 全成果物の保存先。 |
| `--output PATH` | `document.ja.docx` | 最終docxの別保存先。 |
| `--template PATH` | なし | pandoc reference docx。 |
| `--env PATH` | `.env` | dotenvファイル。既存環境変数は上書きしません。 |
| `--glossary PATH` | なし | 8列schemaのUTF-8 CSV用語集。 |
| `--structure-rules PATH` | なし | StructureStageへ渡す外部ルール。 |
| `--translation-rules PATH` | 最小組み込みルール | LLM TranslateStageへ渡す外部ルール。 |
| `--review-rules PATH` | 最小組み込みルール | Review subgraphへ渡す外部ルール。 |
| `--translator default\|llm` | `default` | LibreTranslateまたはLLMを選択。 |
| `--context-chars INTEGER` | `50000` | 1 requestのprompt文字数上限。 |
| `--batch-chars INTEGER` | `20000` | Translate/Reviewの原文文字数上限。 |
| `--max-batch-elements INTEGER` | `0` | 要素数上限。`0` は文字数だけで動的に決定。 |
| `--review-rag` | 無効 | Qdrantの検索結果をReviewへ追加。 |
| `--skip-vlm` | 無効 | StructureStageのVLM呼び出しを省略。 |
| `--skip-review` | 無効 | ReviewのLLM呼び出しを省略。 |
| `--skip-docx` | 無効 | docx生成を省略。 |
| `--force` | 無効 | ParseStage cacheを無視。 |

`--context-chars 50000 --max-batch-elements 0` では、`--batch-chars 20000`〜`30000` が実用的です。出力が長い文書や小さい出力上限のmodelでは `10000`〜`20000` に下げてください。LLM/VLM呼び出しが失敗すると、そのbatchだけを要素境界で半分にし、1要素まで自動縮小します。

用語集schemaは `english-short,english-long,japanse-short,japanese-long,kind,description,note,reference` です。検索は英語2列に対して行い、一致した行だけをLLMへ渡します。`note` と `reference` は内部管理用で、promptには含めません。

Review RAGへ文書を登録する場合は、ファイルまたはディレクトリを指定します。対応形式はPDF、DOCX/DOTX、Markdown、UTF-8テキスト系です。

```bash
uv run python skills/translate-ja-v3/scripts/ingest_qdrant.py \
  --input ./docs/domain \
  --collection domain-documents \
  --chunk-chars 1500 \
  --overlap-chars 200
```

同じ入力は安定IDでupsertされます。更新前の余剰chunkも削除する場合だけ `--replace-source` を追加します。外部APIを呼ばず抽出結果を確認するには `--dry-run` を使います。

## 👤 Author

k5-mot

## 📜 License

MIT
