# Stage 詳細

この文書は `scripts/translate.py` の各 stage クラスを修正するときに読む。全体方針は `implementation-guide.md`、完全仕様は `spec-v2.md` を参照する。各クラスは、そのフェーズの変換、成果物保存、manifest 記録を担当する。

Parse、Normalize、Render、Docx は工程単位で Resume する。Structure は成功したページ内のtext refと表セルref、Translate は本文・見出し・表タイトル・表セル、Review はレビュー対象の各 ID を manifest に記録し、通常の出力 JSON を部分成果物として未完了要素から Resume する。再利用時は入力・設定・出力 hash を必ず照合する。

## 01 Parse

実装クラス: `ParseStage`

入力 PDF/Word を Docling Serve で解析し、Docling JSON、ページ PNG、figure/image artifact を保存する。接続設定は `python-dotenv` で読み込んだ `.env` の `DOCLING_SERVER_URL` / `DOCLING_API_KEY` 系を使う。

出力:

- `<stem>.json`
- `artifacts/*.png`

Parse Stage は Docling の文書モデルを可能な限りそのまま保存する。独自 Pydantic モデルへ全文コピーしない。
Docling ZIP内の唯一のJSONを、入力ファイルと同じstemの `<stem>.json` として保存する。

## 02 Normalize

実装クラス: `NormalizeStage`

`normalize_document` が入力JSONを複製し、座標に基づくtextsの読み順だけを補正する。

実装済みの主な処理:

- `prov[].page_no` と `prov[].bbox` による texts の読み順補正
- `BOTTOMLEFT` / `TOPLEFT` の座標原点の差を吸収した、ページ順・上から下・左から右の安定ソート
- texts の並べ替えに伴う `self_ref`、`$ref`、body/group children の参照更新
Normalizeでは本文、label、表セル、コードmetadataを変更しない。座標による並べ替えはpatchとして追跡する。

## 03 Structure

実装クラス: `StructureStage`

構造を第2段階で補正する。Normalizeの座標補正済みJSONを入力に、text要素、bbox、表セルをページごとに要約する。`build_multimodal_content` が `pages[].image.uri` から解決したpage PNGを1枚だけ添付し、VLM/LLMにコード判定と構造patchを返させる。URIや画像ファイルがない場合は、別の画像へフォールバックせずテキストだけを渡す。

補正順序は必ず次のとおりとする。

```text
Normalize: page + bbox の決定論的補正
    -> Structure: VLM による保守的補正
```

VLM に許可する操作:

- reorder
- merge
- semantic_type 変更
- 表セルのインラインコードspan設定

VLM に禁止する操作:

- 文書全文の再生成
- 原文の意味変更
- 翻訳
- validation 不能な任意 JSON の返却

VLM応答はJSON objectとして受け取り、`apply_structure_patches` で許可した操作だけを適用する。本文と誤認識されたコードは `set_label` でcodeへ変更し、前後の同一コードブロックは `merge_texts` で連結する。表セルの `set_table_cell_inline_code` は原文に完全一致するspanだけを `structure_ja_v2.inline_code_spans` へ保存する。requestのテキスト上限は `--context-chars`（既定50,000文字）を使い、各requestには対象ページの画像だけを添付する。

## 04 Translate

実装クラス: `TranslateStage`

翻訳対象を分類し、OpenAI 互換 API で日本語訳を追加する。原文は保持し、翻訳結果は各要素の `translate_ja_v2` metadata に追加する。

翻訳対象:

- 通常本文
- 見出し
- 表セルの自然言語
- 図表キャプション

見出しとその配下の下位見出し・本文を意味ブロックとし、同じレベルまたは上位レベルの見出しで次のブロックを開始する。見出し階層は翻訳 context に保持する。原文合計を `--batch-chars` の上限（既定1,500文字）以内に収めて、複数ブロックを1回の翻訳 request へまとめる。上限を超えるブロックは要素境界で分割するが、単独要素を途中で分割して文脈を壊さない。表はタイトルと翻訳対象セルを表単位でまとめ、上限を超える場合だけセル境界で分割する。request 全体には `--context-chars` の上限を適用する。

翻訳 request と response は要素IDを保持する。response のIDに欠落、追加、変更、重複がある場合は訳文を適用しない。

保護対象:

- コードブロック
- stack trace
- log block
- CLI command
- URL
- path
- identifier
- 数式

見出しと表タイトルは `英語 / 日本語` の render text を作る。本文は和訳のみを render text にする。コード、URL、path、identifier、command は原則として翻訳しない。

## 05 Review

実装クラス: `ReviewStage`

翻訳済み JSON を近接要素とあわせてレビューし、誤訳、用語集不一致、前後要素との表記ゆれ、近接範囲の文体ずれだけを保守的に補正する。

レビュー対象:

- 通常本文
- 見出し
- 表セルの自然言語
- 図表キャプション

レビュー対象外:

- コードブロック
- `translated=false` の保護対象
- 原文、構造、順序、label、表構造

LLM には structured output や JSON 形式を要求せず、文書順に1要素ずつ通常テキストとしてレビュー後の日本語訳だけを返させる。各要素の直前に前後の最新訳文を参照し、先に補正した表記を後続要素のレビューへ反映する。応答が空の場合は、その要素だけ元訳を使う。訳文を変更した場合は、対象IDと変更文字数をINFOログへ出力する。見出しと表タイトルはレビュー後も `英語 / 日本語` の render text を保つ。

`--skip-review` はこの工程だけを省略する。

## 06 Render

実装クラス: `RenderStage`

翻訳済み JSON から Markdown を生成する。Markdown は inspection しやすい最終出力として優先する。

出力優先順位:

1. 翻訳済み text
2. 翻訳対象外として保護された原文
3. 画像・figure 参照
4. table renderer の出力

コードブロックは fenced code block として保持し、翻訳しない。Markdown table が壊れる場合は、MVP 後に HTML table fallback を検討する。

## 07 Docx

実装クラス: `DocxStage`

Markdown と任意の reference docx/dotx を pandoc に渡し、最終 Word docx を生成する。pandoc が利用できない場合は明示的に失敗し、`--skip-docx` 指定時はこのフェーズ全体を実行しない。

## OpenAI 境界

現行実装は単一スクリプトを優先するため、API 呼び出しは `openai_client` と `chat_text` に集約する。過度な wrapper module は作らない。

エラー処理では timeout、rate limit、invalid structured output、validation failed を区別する。retry 方針は API client 境界で一元管理する。
