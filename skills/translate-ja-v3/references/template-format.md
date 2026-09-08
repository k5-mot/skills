# template.docx 書式仕様

この文書は `examples/template.docx` の正本仕様である。reference docxの作成・修正・検証では以下をすべて満たす。

## 基本書式

| 対象 | 英語 | 日本語 | サイズ・書式 |
| --- | --- | --- | --- |
| 本文 | Times New Roman | MS P明朝 | 10.5pt、黒 |
| 見出し・表紙・caption | Arial | MS Pゴシック | 用途別、黒 |
| 主題 | Arial | MS Pゴシック | 28pt |
| 副題 | Arial | MS Pゴシック | 14pt |
| 表・図caption | Arial | MS Pゴシック | 10pt |
| コードcaption | Arial | MS Pゴシック | 10.5pt、中央 |
| ヘッダー・フッター | Arial | MS Pゴシック | 9pt |
| コード・数式block | 専用style | 専用style | 10.5pt、黒枠 |

Note系を除き、文字、罫線、表、captionはすべて黒とする。

## ページ

- A4縦、210×297mm。
- 上下左右の余白はすべて12.7mm。テンプレート実体より本仕様を優先する。
- header/footerの紙端からの距離は約10mm。
- header/footerは紙端側を動かさず、本文側の空行でclearanceを取る。

## 見出しと目次

| level | サイズ | 太字 | 配置 | 本文開始位置 |
| --- | ---: | --- | --- | ---: |
| 見出し1 | 14pt | あり | 左 | 12.7mm |
| 見出し2 | 12pt | あり | 左 | 12.7mm |
| 見出し3 | 11pt | あり | 左 | 12.7mm |
| 見出し4〜6 | 10.5pt | なし | 左 | 約22.9mm |

イタリックは使わない。番号は `7`、`7.1`、…、`7.1.1.1.1.1` とし、余分なbulletを付けない。連続見出し間は前段落の後余白と次段落の前余白を0にし、最後の見出しと本文の間は通常余白を残す。

目次titleは「目次」、無番号とし、`TOC \\o "1-6" \\h \\z \\u` を基準に本冊の見出し1〜6だけを含める。TOC field後に古いcache textを重複させない。

## 本冊、付録、ページ番号

付録Aの「付録A」は18pt中央、付録見出し1は14pt左、付録見出し2は12pt左とする。付録styleはoutline level 9相当で目次に含めない。

本冊のページ番号は `<章番号>-<章内ページ番号>`、付録Aは `A-<ページ番号>` とする。章ごとに1へ戻し、総ページ数や `Page 1 of 20` は使わない。

## ヘッダーとフッター

どちらも左・中央・右の3領域を持つ。headerは「内容、黒罫線、本文側空行」、footerは「本文側空行、黒罫線、内容」の順とする。footerの日付は `yyyy/MM/dd最終更新`。表紙には通常本文用header/footerを出さない。

## list、図、表

bulletと番号付きlistはWordの正式な番号定義を使い、level間indentは約3.2mmとする。Microsoft Wordで記号が表示されることを確認する。

図は外部URLでなく文書へ埋め込み、labelは「図」とする。表labelは「表」とする。複数ページ表は先頭行を繰り返し、可能な限り行途中で分割せず、header/footer罫線と接触させない。外枠・内罫線は黒。表紙情報表、改訂履歴、通常表、複数ページ確認用の長い表を含める。

## コード、数式、inline code

コードlabelは「コード」、captionは中央とする。コードblockと数式blockは黒い四周罫線、10.5ptの正式なWord styleとしてstyle galleryへ登録する。本文や表セルの短いコードには段落styleでなくinline code文字styleを使い、両方のsampleを含める。

## Note系

NOTE、TIP、IMPORTANT、WARNING、CAUTIONの5種類を含め、文字色は黒、背景と左罫線だけ次の色を許可する。

| 種類 | 背景 | 左罫線 |
| --- | --- | --- |
| NOTE | `#DDF4FF` | `#0969DA` |
| TIP | `#DAFBE1` | `#1A7F37` |
| IMPORTANT | `#F3E8FF` | `#8250DF` |
| WARNING | `#FFF8C5` | `#9A6700` |
| CAUTION | `#FFEBE9` | `#CF222E` |

## 表紙と補助要素

表紙はシステム名、文書title、英語subtitle、区切り線、Document ID、Revision、Status、Date、Author、会社名を階層的に配置する。文書ID・文書種別・副題は表紙、header、文書管理表で一致させる。

Wordの正式な脚注sample、定義語・定義本文styleと定義list sampleを含める。内部bookmarkと内部linkは使用可能だが、外部file参照は禁止する。

## 外部参照禁止

OOXML package内の `TargetMode="External"` は0件とする。`INCLUDETEXT`、`INCLUDEPICTURE`、`LINK`、`DDE`、`DATABASE`、外部HYPERLINKを含めない。画像は埋め込み、`updateFields` と不要なTOC `dirty` flagを残さない。内部TOCやPAGE fieldは使用できる。

## 納品前確認

Microsoft Wordで外部参照警告が出ないこと、見出し1〜6・コード・数式style、番号位置、list記号、目次、付録除外、header/footer clearance、ページ番号、埋め込み画像、Note 5種、A4/全辺12.7mm、表紙整合、黒色、inline code、脚注、定義list、長い表を確認する。全pageをrenderし、崩れや重なりがないことも確認する。
