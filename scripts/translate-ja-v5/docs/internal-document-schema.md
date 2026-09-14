# 内部文書スキーマ

## 目的

`translate-ja-v5`は、Docling Serveが返す`DoclingDocument` JSONを、翻訳工程専用の小さな内部文書モデルへ変換します。この文書は、その内部型、Docling要素との対応、翻訳layer、不変条件を説明します。

実装上の正本は次の2ファイルです。

- `src/model.py`: Pydanticによる内部型定義
- `src/processing/normalize.py`: Docling JSONから内部型への変換規則

内部JSONはResume用artifactであり、外部公開APIではありません。v4以前の中間JSONとの互換性もありません。

## 保存場所と単位

| Artifact | 内容 | Root型 |
|---|---|---|
| `.work/parsed.json` | Docling Serveの返却値を結合したJSON | Docling固有形式 |
| `.work/normalized.json` | 文書全体の正規化結果 | `Document` |
| `.work/structured/page_NNNNNN.json` | Structure補正後の1ページ | `Page` |
| `.work/translated/page_NNNNNN.json` | 翻訳layerを追加した1ページ | `Page` |
| `.work/reviewed/page_NNNNNN.json` | Review layerを追加した1ページ | `Page` |

`.work/state.json`はResume状態であり、本文の内部文書スキーマには含まれません。

## 型の階層

```text
Document
└── pages: Page[]
    └── blocks: Block[]
        ├── source / translated / reviewed: Inline[]
        ├── caption / translated_caption / reviewed_caption: Inline[]
        └── cells: TableCell[]
            └── source / translated / reviewed: Inline[]
```

JSONへ保存するとき、Pythonのtupleである`bbox`は4要素のJSON arrayになります。

## Document

文書全体をページ順に保持します。

| Field | JSON型 | 必須 | 既定値 | 意味 |
|---|---|---:|---|---|
| `pages` | `Page[]` | いいえ | `[]` | ページ番号順のページ |

Normalize後の`pages`は`Page.number`の昇順です。PDF第1ページもモデルには存在しますが、通常の翻訳workflowでは表紙として扱い、翻訳対象は第2ページ以降です。

## Page

PDFの1ページと、そのページに属するBlockを保持します。

| Field | JSON型 | 必須 | 既定値 | 意味 |
|---|---|---:|---|---|
| `number` | `integer` | はい | なし | 1始まりのPDFページ番号 |
| `width` | `number \| null` | いいえ | `null` | Docling page sizeの幅 |
| `height` | `number \| null` | いいえ | `null` | Docling page sizeの高さ |
| `blocks` | `Block[]` | いいえ | `[]` | ページ内の文書要素 |

`blocks`は`Block.order`の昇順で処理・描画されます。

## Block

見出し、段落、list、表、図など、ページ内の意味単位です。

| Field | JSON型 | 必須 | 既定値 | 意味 |
|---|---|---:|---|---|
| `id` | `string` | はい | なし | Docling refを基にした安定ID |
| `order` | `integer` | はい | なし | ページ内の0始まり文書順 |
| `kind` | `BlockKind` | はい | なし | Block種別 |
| `bbox` | `[number, number, number, number] \| null` | いいえ | `null` | 最初のprovenanceにある`left, top, right, bottom` |
| `source` | `Inline[]` | いいえ | `[]` | 原文layer |
| `translated` | `Inline[] \| null` | いいえ | `null` | 翻訳済みlayer |
| `reviewed` | `Inline[] \| null` | いいえ | `null` | Review済みlayer |
| `level` | `integer \| null` | いいえ | `null` | 見出しまたはlistの階層 |
| `ordered` | `boolean` | いいえ | `false` | 順序付きlistか |
| `checked` | `boolean \| null` | いいえ | `null` | task listの選択状態。通常listは`null` |
| `language` | `string \| null` | いいえ | `null` | code blockの言語 |
| `alert_kind` | `AlertKind \| null` | いいえ | `null` | Alert種別 |
| `asset_path` | `string \| null` | いいえ | `null` | `.work/`を基準にしたfigure asset path |
| `alt_text` | `string \| null` | いいえ | `null` | figureの代替text |
| `caption` | `Inline[]` | いいえ | `[]` | 図表題の原文layer |
| `translated_caption` | `Inline[] \| null` | いいえ | `null` | 図表題の翻訳済みlayer |
| `reviewed_caption` | `Inline[] \| null` | いいえ | `null` | 図表題のReview済みlayer |
| `cells` | `TableCell[]` | いいえ | `[]` | table cell |

### BlockKind

```text
paragraph
heading
list_item
blockquote
alert
code
formula
table
figure
footnote
horizontal_rule
```

種別ごとに主に使用するfieldは次のとおりです。

| `kind` | 主なfield | 補足 |
|---|---|---|
| `paragraph` | text layer | 通常本文 |
| `heading` | text layer、`level` | Markdown出力時はlevel 1–6へ制限 |
| `list_item` | text layer、`level`、`ordered`、`checked` | `level`は1始まり |
| `blockquote` | text layer | Structure工程で段落から補正可能 |
| `alert` | text layer、`alert_kind` | Structure工程で段落から補正可能 |
| `code` | text layer、`language` | Inlineは通常`kind=code` |
| `formula` | text layer | Doclingの`text`または`latex`を保持 |
| `table` | `cells`、caption layer | 本文layerは通常空 |
| `figure` | `asset_path`、`alt_text`、caption layer | assetがなければNormalizeを失敗させる |
| `footnote` | text layer | Markdown footnoteへ変換 |
| `horizontal_rule` | なし | モデルとRendererが対応する予約種別。現行Normalize/Structureは生成しない |

`Block`はdiscriminated unionではないため、Pydantic型だけでは「figureなら`asset_path`必須」のような種別固有条件をすべて強制しません。Normalizeと最終Rendererの検査がこれらを補います。

### AlertKind

```text
note
tip
important
warning
caution
```

## Inline

Block本文、caption、table cell内の文字列と装飾を表します。

| Field | JSON型 | 必須 | 既定値 | 意味 |
|---|---|---:|---|---|
| `id` | `string` | はい | なし | 親refから派生した安定ID |
| `text` | `string` | いいえ | `""` | 可視文字列 |
| `kind` | `InlineKind` | いいえ | `text` | Inline種別 |
| `marks` | `InlineMark[]` | いいえ | `[]` | 文字装飾 |
| `href` | `string \| null` | いいえ | `null` | link先。`kind=link`で使用 |

### InlineKind

```text
text
code
link
line_break
```

`line_break`は`text`ではなく改行として描画されます。`link`は`href`を使い、`code`はMarkdown inline codeとして描画されます。`line_break`はモデルとRendererが対応する予約種別で、現行Normalizeは生成しません。

### InlineMark

```text
strong
emphasis
strikethrough
underline
subscript
superscript
```

NormalizeはDoclingの`formatting.bold`、`italic`、`strikethrough`、`underline`、`script=sub|super`をこの値へ変換します。

## TableCell

1つの表cellと、その翻訳状態を保持します。

| Field | JSON型 | 必須 | 既定値 | 意味 |
|---|---|---:|---|---|
| `row` | `integer` | はい | なし | 0始まりrow位置 |
| `column` | `integer` | はい | なし | 0始まりcolumn位置 |
| `rowspan` | `integer` | いいえ | `1` | 占有row数 |
| `colspan` | `integer` | いいえ | `1` | 占有column数 |
| `header` | `boolean` | いいえ | `false` | header cellか |
| `source` | `Inline[]` | いいえ | `[]` | 原文layer |
| `translated` | `Inline[] \| null` | いいえ | `null` | 翻訳済みlayer |
| `reviewed` | `Inline[] \| null` | いいえ | `null` | Review済みlayer |

Normalizeは`rowspan`と`colspan`を1以上にします。最終検査では同じ`row`と`column`の重複も拒否します。

## 原文・翻訳・Review layer

本文、caption、table cellは、同じ構造のまま3段階のInline列を持ちます。

```text
source
  └─ Translate → translated
                    └─ Review → reviewed
```

- `source` / `caption`: Doclingから得た原文
- `translated` / `translated_caption`: 翻訳結果
- `reviewed` / `reviewed_caption`: Review後の最終候補

最終出力は`reviewed`、`translated`、`source`の順で、最初に`null`ではないlayerを使用します。空配列は「存在するlayer」として扱われるため、未処理は`[]`ではなく`null`で表します。

翻訳とReviewはInline IDを対応keyとして文字列だけを置き換えます。`marks`、`href`、Block順、table位置などの構造は前layerから維持します。

## ID規則

Doclingの`self_ref`は可能な限りBlock IDとしてそのまま維持します。

| 対象 | ID例 |
|---|---|
| text Block | `#/texts/12` |
| text Inline | `#/texts/12/inline/0` |
| figure caption Inline | `#/pictures/3/caption/0/inline/0` |
| grid形式のtable cell Inline | `#/tables/1/cell/2/3/inline/0` |
| flat形式のtable cell Inline | `#/tables/1/cell/4/inline/0` |

現在のNormalizeはDocling text itemを1つのInlineへ変換するため、通常の本文Inline suffixは`inline/0`です。翻訳応答は入力されたInline ID集合と完全一致する必要があり、未知ID、欠落ID、空訳を拒否します。

## Docling labelとの対応

Normalize直後の主な対応です。Structure工程は本文を変更せず、必要なBlockだけ`heading`、`paragraph`、`blockquote`、`alert`、`code`へ補正できます。

| Docling `label` | 内部`Block.kind` | 補足 |
|---|---|---|
| `title` | `heading` | levelがなければ1 |
| `section_header` | `heading` | levelがなければ1 |
| `heading` | `heading` | levelがなければ1 |
| `header` | `heading` | levelがなければ1 |
| `paragraph` | `paragraph` |  |
| `text` | `paragraph` |  |
| `list_item` | `list_item` | 親listからlevelとorderedを取得 |
| `checkbox_selected` | `list_item` | `checked=true` |
| `checkbox_unselected` | `list_item` | `checked=false` |
| `caption` | `paragraph` | 図表所有captionは図表へ内包し、重複Block化しない |
| `footnote` | `footnote` |  |
| `code` | `code` |  |
| `program_listing` | `code` |  |
| `formula` | `formula` |  |
| `table` | `table` | `data.grid`またはcell配列を変換 |
| `picture` | `figure` | asset URIを`structured/assets/`配下へ変換 |

`group`、`list`、`ordered_list`はBlockを作らず、子refの順序やlist階層を決めるcontainerとして展開します。

## 除外するDocling要素

次の要素は意図的に本文から除外します。

- `page_header`
- `page_footer`
- `document_index`
- 既知の目次見出しを持つページ
- tableまたはpictureが所有するcaptionの重複Block
- picture配下にあり、captionではない重複text
- 可視内容を持たない未知要素やcontainer

Pandocが目次、図一覧、表一覧を再生成するため、元PDFの目次ページは残しません。

## Normalize時に失敗する条件

次の場合は内容を推測したり黙って捨てたりせず、Normalizeを失敗させます。

- `schema_name`が`DoclingDocument`ではない
- `pages`が空、またはobjectではない
- text、table、picture、key-value、formのcollectionがarrayではない
- `$ref`を解決できない
- 要素が存在しないページを参照する
- 内容を持つ未知labelがある
- picture asset URIがない
- tableが対応するcell形式を持たない

最終Markdown生成前には、制御文字、figure assetの存在、table cell位置の重複、rowspan/colspan、文書内linkも追加検査します。

## JSON例

次は見出しと表を持つ最小例です。

```json
{
  "pages": [
    {
      "number": 2,
      "width": 612.0,
      "height": 792.0,
      "blocks": [
        {
          "id": "#/texts/0",
          "order": 0,
          "kind": "heading",
          "bbox": [72.0, 720.0, 300.0, 690.0],
          "source": [
            {
              "id": "#/texts/0/inline/0",
              "text": "Overview",
              "kind": "text",
              "marks": [],
              "href": null
            }
          ],
          "translated": [
            {
              "id": "#/texts/0/inline/0",
              "text": "概要",
              "kind": "text",
              "marks": [],
              "href": null
            }
          ],
          "reviewed": null,
          "level": 1,
          "ordered": false,
          "checked": null,
          "language": null,
          "alert_kind": null,
          "asset_path": null,
          "alt_text": null,
          "caption": [],
          "translated_caption": null,
          "reviewed_caption": null,
          "cells": []
        },
        {
          "id": "#/tables/0",
          "order": 1,
          "kind": "table",
          "bbox": null,
          "source": [],
          "translated": null,
          "reviewed": null,
          "level": null,
          "ordered": false,
          "checked": null,
          "language": null,
          "alert_kind": null,
          "asset_path": null,
          "alt_text": null,
          "caption": [],
          "translated_caption": null,
          "reviewed_caption": null,
          "cells": [
            {
              "row": 0,
              "column": 0,
              "rowspan": 1,
              "colspan": 1,
              "header": true,
              "source": [
                {
                  "id": "#/tables/0/cell/0/inline/0",
                  "text": "Name",
                  "kind": "text",
                  "marks": [],
                  "href": null
                }
              ],
              "translated": null,
              "reviewed": null
            }
          ]
        }
      ]
    }
  ]
}
```

## 機械可読JSON Schemaの確認

Pydanticから、その時点の実装に対応するJSON Schemaを生成できます。

```bash
PYTHONPATH=scripts/translate-ja-v5 uv run python - <<'PY'
import json

from src.model import Document

print(json.dumps(Document.model_json_schema(), ensure_ascii=False, indent=2))
PY
```

内部型を変更した場合は、`src/model.py`、Normalize、Renderer、Resume用artifact、本文書を同時に見直してください。
