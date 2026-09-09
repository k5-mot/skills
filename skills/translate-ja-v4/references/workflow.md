# run_pipeline.py ワークフロー

## 起動と出力

`run_pipeline.py` はdotenvを読み、ログを初期化して、入力、外部rules、batch、timeout、API retry、Stage retry、PDF分割ページ数をまとめた `PipelineOptions` をLangGraphへ渡す。既定の出力構成は次のとおりである。

```text
outputs/<document_filename>/
├── <input-stem>.json
├── document.normalized.json
├── document.structured.json
├── document.structure-audit.json
├── document.cleaned.json
├── document.translated.json
├── document.reviewed.json
├── document.ja.md
├── document.ja.docx
├── artifacts/
├── manifest.json
└── .langgraph.sqlite3
```

## Resume

各Stageは入力hash、設定hash、出力hashをmanifestと比較する。一致する完了Stageは成果物を再利用する。Parse、Normalize、Clean、Markdown、DocxはStage単位、Structure、Translate、Reviewは要素単位で進捗を保存する。

Structureの処理済みIDは `completed_elements`、Translate/Reviewは各要素の `translate_ja_v4` / `review_ja_v4` metadataで判定する。各成功batchの直後に成果物とmanifestをatomic保存するため、中断後は未完了要素から続行する。CLI設定、rules、glossary、model、backendを変えると設定hashが変わり、該当Stage以降を再実行する。

## ParseStage

PDFを既定10ページずつ一時PDFへ分け、Docling Serveへ順番に送る。分割数は `--pdf-chunk-pages` で変更できる。server側page image生成は無効にする。返却ZIPからJSONとartifactを取り出し、collection index、JSON pointer、page number、artifact URIを全体文書へ再採番して連結する。

各chunkの返却直後に、`DoclingDocument` schema、versionの存在、必須collection、ページ数、`self_ref`、全JSON pointer、provenanceのpage参照、table cell形式を検証する。version番号そのものは固定せず、対応fieldを満たすschemaを受け入れる。検証失敗時は同じchunkの変換から指数backoffで再試行し、不完全なJSONを後続Stageへ渡さない。

連結後、元PDFをpypdfium2で直接走査する。各PDF text objectから原文、bbox、font名、実効文字サイズ、weightを抽出して `pages[*].text_spans` に保存し、全ページをPNG化して `pages[*].image.uri` を `artifacts/page_XXXXXX.png` にする。spanは翻訳対象ではなく、StructureStageが見出しlevelを判断する根拠である。

Word入力は分割せずDocling Serveへ送る。JSONとartifact URIの整合性を確認後、artifact staging directoryを `os.replace` で切り替え、最後にJSONをatomic保存する。manifestにはartifact全fileの相対path、size、SHA-256から算出したdirectory hash、件数、合計byte数を保存する。Resume時はdirectory hashも一致した場合だけParse成果物を再利用する。`--force` があれば完了成果物を再利用しない。

## NormalizeStage

最初に次を削除し、collectionを詰めて `self_ref`、`$ref`、body/furniture treeを更新する。

- `page_header`、`page_footer` labelのtext
- picture配下のtext。ただしcaptionは残す
- `document_index` labelが存在するページ上の全要素。目次、図目次、表目次を同じ扱いにする

残った座標付きtextをpage、縦位置、横位置で並べ替える。座標がない要素のslotは保つ。その後、同じ親・ページ・行またはPDF spanに属する本文/list断片、近接している改行分割本文、既にcodeと判定された断片を連結する。codeは改行、それ以外は必要な空白で結ぶ。

同じ列数を持ち、同一ページで近接するか連続ページの下端・上端に接するtableは連結する。重複したheader rowを除き、grid、table_cellsまたはcells、provを統合する。ここでは明白な断片だけを扱い、本文をcodeへ再分類するような意味判断はStructureStageへ残す。

## StructureStage

ページごとにtext、対応するPDF span、table cellのcompact JSONを作り、ローカルpage imageと一緒にLangChain structured modelへ渡す。spanの文字サイズ、font、weight、画像上の位置を根拠に、見出しlevel、見出しとcaptionの誤検出、本文として検出されたcode、隣接code結合、表セルinline codeを外部ルールに従って補正する。

コード結合では左要素へ原文を結合し、右要素を空にして `merged_into` を記録する。要素を配列から削除しないため、後続batchとResumeでrefが変化しない。VLMが返した各patchは `document.structure-audit.json` へ、安定ID、ページ、patch、適用・拒否、変更前後、理由を記録する。監査ファイルも各成功batch後にatomic保存する。

全VLM補正後、先頭見出しをlevel 1以下、後続見出しを直前より最大1段深いlevelへ決定論的に丸め、階層の飛びを残さない。この最終補正は `--skip-vlm` でも実行する。promptが `context_chars` を超えないよう事前分割し、全要素にpatchが返る最悪ケースの推定出力が `max_output_tokens` を超える場合も要素境界で分割する。API失敗時は要素数を半減する。

## CleanStage

非コード・非見出し本文、および表セルの `...` 以上の連続dotと `・・・` 以上の中黒をそれぞれ3文字へ縮める。Structureが検出したinline code spanは保護する。文書順や構造は変更しない。

## TranslateStage

英字を含む本文、見出し、table caption、table cellを対象にする。table cellは `data.grid`、`data.table_cells`、`data.cells` を共通iteratorで走査し、Structure、Clean、Translate、Review、Markdownで同じ更新先pathを使う。code、page header/footer、および `APPENDIX <番号または英字>` 以降の付録内見出しは除外する。したがって付録の見出しだけが英語のまま残り、付録本文は翻訳される。原文は上書きせず `translate_ja_v4` metadataへ英語、日本語、描画文字列、種別を保存する。通常見出しは英日併記、本文は日本語だけを描画する。

LLM backendはLangChainの `ChatPromptTemplate | with_structured_output` LCEL chainを使う。各要素では英語2列に一致した用語だけを添付し、`note` と `reference` は除外する。LibreTranslate backendは同じ基底classを実装し、JSON全体ではなく対象の原文文字列だけを配列送信する。送信前にURL、path、command option、inline code、機械的に識別できるidentifierをID付きの `<span translate="no">` へ置換し、`format=html` で翻訳する。応答内に各span IDが1個あることを確認して原文表記へ戻し、残りをplain textへ復元する。欠落・重複時は成果物を更新せず失敗にする。

batchは `batch_chars` と任意の `max_batch_elements` で作る。`0` は件数無制限であり、文字数制限は残る。LLM backendだけはpromptが `context_chars` に収まるよう追加調整し、原文の1.25倍とJSON field余白から見積もった応答が `max_output_tokens` を超える前にも分割する。失敗時は失敗batchだけを二分する。LibreTranslateのbatchは `context_chars` と `max_output_tokens` の影響を受けない。

## ReviewStage

対象ごとに原文、現在訳、一致用語、任意のQdrant根拠を作る。LangGraph subgraphがFidelity ReviewerとTerminology Reviewerを並列実行し、同じ案はそのまま採用する。不一致だけをAdjudicatorへ渡す。全agentは同じ外部Reviewルールに従う。

Reviewもbatch単位で実行し、現在訳の1.1倍とreason・JSON field余白から推定した応答が `max_output_tokens` を超えないよう分割する。失敗時は要素境界で二分する。結果は `translate_ja_v4.review_ja_v4` に理由を保存する。異常に長い出力は元の翻訳へ戻す。`--skip-review` ではtranslated JSONをreviewed JSONへコピーする。

## MarkdownStageとDocxStage

MarkdownStageはpage/order順に本文、見出し、code block、表、画像を描画する。表セルinline codeはbacktickで囲む。code本文内のbacktick列より長いfenceを選び、保存前にfenceの対応、table列数、制御文字、翻訳placeholderの残存、ローカル画像URIの安全性と存在を検証する。不正なMarkdownは保存せずStageを失敗させる。

DocxStageはpandocとreference docxでWordを生成した後、OOXMLを後処理する。内容が `---` だけの段落は文字列を消して段落下罫線へ変換する。図・表captionには `SEQ 図` / `SEQ 表` fieldを付け、文書先頭へ「目次」「図目次」「表目次」と対応するTOC fieldを挿入する。`updateFields=true` によりWordで開いたときにfieldを更新できる。最後に連続する見出し段落間だけ前後余白を0にする。`--skip-docx` ではMarkdownまで生成する。

## 障害時

ERROR直前のStage、manifestのstatus、completed値を確認する。認証や接続エラーは `.env`、Docling OOMは `--pdf-chunk-pages`、LLM容量エラーは `context_chars` とrules/glossary量、pandoc失敗はPATHとtemplateを確認する。一時的なDocling・LibreTranslate・LLM障害にはAPI retry、Stage例外にはLangGraph retryを別々に設定する。同じコマンドを再実行すれば、hash一致部分からResumeする。
