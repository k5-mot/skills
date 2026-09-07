---
name: translate-ja-v2
description: PDF/Word文書をDocling Serve、pypdfium2、LibreTranslateまたはOpenAI互換API、pandocで段階別JSON、ページ画像、日本語Markdown、Word docxへ変換し、manifestによる工程・要素単位Resumeを行う。Use when Codex runs or modifies scripts/translate.py, troubleshoots Parse/Normalize/Structure/Clean/Translate/Review/Markdown/Docx stages, validates translation output, or maintains the bundled DOTX template.
---

# translate-ja-v2

PDF/Word文書を解析し、構造補正、clean、日本語翻訳、レビュー、Markdown、Word docx生成までを実行する。

## 参照資料

作業前に、目的に対応する資料を読む。

- パイプラインの実行、Stageの処理順、Resume、障害調査: [workflow.md](references/workflow.md)
- `translate.py`、CLI、依存関係、データ、hash、API設定の変更: [spec.md](references/spec.md)
- テスト、統合検証、期待値の確認: [test.md](references/test.md)
- `examples/template.dotx` の作成、修正、検証: [template-format.md](references/template-format.md)

実装と文書が矛盾する場合は、実際に検証された `scripts/translate.py` とテストを確認し、同じ変更で対応する参照資料も更新する。

## 実行

`.env` にDocling ServeとLibreTranslateの接続情報を用意する。StructureまたはReviewでLLMを使う場合はOpenAI互換APIの接続情報も用意し、リポジトリルートから実行する。

```bash
uv run python skills/translate-ja-v2/scripts/translate.py \
  --input ./inputs/sample.pdf \
  --output-dir ./outputs/sample \
  --template ./skills/translate-ja-v2/examples/template.dotx
```

同じ設定と出力先で再実行し、validな完了工程と要素をResumeする。Structure、Translate、Reviewは要素単位、その他は工程単位である。全ステージの検証では `--skip-vlm`、`--skip-review`、`--skip-docx` を指定しない。

## 実装規則

1. 正本の実装は [scripts/translate.py](scripts/translate.py) に保ち、必要性のないwrapper、基底class、factory、package階層を増やさない。
2. Normalizeは座標によるtext順序と参照の補正だけを行う。
3. Structureは見出し階層、見出しと誤認識されたcaption、コードblock、コード連結、表セルinline codeをVLMで補正し、翻訳や全文再生成をさせない。
4. Cleanは非コード本文と表セルの3文字以上連続する `.` と `・` を3文字へ縮める。
5. Translateは既定でLibreTranslateを使い、`--translator llm` の場合だけOpenAI互換APIを使う。Reviewはbackendにかかわらず原文に一致する用語集を参照する。`--review-mode multi --review-rag` ではQdrant RAG、Fidelity Reviewer、Terminology Reviewer、必要時だけのAdjudicatorを使う。どちらも原文を上書きせず、`translate_ja_v2` metadataへ追加する。
6. 見出しと表タイトルは英日併記、本文は日本語、コード・URL・パス・識別子は原文を描画する。
7. PDFはpypdfium2で10ページずつ分割してDocling Serveへ直列送信し、参照とページ番号を再採番してJSONをローカル連結する。page imageもローカル生成し、Docling JSONの相対URIから正確に解決する。無関係な画像へfallbackしない。
8. JSONと成果物をatomic保存し、hash一致を確認してからResumeする。
9. ログ本文は英語、既定levelはDEBUGとする。開始・完了・省略・ResumeはINFO、反復的な詳細はDEBUGとし、secretや巨大payloadを出さない。
10. 外部APIを使うunit testはfake clientで検証し、変更後は [test.md](references/test.md) の該当検証を実行する。
