# run_pipeline.py ワークフロー

## 起動と出力

`run_pipeline.py` はdotenvを読み、ログを初期化して `PipelineOptions` をLangGraphへ渡す。既定の出力構成は次のとおりである。

```text
outputs/<document_filename>/
├── <input-stem>.json
├── document.normalized.json
├── document.structured.json
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

Structureの処理済みIDは `completed_elements`、Translate/Reviewは各要素の `translate_ja_v3` / `review_ja_v3` metadataで判定する。各成功batchの直後に成果物とmanifestをatomic保存するため、中断後は未完了要素から続行する。CLI設定、rules、glossary、model、backendを変えると設定hashが変わり、該当Stage以降を再実行する。

## ParseStage

PDFを10ページずつ一時PDFへ分け、Docling Serveへ順番に送る。server側page image生成は無効にする。返却ZIPからJSONとartifactを取り出し、collection index、JSON pointer、page number、artifact URIを全体文書へ再採番して連結する。最後に元PDF全ページをpypdfium2でPNG化し、`pages[*].image.uri` を `artifacts/page_XXXXXX.png` にする。

Word入力は分割せずDocling Serveへ送る。`--force` がなければ完了成果物を再利用する。

## NormalizeStage

座標を持つtextだけをpage、縦位置、横位置で並べ替える。座標がない要素のslotは保ち、内容補正、label変更、cleanは行わない。並べ替え後は `self_ref` と `$ref` を更新する。

## StructureStage

ページごとにtextとtable cellのcompact JSONを作り、ローカルpage imageと一緒にLangChain structured modelへ渡す。見出しlevel、caption、code、隣接code結合、表セルinline codeを外部ルールに従って補正する。

コード結合では左要素へ原文を結合し、右要素を空にして `merged_into` を記録する。要素を配列から削除しないため、後続batchとResumeでrefが変化しない。promptが `context_chars` を超えないよう事前分割し、API失敗時は要素数を半減する。

## CleanStage

非コード・非見出し本文、および表セルの `...` 以上の連続dotと `・・・` 以上の中黒をそれぞれ3文字へ縮める。Structureが検出したinline code spanは保護する。文書順や構造は変更しない。

## TranslateStage

英字を含む本文、見出し、table caption、table cellを対象にする。code、page header/footerは除外する。原文は上書きせず `translate_ja_v3` metadataへ英語、日本語、描画文字列、種別を保存する。見出しは英日併記、本文は日本語だけを描画する。

LLM backendはLangChainの `ChatPromptTemplate | with_structured_output` LCEL chainを使う。各要素では英語2列に一致した用語だけを添付し、`note` と `reference` は除外する。LibreTranslate backendは同じ基底classを実装し、配列を一括送信する。

batchは `batch_chars` と任意の `max_batch_elements` で作る。`0` は件数無制限であり、文字数制限は残る。LLM backendだけはpromptが `context_chars` に収まるよう追加調整し、失敗時は失敗batchだけを二分する。LibreTranslateのbatchは `context_chars` の影響を受けない。

## ReviewStage

対象ごとに原文、現在訳、一致用語、任意のQdrant根拠を作る。LangGraph subgraphがFidelity ReviewerとTerminology Reviewerを並列実行し、同じ案はそのまま採用する。不一致だけをAdjudicatorへ渡す。全agentは同じ外部Reviewルールに従う。

Reviewもbatch単位で実行し、失敗時は要素境界で二分する。結果は `translate_ja_v3.review_ja_v3` に理由を保存する。異常に長い出力は元の翻訳へ戻す。`--skip-review` ではtranslated JSONをreviewed JSONへコピーする。

## MarkdownStageとDocxStage

MarkdownStageはpage/order順に本文、見出し、code block、表、画像を描画する。表セルinline codeはbacktickで囲む。DocxStageはpandocとreference docxでWordを生成し、連続する見出し段落間だけ前後余白を0にする。`--skip-docx` ではMarkdownまで生成する。

## 障害時

ERROR直前のStage、manifestのstatus、completed値を確認する。認証や接続エラーは `.env`、Docling OOMは10ページ分割が有効か、LLM容量エラーは `context_chars` とrules/glossary量、pandoc失敗はPATHとtemplateを確認する。同じコマンドを再実行すれば、hash一致部分からResumeする。
