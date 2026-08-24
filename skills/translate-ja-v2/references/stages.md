# Stage 詳細

この文書は `scripts/translate.py` の各 stage クラスを修正するときに読む。全体方針は `implementation-guide.md`、完全仕様は `spec-v2.md` を参照する。各クラスは、そのフェーズの変換、成果物保存、manifest 記録を担当する。

## 01 Parse

実装クラス: `ParseStage`

入力 PDF/Word を Docling Serve で解析し、Docling JSON、ページ PNG、figure/image artifact を保存する。接続設定は `python-dotenv` で読み込んだ `.env` の `DOCLING_SERVER_URL` / `DOCLING_API_KEY` 系を使う。

出力:

- `<stem>.docling.json`
- `artifacts/*.png`

Parse Stage は Docling の文書モデルを可能な限りそのまま保存する。独自 Pydantic モデルへ全文コピーしない。

## 02 Normalize

実装クラス: `NormalizeStage`

決定論的ルールで PDF 解析結果のノイズを補正する。現行実装では `normalize_document`、`normalize_text_item`、`normalize_table_item` を中心に、入力 JSON copy と patch 配列を返す。

実装済みの主な処理:

- `prov[].page_no` と `prov[].bbox` による texts の読み順補正
- `BOTTOMLEFT` / `TOPLEFT` の座標原点の差を吸収した、ページ順・上から下・左から右の安定ソート
- texts の並べ替えに伴う `self_ref`、`$ref`、body/group children の参照更新
- URL 保護付きの過剰記号・空白縮約
- table cell の空白・改行整形
- code/program_listing の翻訳対象外 metadata 付与

追加候補:

- Fragment Merge Rule
- Log Block Rule
- Hyphenation 高度化
- Stack Trace Detector 高度化
- Report Detector
- ASCII Table Detector

補正は必ず patch として追跡する。patch には最低限、operation、target、before/after または根拠、reason、rule_version を含める。

## 03 Structure

実装クラス: `StructureStage`

構造と reading order を第2段階で補正する。現行実装では、Normalize の座標補正済み JSON を入力に、`collect_structure_units` が text 要素と bbox を要約する。`build_multimodal_content` が `artifacts/` 内の page PNG を添付し、VLM/LLM に段組みなど座標だけでは曖昧な構造の patch を返させる。

補正順序は必ず次のとおりとする。

```text
Normalize: page + bbox の決定論的補正
    -> Structure: VLM による保守的補正
```

VLM に許可する操作:

- reorder
- merge
- split
- group
- semantic_type 変更

VLM に禁止する操作:

- 文書全文の再生成
- 原文の意味変更
- 翻訳
- validation 不能な任意 JSON の返却

VLM 応答は JSON object として受け取り、`apply_structure_patches` で `set_label`、`set_level`、`set_text`、`reorder_texts` だけを適用する。添付画像数は `TRANSLATE_JA_V2_MAX_VLM_IMAGES` で抑制できる。

## 04 Translate

実装クラス: `TranslateStage`

翻訳対象を分類し、OpenAI 互換 API で日本語訳を追加する。原文は保持し、翻訳結果は各要素の `translate_ja_v2` metadata に追加する。

翻訳対象:

- 通常本文
- 見出し
- 表セルの自然言語
- 図表キャプション

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

## 05 Render

実装クラス: `RenderStage`

翻訳済み JSON から Markdown を生成する。Markdown は inspection しやすい最終出力として優先する。

出力優先順位:

1. 翻訳済み text
2. 翻訳対象外として保護された原文
3. 画像・figure 参照
4. table renderer の出力

コードブロックは fenced code block として保持し、翻訳しない。Markdown table が壊れる場合は、MVP 後に HTML table fallback を検討する。

## 06 Docx

実装クラス: `DocxStage`

Markdown と任意の reference docx/dotx を pandoc に渡し、最終 Word docx を生成する。pandoc が利用できない場合は明示的に失敗し、`--skip-docx` 指定時はこのフェーズ全体を実行しない。

## OpenAI 境界

現行実装は単一スクリプトを優先するため、API 呼び出しは `openai_client` と `chat_text` に集約する。過度な wrapper module は作らない。

エラー処理では timeout、rate limit、invalid structured output、validation failed を区別する。retry 方針は API client 境界で一元管理する。
