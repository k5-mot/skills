# Stage 詳細

この文書は Stage 実装時に読む。全体方針は `implementation-guide.md`、完全仕様は `spec-v2.md` を参照する。

## 01 Parse

入力 PDF を Docling で解析し、DoclingDocument、ページ PNG、figure/image artifact を保存する。

出力:

- `stages/01_parse/document.json`
- `stages/01_parse/pages/000001.json`
- `assets/pages/page_000001.png`
- `assets/figures/*`

Parse Stage は Docling の文書モデルを可能な限りそのまま保存する。独自 Pydantic モデルへ全文コピーしない。

## 02 Normalize

決定論的ルールで PDF 解析結果のノイズを補正する。Rule はファイル I/O を持たず、入力 page/document と context から patch と更新後 artifact を返す。

MVP で実装する rule:

- Ellipsis Rule
- Table Artifact Rule
- Fragment Merge Rule
- Log Block Rule

MVP 後に扱う rule:

- Hyphenation 高度化
- Stack Trace Detector 高度化
- Report Detector
- ASCII Table Detector

補正は必ず patch として追跡する。patch には最低限、operation、target、before/after または根拠、reason、rule_version を含める。

## 03 Structure

構造と reading order を補正する。heuristic で十分なページは VLM を呼ばず、必要なページだけ VLM に送る。

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

VLM 応答は structured output として受け取り、参照先の存在、page 範囲、重複、欠落、循環、型変更の妥当性を検証してから適用する。

## 04 Translate

翻訳対象を分類し、chunk を作成し、OpenAI API で日本語訳を追加する。原文は保持し、翻訳で上書きしない。

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

Structured translation output は、chunk id、source hash、translated text、protected spans を含む。source hash が一致しない翻訳結果は破棄する。

## 05 Render

翻訳済み Document Artifact から Markdown を生成する。Markdown は inspection しやすい最終出力として優先する。

出力優先順位:

1. 翻訳済み text
2. 翻訳対象外として保護された原文
3. 画像・figure 参照
4. table renderer の出力

コードブロックは fenced code block として保持し、翻訳しない。Markdown table が壊れる場合は、MVP 後に HTML table fallback を検討する。

## OpenAI 境界

API 呼び出しは `openai/` 配下に閉じ込める。Structure や Translation のドメインロジックへ SDK 呼び出しを直書きしない。

エラー処理では timeout、rate limit、invalid structured output、validation failed を区別する。retry 方針は StageRunner または API client 境界で一元管理する。
