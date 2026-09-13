## Purpose

既存の英語PDFと日本語PDFをページまたは対応区間ごとに比較し、複数観点と外部根拠を用いた再開可能な品質ReviewをMarkdownとして提供する。

## ADDED Requirements

### Requirement: 比較Review CLI
ユーティリティは`review`サブコマンドで既存の英語PDFと日本語PDFを比較し、指定された`review.md`へ指摘を出力しなければならない（SHALL）。入力PDFを変更してはならない（SHALL NOT）。

#### Scenario: 二つのPDFをReviewする
- **WHEN** 利用者が`review --source source-en.pdf --destination translation-ja.pdf --output review.md`を実行する
- **THEN** 原文と訳文の対応箇所、重大度、理由および修正案を含む`review.md`が生成される

### Requirement: 複数観点の直列Review
比較Reviewは決定的検査、忠実性の批評、日本語品質の批評、必要時の最小修正案、最終検証から構成しなければならない（SHALL）。忠実性の批評は意味、欠落、追加、否定、条件および因果関係を検査し、日本語品質の批評は用語、文書内一貫性および自然さを検査しなければならない（SHALL）。すべての処理は同時実行数1で実行しなければならない（SHALL）。

#### Scenario: 二つの批評が異なる問題を検出する
- **WHEN** 訳文に意味の欠落と日本語用語の不統一が共存する
- **THEN** 両方の指摘を統合し、互いの指摘を失わず最終検証へ渡す

#### Scenario: 検証が再び不合格になる
- **WHEN** 最小修正案を一度差し戻した後も最終検証が不合格になる
- **THEN** 対象区間を失敗として記録し、Review全体を失敗終了させる

### Requirement: Reviewルールと根拠
比較Reviewは`templates/review-rules.md`を適用し、Structure用およびTranslation用Rulesを暗黙に適用してはならない（SHALL NOT）。Qdrantが設定されている場合、日本語品質の批評は既存collectionを検索し、本文とsource metadataを伴う根拠を引用した指摘だけを返さなければならない（SHALL）。検索根拠は修正案と最終検証にも渡さなければならない（SHALL）。

#### Scenario: Qdrantを利用できる
- **WHEN** Qdrant接続とReview用collectionが設定され、関連文書が検索できる
- **THEN** 日本語品質の指摘は参照本文とsource metadataを根拠として示す

#### Scenario: Qdrantを設定しない
- **WHEN** Qdrant接続が設定されていない
- **THEN** 外部参照なしで比較Reviewを継続する

#### Scenario: 設定済みQdrantの検索に失敗する
- **WHEN** Qdrantが設定されているが対象区間の検索に失敗する
- **THEN** 根拠なしの結果へ黙ってfallbackせず対象Reviewを失敗させる

### Requirement: 比較ReviewのResume
比較Reviewは出力ファイルと同じdirectoryの`.work-review/`にatomic更新する`state.json`と内部成果物を保存し、完了済みの対応付けとReview結果を再利用しなければならない（SHALL）。入力のいずれかが変わった通常実行はResumeせずエラーにしなければならない（SHALL）。

#### Scenario: 長い文書のReviewを再開する
- **WHEN** Reviewが途中で中断し、同じ二つのPDFと設定で再実行される
- **THEN** 完了済み区間を再利用して最初の未完了区間から再開する

#### Scenario: Review入力が変わる
- **WHEN** sourceまたはdestination PDFのSHA-256が前回状態と異なる
- **THEN** 通常実行を拒否し、強制的な新規Reviewが必要であることを示す

### Requirement: 比較Reviewの作業領域
`.work-review/`は`state.json`、source解析成果物、destination解析成果物、`aligned.json`および区間別Review成果物を保持しなければならない（SHALL）。公開成果物は指定された`review.md`だけでなければならない（SHALL）。

#### Scenario: Reviewが完了する
- **WHEN** 全区間のReviewと最終検証が完了する
- **THEN** 指定された場所に`review.md`が存在し、Resume用成果物は隣接する`.work-review/`内だけに存在する

### Requirement: Reviewの観測可能性
Langfuseが完全に設定されている場合、比較Reviewは入力原文、訳文、Rules、prompt、response、RAG根拠、指摘および修正案を階層的traceへ記録しなければならない（SHALL）。Langfuse障害は警告とし、Review判定そのものを変更してはならない（SHALL NOT）。

#### Scenario: Review traceを確認する
- **WHEN** Langfuseが設定された状態で比較Reviewを実行する
- **THEN** 実行から対応区間と各Review処理までを追跡できるtraceが記録される
