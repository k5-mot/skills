# Structure Rules

- 原文の追加、削除、書換えを行わない。
- 明らかな見出し階層、コード、引用、Alertの誤判定だけを補正する。
- `kind`が`heading`でない要素の`level`と、`alert`でない要素の`alert_kind`は`null`にする。
- 判断できない要素は変更しない。
