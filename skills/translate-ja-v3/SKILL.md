---
name: translate-ja-v3
description: LangChainとLangGraphを使い、PDF/WordをDocling Serveで解析して構造補正、clean、LibreTranslateまたはLLM翻訳、複数agent Review、Markdown、Word docxへ変換する。Stage別ファイル、SQLite checkpoint、manifestによる工程・要素単位Resume、Qdrant RAGに対応する。Use when running, modifying, testing, or troubleshooting the translate-ja-v3 pipeline and its templates, glossary, or external rules.
---

# translate-ja-v3

LangChain/LangGraphベースの文書翻訳パイプラインを実行・保守する。

## 参照資料

作業内容に対応する資料を先に確認する。

- Stage順序、入出力、Resume、障害時の挙動: [workflow.md](references/workflow.md)
- ライブラリ、API、CLI、データ契約、LangGraph設計: [spec.md](references/spec.md)
- unit・統合テストと期待値: [test.md](references/test.md)
- `examples/template.docx` のWord書式: [template-format.md](references/template-format.md)

## 実行

リポジトリルートから次を実行する。

```bash
uv run python skills/translate-ja-v3/scripts/run_pipeline.py \
  --input ./inputs/sample.pdf \
  --output-dir ./outputs/sample \
  --template ./skills/translate-ja-v3/examples/template.docx \
  --glossary ./skills/translate-ja-v3/examples/glossary.csv \
  --structure-rules ./skills/translate-ja-v3/examples/structure-rules.md \
  --translation-rules ./skills/translate-ja-v3/examples/translation-rules.md \
  --review-rules ./skills/translate-ja-v3/examples/review-rules.md \
  --translator llm \
  --context-chars 50000 \
  --batch-chars 20000 \
  --max-batch-elements 0
```

Review RAG用文書をQdrantへ登録するときは、[spec.md](references/spec.md) のIngest契約を確認して次を実行する。

```bash
uv run python skills/translate-ja-v3/scripts/ingest_qdrant.py \
  --input ./docs/domain \
  --collection domain-documents
```

## 実装規則

1. `run_pipeline.py` はCLIだけを扱い、Stage順序は `translate_ja_v3/graph.py` に置く。
2. Stage固有処理は `translate_ja_v3/stages/<stage>.py` のprivate関数へ置き、複数Stageで使う処理だけを共通moduleへ置く。
3. LLMはLangChainのprompt、LCEL、structured outputを使い、処理順、並列Review、checkpointはLangGraphで表現する。
4. 組み込みpromptは役割、日本語訳、外部ルール遵守、出力schemaだけに限定し、変更可能な判断基準を外部rulesへ置く。
5. Structure、Translate、Reviewは要素単位、それ以外はStage単位でResumeする。
6. ログ本文は英語、既定levelはDEBUGとし、level名だけを色付きにする。secretや巨大payloadは記録しない。
7. 挙動を変更した場合は関連するreferenceとテストも同じ変更で更新する。
8. Qdrant ingestは安定IDでupsertし、既存pointの削除は明示的な `--replace-source` の場合だけ行う。
