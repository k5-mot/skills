# Open WebUI API Reference

このSkillで使うOpen WebUI APIの要点。詳細確認やエンドポイント追加時に読む。

Sources:

- Open WebUI API Endpoints: https://docs.openwebui.com/reference/api-endpoints/
- Open WebUI API Keys: https://docs.openwebui.com/features/authentication-access/api-keys/
- Open WebUI environment variables: https://docs.openwebui.com/reference/env-configuration/#api_keys_allowed_endpoints
- Agent Skills level 3 resources: https://platform.claude.com/docs/ja/agents-and-tools/agent-skills/overview#3

## Authentication

Open WebUI API keys are personal access tokens. A key inherits the role and group permissions of the user who created it, so automation should use a dedicated bot/service account where possible.

Default request headers:

```http
Authorization: Bearer <OPEN_WEBUI_API_KEY>
Accept: application/json
Content-Type: application/json
```

If a reverse proxy consumes the `Authorization` header before Open WebUI receives it, Open WebUI can read credentials from a custom header. The default custom header name is `x-api-key`; administrators can change it with `CUSTOM_API_KEY_HEADER`.

Never print, save, or post API keys. Treat `401 Unauthorized` as one of: missing/incorrect `Bearer` format, deleted key, insufficient user permission, or endpoint restriction mismatch.

## API Key Configuration

Relevant Open WebUI server settings:

| Setting                                 | Purpose                                                                                                                 |
| --------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `ENABLE_API_KEYS`                       | Enables API key creation. Replaces deprecated `ENABLE_API_KEY`.                                                         |
| `ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS` | Enables endpoint allowlisting for API-key access. Replaces deprecated `ENABLE_API_KEY_ENDPOINT_RESTRICTIONS`.           |
| `API_KEYS_ALLOWED_ENDPOINTS`            | Comma-separated allowlist used when endpoint restrictions are enabled. Replaces deprecated `API_KEY_ALLOWED_ENDPOINTS`. |
| `CUSTOM_API_KEY_HEADER`                 | Header name checked after `Authorization: Bearer` and `token` cookie; default is `x-api-key`.                           |

For this Skill, a restricted key normally needs the prefixes used by the commands you enable:

```text
/api/models,/api/v1/models,/api/v1/chats,/api/v1/chats/all/db,/api/chat/completions,/api/v1/knowledge,/api/v1/channels,/api/v1/files
```

If a deployment treats message posting or thread endpoints as distinct route prefixes, include the specific prefixes used by that instance, for example:

```text
/api/v1/channels,/api/v1/channels/{channel_id}/messages,/api/v1/files
```

## Base URL

`OPEN_WEBUI_BASE_URL` should be the origin of the Open WebUI deployment without a trailing slash, for example:

```text
http://open-webui:8080
```

The helper script strips a trailing slash before appending endpoint paths.

## Endpoints Used by `scripts/client.py`

These are the endpoints the bundled helper uses. Channel endpoints are Open WebUI application routes used by the WebUI and may vary across versions; if a deployment differs, prefer overriding the configurable paths rather than editing call sites.

| Operation                     | Method | Path                                                                                   | Notes                                                                                                                                                               |
| ----------------------------- | -----: | -------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| List models                   |  `GET` | `/api/models`                                                                          | Configurable with `OPEN_WEBUI_MODELS_PATH`.                                                                                                                         |
| Get model details             |  `GET` | `/api/v1/models/model?id={model_id}`                                                   | Returns one model's details when the instance supports this route.                                                                                                  |
| List chats                    |  `GET` | `/api/v1/chats?skip={skip}&limit={limit}`                                              | Returns chats visible to the API-key user. Admin/user permissions control scope.                                                                                    |
| List all DB chats             |  `GET` | `/api/v1/chats/all/db`                                                                 | Requires an admin API key and admin export access. Configurable with `OPEN_WEBUI_CHATS_ALL_DB_PATH`; used by activity reports before falling back to visible chats. |
| Get chat                      |  `GET` | `/api/v1/chats/{chat_id}`                                                              | Fetches one chat including its history payload.                                                                                                                     |
| Create chat                   | `POST` | `/api/v1/chats/new`                                                                    | Body is `{ "chat": {...} }`; caller must provide UI-compatible history/message structure for frontend rendering.                                                    |
| Update chat                   | `POST` | `/api/v1/chats/{chat_id}`                                                              | Merges a partial or full chat payload. Used before triggering completions for appended messages.                                                                    |
| Chat completion               | `POST` | `/api/chat/completions`                                                                | OpenAI-compatible completion endpoint; use with `chat_id` and assistant message `id` for UI-compatible updates.                                                     |
| List knowledge bases          |  `GET` | `/api/v1/knowledge`                                                                    | Returns knowledge bases visible to the API-key user.                                                                                                                |
| Get knowledge base            |  `GET` | `/api/v1/knowledge/{knowledge_id}`                                                     | Fetches one knowledge collection.                                                                                                                                   |
| List channels                 |  `GET` | `/api/v1/channels/`                                                                    | Configurable with `OPEN_WEBUI_CHANNELS_LIST_PATH`. Response may be a list, `{channels: [...]}`, or `{data: [...]}`.                                                 |
| Get channel                   |  `GET` | `/api/v1/channels/{channel_id}`                                                        | Used after resolving a channel name or ID.                                                                                                                          |
| List channel messages         |  `GET` | `/api/v1/channels/{channel_id}/messages?skip={skip}&limit={limit}`                     | Returns channel messages. The helper can optionally fetch threads per message.                                                                                      |
| Get message thread            |  `GET` | `/api/v1/channels/{channel_id}/messages/{message_id}/thread?skip={skip}&limit={limit}` | Used when `--include-threads` or `thread` is requested.                                                                                                             |
| Post message                  | `POST` | `/api/v1/channels/{channel_id}/messages/post`                                          | Configurable with `OPEN_WEBUI_CHANNELS_POST_PATH_TEMPLATE`; body includes `content`, `meta`, optional `data`, optional `parent_id`.                                 |
| Get file metadata             |  `GET` | `/api/v1/files/{file_id}`                                                              | Fetches metadata for a file ID found in message `data.files[*].id`.                                                                                                 |
| Get extracted file content    |  `GET` | `/api/v1/files/{file_id}/data/content`                                                 | Preferred route for extracted text content.                                                                                                                         |
| Get raw file content fallback |  `GET` | `/api/v1/files/{file_id}/content`                                                      | Used when extracted content is unavailable or `--raw` is requested.                                                                                                 |

The public Open WebUI API docs explicitly document file upload and processing routes such as `POST /api/v1/files/` and `GET /api/v1/files/{id}/process/status`. This Skill does not upload files; it reads files already attached to channel messages.

## CLI Command Groups

Top-level commands are grouped by resource:

- `models`: `list`, `get`
- `chats`: `list`, `get`, `create`, `update`, `post`, `completion`; `list --all-users` uses the admin all-DB endpoint.
- `knowledge`: `list`, `get`
- `channels`: `list`, `get`, `resolve`, `messages`, `thread`, `file-content`, `post`

## Request Bodies

### Post Channel Message

```json
{
  "content": "Markdown body",
  "meta": {
    "source": "hermes-agent",
    "skill": "open-webui-skill"
  },
  "data": {},
  "parent_id": "optional-thread-message-id"
}
```

`data` and `parent_id` are optional. `content` must be non-empty and must not exceed `OPEN_WEBUI_MAX_MESSAGE_CHARS`.

### Append to Existing Chat

Adding a user message to an existing UI chat is a two-step operation:

1. `POST /api/v1/chats/{chat_id}` with a user message and an empty assistant placeholder in `chat.history.messages`.
2. `POST /api/chat/completions` with `chat_id`, the assistant message `id`, message history, model, and session ID.

Open WebUI expects caller-generated message IDs, `childrenIds`, and `history.currentId`. Missing tree links can make messages exist in storage but not render in the WebUI.

## Response Handling

The helper treats any `2xx` status code as successful and tries to parse JSON first. Error responses include:

- `ok: false`
- `status_code`, when available
- redacted `response_text`
- parsed `response`, when it is JSON

The helper redacts the current `OPEN_WEBUI_API_KEY` from exception text and API response text before returning or printing results.

## Operational Notes

- Prefer `#channel-name` or a known channel ID. Plain names are normalized by stripping a leading `#`.
- If `--channel` is omitted in code paths that allow omission, use `OPEN_WEBUI_DEFAULT_CHANNEL`.
- Use `--dry-run` only for previews. A dry run resolves less and returns the request shape instead of calling the API.
- For attached file content, first inspect message `data.files[*].id`, then call `file-content`.
- With endpoint restrictions enabled, make the allowlist as narrow as possible but broad enough for the exact paths used by the helper.
