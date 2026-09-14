# translate-ja v3/v4機能差分監査

## 結論

v5は、PDFから検証済みDOCXを生成する最小コアについてv3/v4の主要機能を維持している。ただし、v3/v4の全機能を包含してはいない。Word翻訳入力、詳細な運用option、独自OOXML後処理などは合意済みの非対象である。断片結合、Structureの一部patch、意味anchorがないPDF比較の再整列は未移行であり、将来要否を実文書で判断する。

監査対象はv3/v4の`README.md`、`references/workflow.md`、公開CLI、各Stage、Qdrant登録、standalone Reviewおよびテストである。v4はv3の主パイプラインを維持し、Parse検証、予算・retry、Langfuse、DOCX後処理、standalone Reviewを追加しているため、共通機能は主にv4との比較で判定した。

## 維持または置換した機能

| v3/v4の機能 | v5での扱い | 判定 |
|---|---|---|
| PDF解析から日本語DOCXまでの一括実行 | `translate`でParse、Normalize、Structure、Translate、Review、Markdown、DOCXを実行 | 維持 |
| LibreTranslateとLLM翻訳 | `--backend libretranslate\|openai` | 維持 |
| Structure、Translation、Reviewの外部Rules | `templates/`の用途別Rulesへ固定 | 置換 |
| 用語集を翻訳とReviewで共有 | `source,target,notes`の小さなCSV | 置換 |
| 複数観点ReviewとRAG | 直列LangGraphのChecker、Fidelity Critic、Japanese Critic、Reviser、Verifier | 強化 |
| 入力変更拒否、途中再開、強制再実行 | `state.json`、ページ単位Resume、`--force` | 置換 |
| Markdownの決定的生成と事前検証 | v5文書モデルからPandoc Markdownを生成してasset、表、link、制御文字を検証 | 維持 |
| 図表目次、目次、見出し番号 | Pandoc標準optionで生成 | 置換 |
| PDF比較Review | `review`と`.work-review/` | 維持 |
| Qdrant参照登録 | `register`、新revision検証後の旧revision自動削除 | 強化 |
| Langfuse | 関数階層と全LLM payloadをPython側で記録 | 強化 |

## 監査中に修復した欠損

| 欠損 | 修復内容 |
|---|---|
| DoclingのListGroup階層、Formatting、hyperlinkが消える | groupの入れ子からlevelとorderedを復元し、装飾とlinkをInlineへ保持 |
| 現行Doclingの`captions`参照を読まない | caption refを解決して図表へ内包し、本文への重複出力を防止 |
| 翻訳済み図題がPandoc Figureの外へ出る | 日本語図題をsemantic Figure captionとして描画 |
| 元PDFの目次と図内textが本文へ重複する | 目次ページとpicture配下の非caption textを除外 |
| Structureの見出しlevelが飛ぶ | 先頭をlevel 1、後続を直前から一段以内へ決定的に補正 |
| Structure promptに抽出座標がない | Blockのbboxをページ画像と共に送信 |
| OpenAI翻訳で保護断片をprompt指示だけに依存する | URL、path、option、code、dotted/snake/camel identifierをplaceholderで往復保護 |
| Doclingのpoll/result 5xxが共通retry外になる | `raise_for_status`をretry対象呼出しの内側へ移動 |
| 破損したResume artifactで処理が停止する | 破損した抽出、対応付け、ページJSONだけを再生成 |
| Qdrantが共通retryを通らない | search、upsert、retrieve、deleteを共通retryで実行 |
| Pandocが既存DOCXへ直接出力する | 同一directoryの一時DOCXを検証後にatomic置換 |
| 巨大PDFのDocling一括変換でServeが再起動する | PDFを10ページずつ変換し、参照、ページ番号、asset URIを再採番して連結 |

## 合意済みの非対象

| v3/v4の機能 | v5で採用しない理由 |
|---|---|
| Word翻訳入力 | 初版はPDF入力だけという確定仕様 |
| v4 CLI、中間JSON、manifest、8列用語集の互換性 | 互換性不要という確定仕様 |
| `--skip-vlm`、`--skip-review`、`--skip-docx` | 翻訳精度優先の一括処理契約と競合する |
| batch件数、timeout、retry回数、Stage retry等の多数のCLI option | 50,000 token予算と固定retry契約へ集約する |
| 任意templateとRules path | 同梱`templates/`を編集する契約へ集約する |
| 独自OOXMLによるSEQ/TOC field、水平線、見出し余白操作 | Pandoc標準機能と`template.docx`へ移し、OOXML後処理を廃止する確定仕様 |
| CleanStageの連続dot・中黒短縮 | 原文記号を翻訳前に変更するだけの独立Stageを置かない |
| Rules、用語集、artifact directory hashによる自動無効化 | `--force`へ集約する確定仕様 |
| 要素単位Resume | 完了済みページだけを再利用する確定仕様 |
| registerのDOTX、多数のtext suffix、dry-run、chunk調整option | 初版の登録契約をPDF、DOCX、Markdown、TXTと固定chunkへ限定する |

## 未移行の機能と影響

| 未移行機能 | 影響 | 現時点の扱い |
|---|---|---|
| 座標・PDF spanを使う本文断片結合とページ跨ぎtable結合 | 内容は失わないが、抽出品質によって段落や表が分断されたままになる | 未実装。Docling出力fixtureで再現してから限定的に追加する |
| Structureの隣接code結合とtable cell inline-code patch | 分断codeや表セル内codeの表示精度がv4を下回る場合がある | 未実装。v5の本文不変patch契約を変えるため別変更とする |
| Structure patch監査JSON | model判断の個別監査はLangfuse依存になる | 公開成果物をDOCXだけとする方針により非採用 |
| 意味anchorがない英日PDFのLLM再整列 | page順fallbackとなり、純粋な文章ページの並べ替えを検出できない場合がある | 未実装。比較Reviewの実データ評価後にsemantic alignmentを検討する |
| 図と親関係がないbbox重複textの除外 | Doclingがparentを設定しない場合、図内文字が本文にも残る可能性がある | v4の80%重複heuristicは誤削除リスクがあるため未移行 |

未移行項目は隠れた互換性として実装せず、実文書の失敗例を追加してから狭い修正として扱う。これにより、v4の過剰な設定面をv5へ戻さず、品質差を測定可能な形で管理する。
