# MCP 接続

inferlab `41-llmwiki` の upstream `llmwiki serve` は stdio transport である。`docker-compose.yml` が host port `34100` へ公開するのは `llmwiki view` の read-only Viewer proxy であり、Streamable HTTP MCP ではない。

MCP client は Docker host 上で Runtime container 内の wrapper を stdio 起動する。

```json
{
  "mcpServers": {
    "llmwiki": {
      "command": "docker",
      "args": [
        "exec",
        "-i",
        "inferlab-llmwiki",
        "node",
        "/app/dist/serve.js"
      ]
    }
  }
}
```

実際の container 名は `${STACK_NAME}-llmwiki` である。上記は `STACK_NAME=inferlab` の例。

## 読み取り専用 tool allowlist

| Tool | 条件 |
|---|---|
| `wiki_status` | 常に許可 |
| `read_page` | 常に許可 |
| `get_context_pack` | `includeSources` は原文が必要な場合だけ有効化 |
| `search_pages` | LLM provider を呼ぶ点を了承して使用 |
| `query_wiki` | `save: false` を必須とする |

`lint_wiki` は lint cache を書くため、この Skill では許可しない。その他の tool は compilation、ingest、export、staging、評価履歴などの状態を変え得るため呼ばない。

`LLM_WIKI_COMPILER_SERVE_URL` だけで MCP を使うには、別途 Streamable HTTP gateway を公開する必要がある。現在の Viewer URL に `/mcp` を追加しても接続できない。
