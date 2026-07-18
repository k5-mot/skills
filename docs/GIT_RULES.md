# Git Rules

## Commit

- Commit messages MUST follow Conventional Commits combined with gitmoji.
  - Format: `<gitmoji> <type>(<scope>): <subject>`
  - Example: `✨ feat(auth): ログイン機能を追加`
- The commit subject and body MUST be written in Japanese.
- The commit body MUST include the intent of the change — explain why the change was made, not just what was changed.
  - Example:
    ```
    🐛 fix(api): レスポンスのタイムアウト値を修正

    外部APIの応答が遅い場合に504エラーが頻発していたため、
    タイムアウトを10秒から30秒に延長した。
    ```
- You SHOULD use the following common type / gitmoji pairs:
  - `✨ feat` — new feature
  - `🐛 fix` — bug fix
  - `♻️ refactor` — refactoring (no behavior change)
  - `📝 docs` — documentation
  - `✅ test` — tests
  - `🔧 chore` — config / tooling
  - `⚡️ perf` — performance improvement

## Branch

- You MUST adopt GitHub Flow.
  - `main` is always deployable. You MUST NOT commit directly to `main`.
  - You MUST create a branch per feature / change, open a Pull Request, and merge into `main` after review.
- Branch names SHOULD follow the format `<type>/<short-description>`.
  - Examples: `feat/user-login`, `fix/timeout-error`, `docs/update-readme`
- Branches SHOULD be small and short-lived; one branch = one purpose.

## References

- [gitmoji](https://gitmoji.dev/)
- [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/)
