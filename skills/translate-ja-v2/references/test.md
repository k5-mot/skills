# translate-ja-v2 テスト仕様

この文書は `translate-ja-v2` の検証手順と期待値を定める。コマンドはリポジトリルート `/home/penguin/repos/skills` を基準に記載する。

## 1. 検証レベル

| Level | 目的 | 外部依存 |
| --- | --- | --- |
| Unit | 変換規則、hash、Resume、API契約、renderを高速検証 | fake client、fake HTTP、fake pandoc |
| Static | lint、format、型整合性を検証 | なし |
| Integration | 実際のDocling/OpenAI/pandocで全ステージを検証 | `.env`、network、pandoc |
| Resume | 完了成果物が再利用されることを検証 | Integration成果物 |

## 2. 前提条件

Python依存を同期する。

```bash
uv sync
```

全ステージの統合検証では次も必要である。

- PATHから実行できるpandoc
- `DOCLING_SERVER_URL` と `DOCLING_API_KEY`
- `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`OPENAI_MODEL`
- 読み取り可能な入力PDF
- 書き込み可能な新規出力ディレクトリ

設定値そのものやAPI keyをテストログへ出さない。

## 3. Unit test

```bash
uv run pytest skills/translate-ja-v2/tests/test_translate_pipeline.py
```

成功条件は全testがpassし、skip、xfail、warning、errorが意図せず増えていないことである。テストは少なくとも次を覆う。

- 既定DEBUGログとANSI色
- 出力パスと入力stem名のParse JSON
- 成果物破損、入力変更、設定変更時のResume拒否
- bbox順序、座標なし要素のslot保持、Docling参照更新
- Normalizeが本文や表セルを変更しないこと
- Structureの許可patch、コードlabel、コード連結、順序補正
- 表セルinline codeのexact span
- page image URIの安全な解決と、無関係なPNGへfallbackしないこと
- context上限時のpairwise fallback
- Cleanによる `.` と `・` の圧縮とコード保護
- 翻訳対象、保護対象、見出し/本文/表の描画規則
- 用語集、翻訳ルール、意味ブロック、batch分割
- StructureのJSON出力指定、4,096 tokens上限、空応答・不完全JSONのrequest retry
- 翻訳応答IDの完全一致、空応答、部分応答、retry
- ReviewのID付きbatch、近接文脈、修正反映、不正応答の二分、空応答・隣接訳コピー・異常な長短・メタ応答・日本語消失での原訳保持、独立batchの並列実行
- Markdown renderer
- Docling async API、固定payload、pollingログ
- 23ページPDFの10、10、3ページ分割と直列変換、JSON参照・ページ番号・artifact URIの連結
- PDFページ画像のローカル生成とURI更新
- pandoc必須判定と相対画像の作業ディレクトリ
- Structure、Translate、Reviewの要素単位Resume
- 全ステージのpipeline wiring

## 4. 静的検証

```bash
uv run ruff check skills/translate-ja-v2
uv run ruff format --check skills/translate-ja-v2
uv run ty check skills/translate-ja-v2
```

3コマンドすべて終了コード0を期待する。format違反を検出した場合は、差分を確認してから `uv run ruff format skills/translate-ja-v2` を実行する。

## 5. Skill構成の検証

利用可能な場合はskill-creator付属validatorを使う。

```bash
python /home/penguin/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  skills/translate-ja-v2
```

成功条件は `Skill is valid!` が表示されることである。また、次を手動確認する。

- `SKILL.md` のfrontmatterに `name` と `description` だけがある。
- `references/` 直下に `template-format.md`、`workflow.md`、`spec.md`、`test.md` だけがある。
- `SKILL.md` の参照リンクがすべて存在する。
- `README.md` の章がタイトル、簡単な説明、Quick Start、Author、Licenseに限られる。

## 6. 全ステージ統合テスト

既存出力を誤ってResumeしないよう、新規の出力ディレクトリを指定する。

```bash
uv run python skills/translate-ja-v2/scripts/translate.py \
  --context-chars 50000 \
  --batch-chars 5000 \
  --input ./inputs/sample.pdf \
  --output-dir ./outputs/sample-full-validation \
  --template ./skills/translate-ja-v2/examples/template.dotx \
  --glossary ./skills/translate-ja-v2/examples/glossary.csv \
  --translation-rules ./skills/translate-ja-v2/examples/translation-rules.md
```

全ステージの確認なので、`--skip-vlm`、`--skip-review`、`--skip-docx` は指定しない。出力先がすでに存在する場合は、削除せず別名の新規ディレクトリを使う。

## 7. 統合テストの期待値

### ファイル

入力が `sample.pdf` の場合、次がすべて存在する。

```text
sample.json
document.normalized.json
document.structured.json
document.cleaned.json
document.translated.json
document.reviewed.json
document.ja.md
document.ja.docx
artifacts/
manifest.json
```

すべてのJSONがparse可能で、Markdownとdocxが空でないこと。docxはZIPとして破損していないこと。

### Parse

- `sample.json` の `pages` 件数がPDFページ数と一致する。
- Docling ServeへのPDF送信が最大10ページであり、チャンク番号順に直列実行される。
- `self_ref`、`$ref`、`page_no` が連結後の全体index・ページ番号を指す。
- 抽出画像のURIが `artifacts/chunk_<6桁>/...` を指し、チャンク間で同名画像が衝突しない。
- 各 `pages[].image.uri` が `artifacts/page_<6桁>.png` を指す。
- URIを出力ディレクトリ基準で解決したPNGが存在する。
- PNG件数がPDFページ数以上である。
- manifestのParseに `artifacts_sha256` がある。

### Normalize

- text要素の内容と件数がParse成果物から変わっていない。
- bboxを持つtextがページ、上、左の順に配置される。
- `$ref` と `self_ref` が有効なindexを指す。

### Structure

- manifestの `elements` が空でなく、すべて `completed` である。
- VLMが返したpatch以外の原文変更がない。
- コードへ変更した要素のlabelが `code` または `program_listing` である。
- 表セルinline code spanがある場合、各spanがセル原文に完全一致する。

### Clean

- 非コード本文・表セルに `....` や `・・・・` が残っていない。
- code要素とinline code spanはStructure成果物から変わっていない。

### Translate

- manifestの全対象要素が `completed` である。
- 翻訳対象要素に `translate_ja_v2` がある。
- 元のtext、caption、cell textが保持される。
- 見出しと表タイトルの `render_text` は英日併記である。
- 本文と自然言語セルの `render_text` は日本語訳である。
- コード、URL、パス、識別子は `translated=false` で原文を保持する。

### Review

- manifestの全レビュー対象が `completed` である。
- 原文、label、順序、表構造がTranslate成果物から変わっていない。
- 変更は翻訳metadataの訳文と描画値に限られる。

### Markdownとdocx

- `document.ja.md` に見出し、本文、表、コード、画像が文書内容に応じて出力される。
- 画像リンクが `artifacts/...` の相対パスである。
- pandocが画像を解決し、`document.ja.docx` を生成する。
- `document.ja.docx` を `unzip -t` で検査してerrorがない。

### Manifest

`stages` の次の8項目がすべて `completed` である。

```text
parse normalize structure clean translate review markdown docx
```

各stageの `input_sha256`、`config_sha256`、`output_sha256` が存在し、現在の成果物と一致する。`skipped` は一件もないこと。

## 8. Resume統合テスト

全ステージ成功後、まったく同じコマンドを同じ出力先で再実行する。

期待値は次のとおり。

- 8ステージすべてで `Resuming completed stage` のINFOログが出る。
- Docling polling、OpenAI batch、PDF page rendering、pandoc実行が発生しない。
- 成果物のhashが変わらない。
- manifestの各stageが `completed` のままである。

要素単位Resumeはunit testで再現する。実APIテストで故意に中断する場合は、ユーザーが所有する既存出力ではなく専用の一時出力先を使う。

## 9. 障害時の確認

| 症状 | 確認項目 |
| --- | --- |
| Doclingが失敗 | endpoint、HTTP status、task status、timeout、ZIP内JSON件数 |
| page imageがない | PDF page数、`pages` key、URI、PNGの存在、出力ディレクトリ基準の解決 |
| Structureが失敗 | `--context-chars`、page単位prompt、fallback、許可patch、対象ref |
| 翻訳ID不一致 | request ID集合、response ID集合、batch分割ログ |
| Reviewが空 | WARNING後に原訳が保持されたか |
| docxが失敗 | `pandoc --version`、template存在、Markdown相対画像 |
| Resumeされない | input/config/output hash、stage status、成果物存在 |

修正後は、失敗を再現するunit testを追加してから、Unit、Static、必要なIntegrationの順に再検証する。
