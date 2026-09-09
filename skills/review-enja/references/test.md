# review-enjaテスト仕様

## 自動検証

リポジトリルートから実行する。

```bash
uv run ruff format --check skills/review-enja
uv run ruff check skills/review-enja
uv run ty check skills/review-enja/scripts
uv run pytest skills/review-enja/tests -q
uv run python skills/review-enja/scripts/run_review.py --help
uv run python /home/penguin/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  skills/review-enja
```

外部APIを使うunit testはfakeまたはmonkeypatchを使い、実接続しない。

## Unit test期待値

- ParseStageは英日それぞれにv4のParse・Normalize設定を渡す。
- 本文、見出し、表題、`grid` / `table_cells` / `cells` を抽出し、code、header、footerを除外する。
- proportional batchは両言語の全IDと順序を維持する。
- Alignment応答のID欠落、重複、順序変更、空対応を拒否する。
- AlignmentとReviewのroot配列形式応答を受理する。
- FidelityとTerminologyを並列実行し、判断不一致だけをAdjudicatorへ送る。
- `source_only` と `translation_only` をLLMなしでmajor判定する。
- 用語集は英語2列で検索し、`note` と `reference` をpromptへ含めない。
- Review失敗時は要素境界でbatchを二分し、完成要素だけをResumeする。
- Reportは要修正項目だけを詳細表示し、JSONには全Reviewer案を保持する。
- manifestは入力・設定・成果物hashが一致する完成StageだけをResumeする。

## 統合検証

同じ内容の英日PDFと実サービスを用意して実行する。

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

期待値は全Stageがcompleted、英日normalized JSONのschemaと参照が正常、Alignmentで両文書の対象IDが重複・欠落なく1回ずつ使われ、Reviewのcompletedとtotalが一致し、`review.md` と `document.reviewed.json` の件数が整合することである。同じコマンドの再実行では全StageがResumeされる。
