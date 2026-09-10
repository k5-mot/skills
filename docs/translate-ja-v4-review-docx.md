# translate-ja-v4 PDF対訳レビュー

`translate_ja_v4/review_docx.py` は英語原文PDFと日本語翻訳PDFを比較し、翻訳を編集せず指摘事項をJSONとMarkdownへ列挙する。

## 実行

```bash
uv run python skills/translate-ja-v4/scripts/translate_ja_v4/review_docx.py \
  --source ./inputs/source-en.pdf \
  --translation ./inputs/translation-ja.pdf \
  --output-dir ./outputs/pdf-review \
  --glossary ./skills/translate-ja-v4/examples/glossary.csv \
  --review-rules ./skills/translate-ja-v4/examples/review-rules.md \
  --context-chars 50000 \
  --batch-chars 20000 \
  --max-batch-elements 0
```

## 処理

1. `review-enja` のParse・Normalize・Alignmentを再利用し、二つのPDFの要素を多対多で対応付ける。
2. 対応結果を `document.translated.json` へ変換する。
3. `translate_ja_v4.stages.review.review_stage()` を直接実行する。
4. 現在訳とReviewStageの最終案が異なる項目、原文欠落、訳文追加を指摘事項として列挙する。

ReviewStageは内部の `document.reviewed.json` だけを更新する。`--source` と `--translation` のPDFは読み取り専用入力であり、上書きしない。

## 出力

```text
outputs/pdf-review/
├── source/                    # 原文PDFのParse・Normalize成果物
├── translation/               # 翻訳PDFのParse・Normalize成果物
├── document.aligned.json      # 英日要素の対応
├── document.translated.json   # v4 ReviewStageへのadapter入力
├── document.reviewed.json     # v4 ReviewStageの完全な出力
├── review.findings.json       # 指摘事項だけを持つ機械可読出力
├── review.md                  # 人が読む指摘票
└── manifest.json              # Parse・Alignment・ReviewのResume情報
```

`review.findings.json` と `review.md` は修正案を提示するだけで、PDFやWord文書への反映処理を持たない。
