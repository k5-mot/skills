# translate-ja

PDF、Word、PowerPoint、HTML、画像などの入力文書を Docling schema JSON に正規化し、構造補正、清掃、チャンク化、日本語翻訳、Markdown 連結、Word docx 変換までを実行する。

## 使い方

```bash
./skills/translate-ja/run.sh --input ./docs/source/source.pdf
```

用語辞書を使う場合:

```bash
./skills/translate-ja/run.sh --input ./docs/source/source.pdf --dictionary-csv ./docs/source/dictionary.csv
```

辞書 CSV は UTF-8 で、列は `english`, `japanese`, `genre`, `description` とする。

## 注意

- Docling Serve と OpenAI 互換 API が必要。外部サービスなしの PDF フォールバックは行わない。
- `.env` または環境変数に `DOCLING_SERVER_URL`, `DOCLING_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL` を設定する。
- `LOG_LEVEL=DEBUG` では stream 差分ログに原文や翻訳文が含まれうるため、機密文書では使わない。
