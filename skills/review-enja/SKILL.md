---
name: review-enja
description: 英語原文PDFと日本語翻訳PDFをDoclingで解析・対応付けし、忠実性と用語の複数LLM agentで翻訳品質をレビューしてJSONとMarkdownの指摘票を作る。Use when comparing an English PDF with its Japanese translation, running or troubleshooting the review-enja pipeline, or inspecting its alignment and review results.
---

# review-enja

英語原文PDFと、その内容を同じ順序で収録した日本語翻訳PDFを比較する。

## 実行

リポジトリルートから次を実行する。

```bash
uv run python skills/review-enja/scripts/run_review.py \
  --source ./inputs/source-en.pdf \
  --translation ./inputs/translation-ja.pdf \
  --output-dir ./outputs/review-enja \
  --glossary ./skills/review-enja/examples/glossary.csv \
  --review-rules ./skills/review-enja/examples/review-rules.md \
  --context-chars 50000 \
  --batch-chars 20000 \
  --max-batch-elements 0
```

Qdrantのドメイン根拠も使う場合だけ `--review-rag` を追加する。

## 成果物

- `source/`、`translation/`: `translate-ja-v4`のParseStage・NormalizeStage成果物
- `document.aligned.json`: 英日要素の対応とページ位置
- `document.reviewed.json`: 各Reviewer、Adjudicator、最終判定の監査情報
- `review.md`: 人が読むレビュー指摘票
- `manifest.json`: Alignment、Review、ReportのResume状態

## 作業時の参照

CLI、Stage、入出力schema、Resume、LLM回数は[spec.md](references/spec.md)を確認する。変更や検証では[test.md](references/test.md)も確認する。

## 制約

- `translate-ja-v4`を同じ`skills/`配下に置く。PDF ParseとNormalizeの単一実装を共有する。
- 組み込みpromptは役割、外部ルール遵守、出力schemaに限定し、品質基準は `--review-rules` へ置く。
- ログ本文は英語、既定levelはDEBUGとし、level名だけを色付きにする。
- 訳文を自動更新せず、修正案と根拠をレビュー成果物へ記録する。
