---
name: md-dev
description: Markdown文書、README、開発ドキュメント、スキル文書を作成・更新・レビューするときの標準手順。Use when Codex writes or edits Markdown files, especially root README files, Quick Start sections, documentation structure, headings, links, tables, code fences, author/license sections, and consistent markdown style.
---

# 📝 Markdown Dev

Markdown 文書を書く・直す・レビューするときは、この Skill を使う。既存プロジェクトでは既存の読者、構成、用語、表記ゆれを尊重し、不足している標準だけを足す。

## 🎯 常時適用

- 見出しは `#` の直後に内容に合う絵文字を置く。
- 見出し階層を飛ばさない。`#` の次は `##`、その次は `###` にする。
- 見出しは短く、読者がスキャンしやすい名詞句にする。
- コマンド、ファイルパス、環境変数、パッケージ名、識別子はバッククォートで囲む。
- コードブロックには可能な限り言語名を付ける。コマンドは `bash`、環境変数例は `dotenv`、構造例は `text` を使う。
- 手順は再現可能な順番で書き、読者が最初に実行するコマンドを Quick Start に置く。
- リンクは裸 URL にせず、意味のあるリンクテキストにする。
- 表は比較に向く場合だけ使う。長い説明を無理に表に詰めない。
- 画像を入れる場合は alt text を付け、本文だけでも意味が通るようにする。
- README では事実と操作手順を優先し、マーケティング風の誇張を避ける。
- 既存文書の public utility、CLI、環境変数、出力ファイルの振る舞いを変えた場合は、該当 README または `docs/` を更新する。

## 📚 ルート README

ルート `README.md` には最低限、次の順で書く。

1. `#` 見出しのタイトル。
2. タイトル直下の簡単な説明。
3. `## 🚀 Quick Start`
4. `## 🧰 Tech Stack`
5. `## 👤 Author`
6. `## 📜 License`

ユーザーから License の指定がない場合は MIT と書く。

## 🚀 Quick Start

Quick Start には、初回利用者が最短で動作確認できるコマンドだけを置く。前提条件が必要な場合は、コマンド直前に短く書く。

````markdown
## 🚀 Quick Start

```bash
uv sync
uv run pytest
```
````

## 🧰 Tech Stack

技術スタックには、使用した主要なライブラリ、パッケージ、フレームワーク、外部サービスを列挙する。全 transitive dependency は書かない。

各技術名には、その技術の公式リファレンス、公式 docs、または GitHub リポジトリへのリンクを付ける。公式リンクが見つからない場合だけ、信頼できる一次情報に近いページを使う。

```markdown
## 🧰 Tech Stack

- [Python](https://docs.python.org/3/)
- [Ruff](https://docs.astral.sh/ruff/)
- [Vite](https://vite.dev/guide/)
```

## ✅ レビュー観点

- README の Quick Start は現在のコマンドで実行できるか。
- 環境変数名、ファイルパス、出力ファイル名が実装と一致しているか。
- 見出しだけ読んでも文書の流れが分かるか。
- 長い段落が続く場合、箇条書きか短い小見出しに分けられるか。
- Tech Stack の各技術名に公式リファレンス、公式 docs、または GitHub へのリンクが付いているか。
- ライセンス、Author、外部サービスの前提が抜けていないか。
