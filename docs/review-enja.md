# review-enja

`review-enja`は、同じ内容を同じ順序で収録した英語原文PDFと日本語翻訳PDFを比較し、翻訳レビューのJSON監査記録とMarkdown指摘票を生成するSkillである。

```bash
uv run python skills/review-enja/scripts/run_review.py \
  --source ./inputs/source-en.pdf \
  --translation ./inputs/translation-ja.pdf \
  --output-dir ./outputs/review-enja \
  --glossary ./skills/review-enja/examples/glossary.csv \
  --review-rules ./skills/review-enja/examples/review-rules.md
```

PipelineはParseStage、AlignmentStage、ReviewStage、ReportStageの順に実行する。PDF解析・正規化は `translate-ja-v4` の実装を共有するため、両Skillを同じ `skills/` directoryへ配置する必要がある。詳細な仕様と環境変数は [`skills/review-enja/references/spec.md`](../skills/review-enja/references/spec.md) を参照する。
