# translate-ja-v2 実装仕様

この文書は `scripts/translate.py` の依存関係、CLI、データ、永続化、各ステージの契約を定める。処理手順の詳細は [workflow.md](workflow.md)、検証方法は [test.md](test.md)、Wordテンプレートは [template-format.md](template-format.md) を参照する。

## 1. スコープ

PDFまたはWord文書をDocling JSONへ変換し、座標正規化、VLM構造補正、決定論的clean、日本語翻訳、翻訳レビュー、Markdown、Word docxを一つのCLIで生成する。

実装の正本は `scripts/translate.py` とする。独自の全文書schema、ページ別stageディレクトリ、YAML設定、汎用Stage基底、factory、StageRunnerは現行仕様に含めない。複数実装の差し替えが必要になるまで追加しない。

## 2. 設計原則

```text
Document != State != Patch
```

- Document: Docling由来JSONと各ステージの成果物。
- State: `manifest.json` に置く実行状態、hash、要素進捗。
- Patch: Normalize、Structure、Cleanが適用した変更の操作、対象、理由。

原文を翻訳で上書きしない。各ステージは別成果物を作り、ファイル保存は可能な範囲でatomicに行う。

## 3. 実行環境とライブラリ

| 種別 | 要件・ライブラリ | 役割 |
| --- | --- | --- |
| Runtime | Python 3.12以上 | CLIと全ステージの実行 |
| Package | uv | 依存同期とPython実行 |
| CLI | Typer | option解析とhelp |
| Model | Pydantic v2 | 凍結設定modelと入力検証 |
| Environment | python-dotenv | `.env` 読込 |
| HTTP | HTTPX | Docling Serveのasync API |
| LLM | OpenAI Python SDK | OpenAI互換Chat Completions |
| PDF | pypdfium2 5.x | 10ページ単位のPDF分割、ページPNGの逐次生成 |
| Image | Pillow | PNG encode |
| Document | Docling Serve | PDF/WordからDocling JSONと要素画像への変換 |
| Render | pandoc | Markdownからdocxへの変換 |
| Test | pytest | 単体・統合テスト |
| Quality | Ruff | lintとformat確認 |
| Type | ty | 静的型検査 |

Python依存はリポジトリルートの `pyproject.toml` と `uv.lock` を正本とする。pandocは外部実行ファイルとしてPATHに必要であり、Python依存には含めない。

## 4. 環境変数

| 変数 | 必須となる工程 | 説明 |
| --- | --- | --- |
| `DOCLING_SERVER_URL` | Parse | Docling Serve base URL |
| `DOCLING_API_KEY` | Parse | Docling Serve API key |
| `OPENAI_BASE_URL` | Structure、Translate、Review | OpenAI互換base URL |
| `OPENAI_API_KEY` | Structure、Translate、Review | API key |
| `OPENAI_MODEL` | Structure、Translate、Review | 共通model名 |

Docling変数は互換名 `DOCLING_SERVE_URL`、`DOCLING_SERVE_API_KEY` も受理する。`--skip-vlm` でもTranslate/Reviewを使う限りOpenAI設定は必要である。

## 5. CLI

```text
--input PATH              必須。PDF/Word入力
--output-dir PATH         全成果物の出力先
--output PATH             最終docxだけ別パスへ出す
--template PATH           pandoc reference DOCX/DOTX
--skip-vlm                StructureのVLM呼出しを省略
--skip-review             Reviewを省略
--skip-docx               Docxを省略
--force                   Parseを強制再実行
--env PATH                dotenv。既定は .env
--glossary PATH           翻訳用CSV用語集
--translation-rules PATH  翻訳・レビュー用ルール文書
--context-chars INTEGER   OpenAI requestの最大テキスト文字数
--batch-chars INTEGER     翻訳・Review候補batchの最大原文・訳文文字数
```

既定値は `context-chars=50000`、`batch-chars=1500` で、いずれも1以上とする。

## 6. 固定値

| 項目 | 値 |
| --- | ---: |
| アプリログレベル | DEBUG |
| Docling全体timeout | 21,600秒 |
| Docling PDF chunk | 10ページ、直列処理 |
| PDF page image scale | 1.0 |
| PDF page image DPI | 72 |
| OpenAI timeout | 1,800秒 |
| OpenAI最大試行回数 | 6 |
| OpenAI retry初期待ち | 5秒 |
| OpenAI retry最大待ち | 60秒 |
| Structure最大出力 | 4,096 tokens |
| Translate・Review最大出力 | 16,384 tokens |
| Translate・Review推定応答上限 | 12,000文字 |
| Translate・Review最大要素数 | 20要素/batch |
| Review最大並列数 | 4バッチ |

HTTP 408、409、429、500、502、503、504と、connection、timeout、rate limit例外をretry対象にし、指数backoffを使う。Structureでは一時的な空応答と不完全JSONをAPI呼び出しからretryする。TranslateとReviewは20要素以内、推定応答JSONを12,000文字以内、完成messagesを `context-chars` 以内へ事前分割し、空応答、不正JSON、ID不一致の複数要素batchをさらに要素境界で分割する。バッチ内IDは文字列とJSON整数を受理して文字列へ正規化した後、完全一致を検証する。Translateは単一要素の生成不全も指数backoffで最大6回までretryする。Reviewは単一要素の生成不全、隣接要素の誤コピー、異常な長短、入力不足を訴えるメタ応答、日本語から英語のみへの退行を検出すると元の訳文を保持する。OpenAI SDK自体の自動retryは0にする。

## 7. Docling変換契約

Docling Serveへ送る主要設定は次のとおり。

```text
to_formats=json
do_ocr=false
force_ocr=false
ocr_preset=tesseract
ocr_lang=jpn,jpn_vert,eng
do_table_structure=true
table_mode=accurate
table_cell_matching=true
do_code_enrichment=true
do_formula_enrichment=true
include_images=true
include_page_images=false
images_scale=1.0
image_export_mode=referenced
target_type=zip
```

ZIPはJSONを正確に1件だけ含むこと。`artifacts` より外側のmemberは展開対象外とし、`..` を含む危険な相対パスは拒否する。

PDFは10ページ単位の一時PDFへ分割し、各チャンクを直列変換する。各結果のschemaとversion、および1始まりで連続する `pages` が期待ページ数と一致することを連結前に検証する。既存collection長をoffsetとして `self_ref` と `$ref` を再採番し、すべての `page_no` と `pages` keyへ先行ページ数を加算する。抽出artifactは `artifacts/chunk_<6桁>/` に分離して同名fileの衝突を避ける。

PDFページ画像は `artifacts/page_<6桁page>.png` とし、JSONのURIはJSONファイルのディレクトリから見た `artifacts/<filename>` とする。

## 8. ステージ契約

| Stage | 入力 | 出力 | 外部サービス | Resume粒度 |
| --- | --- | --- | --- | --- |
| Parse | 入力文書 | `<stem>.json`, `artifacts/` | Docling Serve（PDFは10ページずつ直列） | 工程 |
| Normalize | Parse JSON | `document.normalized.json` | なし | 工程 |
| Structure | Normalize JSON、page PNG | `document.structured.json` | OpenAI互換API | 要素 |
| Clean | Structure JSON | `document.cleaned.json` | なし | 工程 |
| Translate | Clean JSON、用語集、ルール | `document.translated.json` | OpenAI互換API | 要素 |
| Review | Translate JSON、ルール | `document.reviewed.json` | OpenAI互換API | 要素 |
| Markdown | Reviewed/Translated JSON | `document.ja.md` | なし | 工程 |
| Docx | Markdown、任意template | `document.ja.docx` | pandoc process | 工程 |

### Normalizeの境界

座標によるtext順序と関連refだけを変更する。text、label、level、table、pictureの意味内容を変更しない。

### Structureの境界

コードlabel、隣接するコードtextの結合、表セルinline code metadataだけを補正する。翻訳、要約、本文生成、見出し補正、順序変更はしない。patch適用時は存在するref、操作種別、値の型を検証し、結合本文は元textからローカル生成する。

### Cleanの境界

本文と表セルの `.` と `・` の3文字以上の連続だけを3文字へ縮める。コードと見出しは変更しない。

### Translateの境界

Docling原文と構造を変えず、`translate_ja_v2` metadataだけを追加する。ページヘッダー、ページフッター、文字や数字を含まない記号だけの要素は翻訳せず原文を描画値として保持する。同一contextと用語集はバッチ上部へ集約し、空fieldは送らない。APIではバッチ内連番IDを使い、応答後に元refへ戻す。完成messagesを `context-chars` 以内へ分割し、API応答のID集合は入力連番と完全一致させる。

### Reviewの境界

翻訳metadataの訳文と描画値だけを修正する。原文と構造は変更しない。原文と訳文の合計を `--batch-chars` 以内へ詰め、完成messagesを `context-chars` 以内へさらに分割し、最大4バッチを並列実行する。API入力はバッチ内連番ID、原文、訳文、非空inline codeだけとし、前後訳、見出しcontext、英語名だけの用語情報、空fieldは送らない。応答ID集合が入力連番と一致したバッチだけを採用し、元refへ戻してから完了状態を各要素について保存する。原文が異なる前後要素の訳文と95%以上一致し、かつ原訳との一致率が80%未満の応答は、ローカルで隣接要素の誤コピーとして棄却する。原訳の1.5倍を超えかつ200文字を超える応答、100文字以上の原訳を60%未満へ短縮する応答、Review入力の不足を訴えるメタ応答、日本語を含む原訳から日本語をすべて除く応答も棄却する。

### Renderの境界

見出しと表タイトルは英日併記、本文と自然言語表セルは日本語、コード・URL・パス・識別子は原文を使う。

## 9. Manifest schema

`manifest.json` の `schema_version` は2である。

```json
{
  "schema_version": 2,
  "run_id": "UUID",
  "created_at": "UTC ISO 8601",
  "updated_at": "UTC ISO 8601",
  "source": {
    "path": "/absolute/path/to/sample.pdf",
    "sha256": "..."
  },
  "stages": {
    "parse": {"stage": "parse", "status": "completed"},
    "normalize": {"stage": "normalize", "status": "completed"},
    "structure": {
      "stage": "structure",
      "status": "running",
      "elements": {
        "#/texts/0": {"status": "completed"},
        "#/texts/1": {"status": "pending"}
      }
    }
  },
  "events": []
}
```

各完了stageは `input_sha256`、`config_sha256`、`output`、`output_sha256` を持つ。Parseは `artifacts` と `artifacts_sha256` も持つ。Structure、Translate、Reviewは `elements` を持つ。要素状態は `pending` または `completed` である。

`events` は開始eventと、completed/skippedの監査履歴を保持する。最新の状態は `stages` を参照する。

## 10. Hashと設定変更

- ファイルは内容のSHA-256を使う。
- ディレクトリは相対パス、区切り、内容をsortしてSHA-256へ含める。
- JSON設定はkeyをsortしたcanonical JSONのSHA-256を使う。
- Structureの入力hashには、VLMを使う場合だけ `artifacts/` のhashを含める。
- template、用語集、翻訳ルール、model、context上限、batch上限は該当stageのconfig hashへ含める。

設定hashの中身はログへ展開せず、manifestにのみ保存する。

## 11. Atomic保存

ファイルは同一ディレクトリの一時ファイルへ書き、`flush()`、`fsync()`、`os.replace()` の順で置換する。JSONはUTF-8、`ensure_ascii=false`、indent 2とする。

Docling artifactsは一時ディレクトリへ完全展開した後にディレクトリ単位で置換する。失敗時は既存artifactsを復元する。

## 12. 用語集と翻訳ルール

用語集CSVには次のheaderを必須とする。

```csv
english,japanese,desc,genre,note
```

空の `english` は無視する。翻訳ルールファイルはUTF-8 textとしてそのままpromptへ渡す。未指定時の組み込みルールは、原文にない追加を禁止し、固有名詞、製品名、API名、コード、URL、パス、識別子、コマンドを保持し、用語集を優先する。

## 13. セキュリティと安全性

- API keyをログ、manifest、成果物へ保存しない。
- OpenAI SDKとHTTP clientの詳細ログはWARNING以上に抑える。
- page image URIは出力ディレクトリ外へ到達できない相対パスだけを受理する。
- ZIP memberは `artifacts/` 配下だけを抽出する。
- VLMの自由形式変更を直接適用せず、許可patchと対象refを検証する。
- 既存成果物はhash一致時だけResumeする。

## 14. 非目標

- OCRの自動切替
- PDFページの並列render
- stageごとの独立CLI
- arbitrary page指定での再実行
- 翻訳memoryや課金集計
- HTML table fallback
- 独自Docling document modelへの全面変換

必要性が確認されるまで、これらの抽象化や機能は追加しない。
