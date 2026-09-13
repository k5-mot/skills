## Why

`skills/translate-ja-v4`は、翻訳、文書構造補正、状態管理、レビュー、検索用文書登録、DOCX後処理を一つのSkillへ重ねたため、利用と保守の両方が複雑になっている。v4との互換性を背負わない単目的のPythonユーティリティとして再設計し、翻訳精度と中断からの安全な再開を優先した最小コアを確立する。

## What Changes

- `scripts/translate-ja-v5/`に、PDFから日本語DOCXを生成する`translate`コマンドを追加する。
- 英語PDFと日本語PDFを比較して`review.md`を生成する`review`コマンドを追加する。
- Review用Qdrant collectionへ参照文書を安全に自動置換登録する`register`コマンドを追加する。
- Docling ServeとLiteLLM経由のOpenAI互換APIを必須サービスとし、LibreTranslate、Langfuse、Qdrantを用途別の任意連携にする。
- 翻訳中のReviewだけをLangGraphで表現し、Pipeline全体のResumeはatomic更新する`state.json`へ一本化する。
- 完了済みページを再利用し、モデル変更時は依存工程以降だけを再実行する。入力PDFの変更は通常エラー、`--force`時だけ全再構築を許可する。
- Pandocと同梱`template.docx`でDOCXを生成し、独自OOXML後処理を廃止する。PDF第1ページは表紙画像として転記し、翻訳本文から除外する。
- `Block`と`Inline`からなる小さな内部文書モデルを導入し、Docling JSONからMarkdownを決定的に生成する。
- v4のCLI、中間JSON、manifest、用語集形式との互換性は提供しない。Word入力も初版対象外とする。

## Capabilities

### New Capabilities

- `japanese-document-translation`: PDFを解析・構造化・翻訳・複数観点Reviewし、再開可能な処理で日本語DOCXを生成する契約。
- `bilingual-document-review`: 既存の英語PDFと日本語PDFを対応付け、根拠付きの比較ReviewをMarkdownへ出力する契約。
- `review-reference-registration`: Review参照文書をQdrantの同一collection内でrevision単位に安全に自動置換する契約。

### Modified Capabilities

なし。

## Impact

- 新規配置: `scripts/translate-ja-v5/`。既存の`skills/translate-ja-v4/`は変更しない。
- 公開CLI: `translate.py translate`、`translate.py review`、`translate.py register`。
- 必須実行環境: Python、Docling Serve、LiteLLMから到達できるOpenAI互換Chat Completions API、Pandoc。
- 任意連携: LibreTranslate、Langfuse、Qdrant。
- 主な新規依存候補: LangGraph、Langfuse SDK、Qdrant client、PDF画像化ライブラリ。依存は各機能を実装する最小範囲に限定する。
