# Oxlint推奨ルール

Oxlint設定を作成・変更するときはこのreferenceを読む。既存設定がある場合は上書きせず、既存ルールの意図を確認してから差分を反映する。

## 採用方針

- 初期標準は `correctness`、`suspicious`、`perf` を中心にする。
- `style`、`pedantic`、`restriction` はプロジェクトの合意がない限り全面有効化しない。
- 個別ルールで、デバッグ残り、過剰な `any`、JSDoc/TSDocの欠落を補う。
- React UIでは `jsx-a11y` と `react` の実害が出やすいルールを有効化する。
- Type-aware ruleは導入コストがあるため、CI時間と型設定が整ってから段階的に追加する。

## .oxlintrc.json例

```json
{
  "$schema": "./node_modules/oxlint/configuration_schema.json",
  "plugins": ["typescript", "react", "jsx-a11y", "jsdoc", "vitest", "import"],
  "categories": {
    "correctness": "error",
    "suspicious": "warn",
    "perf": "warn",
    "style": "off",
    "pedantic": "off",
    "restriction": "off"
  },
  "rules": {
    "eslint/no-console": "warn",
    "eslint/no-debugger": "error",
    "typescript/no-explicit-any": "warn",
    "typescript/consistent-type-imports": "warn",
    "jsdoc/require-param": "warn",
    "jsdoc/require-param-name": "warn",
    "jsdoc/require-param-description": "warn",
    "jsdoc/require-returns": "warn",
    "jsdoc/require-returns-description": "warn",
    "jsx-a11y/control-has-associated-label": "warn",
    "jsx-a11y/heading-has-content": "warn",
    "jsx-a11y/no-static-element-interactions": "warn"
  },
  "overrides": [
    {
      "files": ["**/*.test.ts", "**/*.test.tsx", "**/*.spec.ts", "**/*.spec.tsx"],
      "rules": {
        "typescript/no-explicit-any": "off"
      }
    }
  ]
}
```

## 既存設定への反映

1. 既存の `.oxlintrc.json`、`oxlint.config.ts`、`package.json` の `oxlint` 設定を確認する。
2. 未設定なら上記例を起点にする。
3. 既存設定がある場合は `correctness` を `error`、`suspicious` と `perf` を `warn` に寄せる。
4. `style`、`pedantic`、`restriction` を有効化する場合は、既存コードへの影響を見て段階的に行う。
5. JSDoc/TSDoc必須ルールを入れたら、既存の関数・コンポーネントに説明不足がないか確認する。
6. 追加後に `pnpm lint` と `pnpm format` を実行する。

## 参考

- Oxlint configuration: https://oxc.rs/docs/guide/usage/linter/config.html
- Oxlint config file reference: https://oxc.rs/docs/guide/usage/linter/config-file-reference.html
- Oxlint CLI categories: https://oxc.rs/docs/guide/usage/linter/cli.html
- Oxlint rules: https://oxc.rs/docs/guide/usage/linter/rules.html
