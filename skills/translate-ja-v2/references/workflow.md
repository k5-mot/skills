# translate.py ワークフロー

この文書は `scripts/translate.py` の実行フローと、各ステージで行う処理の正本である。実装上の定数、データ契約、依存ライブラリは [spec.md](spec.md)、検証手順は [test.md](test.md) を参照する。

## 1. 全体フロー

`run_pipeline()` は次の順序で具体的なStageクラスを呼び出す。

```text
CLI引数解析
  ↓
.env読込・ログ初期化・出力パス構築
  ↓
Parse → Normalize → Structure → Clean → Translate → Review
  ↓
Markdown → Docx
```

前段の成果物を次段の入力にし、各JSONは別ファイルへ保存する。翻訳で原文を上書きせず、翻訳結果は `translate_ja_v2` metadataとして追加する。

## 2. 起動

1. TyperがCLI引数を解析する。
2. `--env` のdotenvファイルを `python-dotenv` で読み込む。既存の環境変数は上書きしない。
3. アプリログを初期化する。既定はDEBUG、依存ライブラリはWARNING以上とする。
4. 入力パスと出力パスを絶対パスへ解決する。
5. 出力ディレクトリを作成し、入力パスとSHA-256を `manifest.json` に記録する。
6. 各ステージを固定順序で実行する。

中断時は終了コード130、その他の未処理例外はstack traceを記録して終了コード1を返す。正常終了時はMarkdownとdocxのパスをINFOログへ出す。

## 3. 出力構成

入力が `sample.pdf` の場合、標準の構成は次のとおり。

```text
outputs/sample/
├── sample.json
├── document.normalized.json
├── document.structured.json
├── document.cleaned.json
├── document.translated.json
├── document.reviewed.json
├── document.ja.md
├── document.ja.docx
├── artifacts/
└── manifest.json
```

Docling直後のJSONだけは入力stemをファイル名に使う。他の成果物名は固定である。`--output-dir` を省略すると `outputs/<入力stem>/`、`--output` を省略すると `<output-dir>/document.ja.docx` を使う。

## 4. Resumeの流れ

`manifest.json` は文書内容ではなく、実行状態と成果物の整合性を管理する。

各ステージは次を照合する。

- ステージ状態が `completed` である。
- 現在の入力SHA-256が記録値と一致する。
- 現在の設定SHA-256が記録値と一致する。
- 主成果物が存在し、SHA-256が記録値と一致する。
- Parseでは `artifacts/` のディレクトリSHA-256も一致する。

すべて一致すれば工程全体を再利用する。上流成果物や設定が変わると入力hashが変わるため、その工程と下流工程が順に再実行される。`--force` はParseの再利用だけを禁止する。

Structure、Translate、Reviewは実行中にも通常の出力JSONと `manifest.json` をatomic保存する。再起動時にステージ状態が `running` で、入力・設定・部分成果物hashが一致すれば、`elements` が `completed` の要素を残し、未完了要素から続行する。専用checkpointファイルは作らない。

Parse、Normalize、Clean、Markdown、Docxは工程単位の状態だけを持つ。

## 5. ParseStage

### 入力と出力

- 入力: PDFまたはWord文書
- 主出力: `<入力stem>.json`
- 追加出力: `artifacts/`

### 処理

1. PDFはpypdfium2で先頭から10ページずつ一時PDFへ分割する。Word文書は分割しない。
2. 各入力をDocling Serveの `/v1/convert/file/async` にmultipartで直列送信する。同時送信はしない。
3. `task_id` を取得し、`/v1/status/poll/{task_id}` を10秒間隔でpollする。
4. 成功後、`/v1/result/{task_id}` からZIPを取得する。
5. ZIP内にJSONが正確に1件あることを検証する。PDFではチャンクごとの `artifacts/` を `artifacts/chunk_000001/` 形式で分離する。
6. PDFの各JSONについて、collection indexを基に `self_ref` と `$ref` を再採番し、`page_no` と `pages` keyへ先行ページ数を加算する。artifact URIもチャンク別pathへ変更する。
7. `groups`、`texts`、`pictures`、`tables`、`key_value_items`、`form_items`、`body.children`、`furniture.children`、`pages` をページ順に連結する。schemaとversion、各チャンクのページ数が一致しない場合は失敗させる。
8. 元PDFのname、filename、hashを設定し、入力stem名のJSONとしてatomic保存する。
9. PDFではpypdfium2を使い、元PDF全体をscale 1.0、72 DPIで1ページずつPNG化する。
10. PNGを `artifacts/page_000001.png` 形式で保存し、Docling JSONの `pages[page_no].image.uri` を `artifacts/<filename>` に更新する。

Docling Serveには `include_page_images=false` を送り、ページ全体画像はローカルで生成する。加えてPDFを10ページ単位で直列処理し、サーバーが文書全体を一度に保持する負荷を抑える。図などの抽出画像を得るため `include_images=true` は維持する。

PDFチャンクは一時ディレクトリで処理し、全チャンクが成功した場合だけ連結JSONとartifactsを公開する。Parse途中で失敗した場合のResumeはチャンク単位ではなく工程単位であり、先頭チャンクから再実行する。

PDFページ、bitmap、Pillow imageは各ページ保存後に閉じ、全ページをメモリへ保持しない。ページ数とDocling JSONの `pages` が対応しない場合は失敗させる。

## 6. NormalizeStage

### 入力と出力

- 入力: `<入力stem>.json`
- 出力: `document.normalized.json`

### 処理

Normalizeは座標に基づくtext要素の並べ替えだけを行う。本文、label、表セル、コード判定、句読点には手を加えない。

1. 各textの `prov[].page_no` と `prov[].bbox` を取得する。
2. `coord_origin` が `BOTTOMLEFT` か `TOPLEFT` かを考慮して上端位置を求める。
3. ページ、上から下、左から右、元indexの順に安定sortする。
4. 座標を持つ要素だけを、元々座標要素が占めていたslot内で並べ替える。座標のない要素は元のslotに残す。
5. textsの `self_ref` と文書内の `$ref`、children参照を新しいindexへ合わせる。
6. 変更があれば `bbox_reading_order` patchを作り、manifestにはpatch件数を記録する。

## 7. StructureStage

### 入力と出力

- 入力: `document.normalized.json` と `artifacts/` のページPNG
- 出力: `document.structured.json`
- 進捗粒度: text refと表セルref

### 目的

Doclingが本文として検出したコードをコードブロックへ補正し、同じコードブロックに属する前後要素を連結する。表セルでは、自然言語中のインラインコードをexact spanとして特定する。

### ページ画像の解決

ページ番号に対応する `pages[].image.uri` を読み、JSONが置かれる出力ディレクトリを基準に相対パスを解決する。scheme、絶対パス、`..` を含むURIは拒否する。対応する画像がない場合は、別のPNGを推測せずテキストのみで処理する。

### 通常処理

ページごとに次をVLMへ渡す。

- ページ番号
- 座標補正済みtextのref、bbox、label、最大500文字のtext
- 表セルのref、bbox、最大500文字のtext
- 対応するページPNG

VLMには翻訳、要約、本文の創作を許可しない。返却patchは次だけを受理する。

- `set_label`。labelは `code` または `program_listing` だけを受理する。
- `merge_texts`。現在の文書上で隣接するtextだけを受理する。
- `set_table_cell_inline_code`

APIにはJSON object形式と最大4,096出力tokensを指定する。HTTP成功でも空本文を返した場合や、本文が不完全なJSONでparseできない場合は、一時的な生成失敗としてAPI呼び出しから指数backoffで再試行する。

コード片を連結するときはVLMが生成した本文とlabelを採用せず、元の隣接textを文書順に改行で結び、labelを `code` にする。削除されたtextに対するDocling参照は連結先へ更新し、残るtextのindexと参照を再整合する。表セルの `code_spans` はセル原文に完全一致する文字列だけを受理し、`structure_ja_v2.inline_code_spans` に保存する。

### Context上限時のfallback

ページ単位promptが `--context-chars` を超える場合は次へ切り替える。

1. 同一ページで隣接する2要素だけを比較し、コードlabelとコード連結を判断する。
2. 表セルはcontext上限内のまとまりに分割する。

順序はNormalizeの出力を正とし、Structureでは並べ替えない。配列順とページ単位の入力から分かる `coordinate_order` と要素ごとのpageはpromptへ重複して渡さない。

各ページのtextと表セルが完了するたびに部分成果物と要素進捗を保存する。`--skip-vlm` の場合はVLM補正を行わず、Normalize結果をStructure成果物として保存する。

## 8. CleanStage

### 入力と出力

- 入力: `document.structured.json`
- 出力: `document.cleaned.json`

### 処理

本文と表セルにある3文字以上の連続記号を次の3文字へ統一する。

```text
....    → ...
......  → ...
・・・・ → ・・・
・・・・・・ → ・・・
```

2文字以下と、すでに3文字の並びは変えない。見出し、コードブロック、表セル内の `structure_ja_v2.inline_code_spans` は保護する。外部APIを使わず、工程単位でResumeする。

## 9. TranslateStage

### 入力と出力

- 入力: `document.cleaned.json`、任意の用語集と翻訳ルール
- 出力: `document.translated.json`
- 進捗粒度: text ref、表タイトルref、表セルref

### 翻訳対象

texts、表タイトル、自然言語を含む表セルを翻訳する。コードブロック、ページヘッダー、ページフッター、記号だけの要素、URL、パス、コマンド、識別子、コードだけの表セルは翻訳しない。ページ装飾と記号は原文を描画値として保持する。

見出しと配下要素を意味ブロックにする。同じレベルまたは上位の見出しで次のブロックを開始し、ブロック単位を保ちながら原文合計を `--batch-chars` 以内へ詰める。上限を超えるブロックは要素境界で分割し、単一要素だけで上限を超える場合はその要素を単独候補にする。表はタイトルとセルを同様に詰める。

各候補を最大20要素に分け、推定翻訳応答JSONが安全上限12,000文字を超える場合も要素境界で事前分割する。さらにsystem prompt、翻訳ルール、共有文脈、共有用語集、入力JSONを含む完成messagesを作り、テキスト全体が `--context-chars` を超える候補も分割する。単一要素でもいずれかの文字数上限を超える場合はAPIを呼ばずに失敗させる。

同じ見出し階層は共有文脈辞書へ一度だけ置き、各要素は短い `context_id` で参照する。用語集もバッチ内で重複を除いて一度だけ置く。空のinline code fieldは送らない。APIには長いDocling refの代わりにバッチ内の連番IDを渡し、入力件数と返却必須ID一覧も明示する。応答後に元refへ戻す。JSON objectの `translations` で同じ連番ID集合を返させ、文字列またはJSON整数のIDを正規化して検証する。IDの欠落、追加、変更、重複、空訳は採用しない。空応答または部分応答で複数要素がある場合は要素境界で二分して再実行する。単一要素でも生成不全なら指数backoffで最大6回まで再試行し、正常な訳を得られなければ失敗させる。

`--glossary` は `english,japanese,desc,genre,note` 列を持つCSVである。対象原文に `english` が含まれる行だけをpromptへ加える。`--translation-rules` を省略した場合は組み込みルールを使う。

結果は元要素の `translate_ja_v2` に追加する。

- 見出し: 原文を `text_en`、訳文を `text_ja`、描画値を `英語 / 日本語` にする。
- 本文: 原文と訳文を保持し、描画値は日本語だけにする。
- 表タイトル: `caption_en`、`caption_ja`、描画値 `英語 / 日本語` を保持する。
- 表セル: 原文と訳文を保持し、描画値は日本語だけにする。
- 非翻訳対象: 原文を描画値として保持し、`translated=false` にする。

## 10. ReviewStage

### 入力と出力

- 入力: `document.translated.json` と翻訳ルール
- 出力: `document.reviewed.json`
- 進捗粒度: 翻訳済みtext、表タイトル、表セルのref

翻訳済み要素を原文と訳文の合計が `--batch-chars` 以内となる候補へ詰める。20要素を超える候補、現在の訳文を使った推定Review応答JSONが安全上限12,000文字を超える候補、完成messagesが `--context-chars` を超える候補は要素境界でさらに分割し、最大4バッチを並列実行する。

APIへ送る各要素はバッチ内連番ID、原文、現在の訳文、存在する場合だけ保護対象inline codeを持つ。入力件数と返却必須ID一覧を明示し、入力配列の順序を文書順として表記ゆれを確認させる。長いDocling ref、見出し文脈、用語集の英語名、直前・直後の訳文、空fieldは送らない。連番IDは応答後に元refへ戻す。前後要素の原文と訳文はAPI入力ではなく、誤コピーを棄却するローカル検証だけに使う。

応答IDの欠落、追加、変更、重複、空訳、JSON不正がある複数要素バッチは要素境界で二分して再実行する。単一要素でも正常応答を得られない場合は原訳を保持する。正常なID付き応答でも、異なる原文を持つ隣接要素の訳文と95%以上一致する応答、原訳の1.5倍を超えかつ200文字を超える応答、100文字以上の原訳を60%未満へ短縮する応答、Review入力の不足を訴えるメタ応答、日本語を含む原訳から日本語をすべて除く応答は原訳へ戻す。バッチ完了時に含まれる各IDを要素単位で完了記録するため、中断後は未完了要素だけを再びバッチ化する。変更できるのは `translate_ja_v2` の訳文と描画値だけであり、原文、Docling構造、順序、label、表構造は変更しない。`--skip-review` ではこの工程を `skipped` と記録し、翻訳済みJSONからMarkdownを生成する。

## 11. RenderStage

### 入力と出力

- 入力: 通常は `document.reviewed.json`。Review省略時は `document.translated.json`
- 出力: `document.ja.md`

texts、tables、picturesを文書順に集め、次の形式へ変換する。

- 見出し: 見出しlevelに対応するMarkdown heading
- コード: fenced code block
- 本文: 翻訳metadataの描画値
- 表: Markdown table
- 画像: Docling JSONのURIを使うMarkdown image

表セル内のinline code spanはbacktickで囲む。画像URIは書き換えず、出力ディレクトリから見た相対パスとしてMarkdownへ出す。

## 12. DocxStage

### 入力と出力

- 入力: `document.ja.md` と任意のreference DOTX/DOCX
- 出力: `document.ja.docx` または `--output` のパス

Markdownのディレクトリを作業ディレクトリにしてpandocを呼ぶ。これにより `artifacts/...` の相対画像URIを解決できる。`--template` 指定時は `--reference-doc` として渡す。pandocが見つからない場合はエラーにする。`--skip-docx` では工程を `skipped` と記録する。

## 13. ログ

ログ本文は英語とし、ANSI対応端末ではレベル名を色分けする。

| Level | 色 | 用途 |
| --- | --- | --- |
| DEBUG | cyan | polling、バッチ、ページ画像、要素変更など反復的な詳細 |
| INFO | green | ステージ開始・完了・省略・Resume、処理全体の完了 |
| WARNING | yellow | 一時エラーのretry、画像欠落、hash不一致、空レビュー |
| ERROR | red | 処理失敗 |
| CRITICAL | magenta | 致命的障害 |

APIキー、request header、base64画像、全文payload、巨大なhash値を通常ログへ出さない。
