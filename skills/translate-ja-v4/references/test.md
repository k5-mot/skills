# translate-ja-v4 テスト仕様

## 自動検証

リポジトリルートから実行する。

```bash
uv run ruff format --check skills/translate-ja-v4
uv run ruff check skills/translate-ja-v4
uv run ty check skills/translate-ja-v4/scripts
uv run pytest skills/translate-ja-v4/tests -q
uv run python skills/translate-ja-v4/scripts/run_pipeline.py --help
```

Skill metadataは次で検証する。

```bash
uv run python /home/penguin/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  skills/translate-ja-v4
```

## Unit test期待値

- path: 入力stem JSONと固定 `document.*` 名が生成される。
- PipelineOptions: API timeout/retry、Stage retry、PDF分割ページ数、LLM出力上限がCLIから型付き設定へ渡る。
- Parse: `#/texts/0` 等のref、page number、artifact URIをchunk offsetで再採番し、PDF spanのbbox、font、実効size、weightを抽出する。対応schemaの必須fieldと全参照を検証し、不正なchunkを再変換する。artifact directoryを置換してdirectory hashをmanifestへ保存し、欠落・改変時はResumeしない。
- Normalize: header/footer、図中文字、目次系ページを参照ごと削除する。座標順、本文・list・code断片、同一ページ/改ページtable断片を補正し、全参照を維持する。
- table: `grid`、`table_cells`、`cells` の更新先をStructure、Clean、Translate、Review、Markdownで共通して扱う。
- Structure: 対応spanをpayloadへ含め、許可されたpatchだけを適用し、code結合後もrefを削除しない。既知操作の短縮応答を正規化し、許可外の値を無視し、推定出力上限でも事前分割し、失敗batchを二分し、見出し階層の飛びを丸める。
- Clean: 本文と表セルのdot・中黒を3文字へ縮め、codeとinline codeを保持する。
- glossary: 8列を必須とし、英語2列だけで検索し、`note` と `reference` をprompt行から除く。
- Translate: backend共通契約、入力文字数・要素数・推定出力tokenによるbatch上限、structured ID照合、失敗時二分、metadata保存、付録内見出しだけの除外を確認する。LibreTranslateへはtext配列だけを送り、URL、path、command option、inline code、identifierを可逆placeholderで保持する。
- Review: 二Reviewerの並列分岐、Adjudicatorへの不一致限定、RAGのbatch検索、失敗時二分を確認する。
- Markdown: 見出し英日併記、本文日本語、code、表、inline code、画像を確認する。
- Docx: pandoc引数、水平線、図表SEQ、目次・図目次・表目次、field自動更新、連続見出し余白のOOXML補正を確認する。
- manifest: hash一致だけをResumeし、設定変更時は再実行する。
- logging: 本文は英語で、ANSI escapeがlevel名だけを囲む。
- ingest: 対応file収集、PDF/Word/text抽出、chunk overlap、安定ID、metadata、dry-run、Qdrant upsert、明示時だけの旧revision削除を確認する。

外部APIを使うunit testは必ずfakeまたはmonkeypatchを使い、実接続しない。

Qdrant接続前の実file確認にはdry-runを使う。

```bash
uv run python skills/translate-ja-v4/scripts/ingest_qdrant.py \
  --input ./docs/domain \
  --dry-run
```

## sample.pdf統合検証

`.env` に実サービスを設定し、出力先を専用directoryにして全Stageを実行する。

```bash
uv run python skills/translate-ja-v4/scripts/run_pipeline.py \
  --input ./inputs/sample.pdf \
  --output-dir ./outputs/sample-v4 \
  --template ./skills/translate-ja-v4/examples/template.docx \
  --glossary ./skills/translate-ja-v4/examples/glossary.csv \
  --structure-rules ./skills/translate-ja-v4/examples/structure-rules.md \
  --translation-rules ./skills/translate-ja-v4/examples/translation-rules.md \
  --review-rules ./skills/translate-ja-v4/examples/review-rules.md \
  --translator llm \
  --context-chars 50000 \
  --batch-chars 20000 \
  --max-batch-elements 0
```

期待値は全成果物が存在し、manifestの全Stageが `completed` または明示的な `skipped`、Structure/Translate/Reviewのcompleted数がtotalと一致することである。JSON pointerが解決でき、page image URIの全fileが存在し、各PDF pageに `text_spans` が存在し、Markdownに日本語が含まれ、docxをZIPとして開けることも確認する。

同じコマンドをもう一度実行し、各StageがINFOでResumeを報告することを確認する。次に途中成果物を複製した専用test出力で、manifestのStructure/Translate/Reviewを `running` にして一部completed情報を除き、未完了要素だけが呼ばれることをfake call counterで確認する。

## 手動Word確認

Microsoft Wordでdocxを開き、fieldを更新する。A4・余白、見出し、表、図、コード、水平線、ページ番号、ヘッダー/フッター、連続見出し間隔、目次・図目次・表目次、外部参照警告の有無を確認する。詳細な期待値は [template-format.md](template-format.md) に従う。
