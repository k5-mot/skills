---
name: open-webui-skill
description: Operate Open WebUI models, chats, knowledge bases, channels, messages, threads, and files using OPEN_WEBUI_API_KEY. Use when listing models, reading or updating chat history, working with knowledge bases, fetching channel messages, or posting Markdown to Open WebUI.
---

# open-webui-skill

## Purpose

Use this skill to operate Open WebUI with the bot user configured by `OPEN_WEBUI_API_KEY`.

## Environment

Required environment variables:

- `OPEN_WEBUI_BASE_URL`: Open WebUI base URL, for example `http://open-webui:8080`.
- `OPEN_WEBUI_API_KEY`: bot user API key. Never include this value in prompts, output, logs, reports, or scheduled job definitions.

Optional environment variables:

- `OPEN_WEBUI_DEFAULT_CHANNEL`: fallback channel when a command omits `--channel`.
- `OPEN_WEBUI_PUBLIC_URL`: browser-facing Open WebUI URL used when reports need clickable links.
- `OPEN_WEBUI_CHANNELS_LIST_PATH`: channel listing endpoint path.
- `OPEN_WEBUI_CHANNELS_POST_PATH_TEMPLATE`: post endpoint path template containing `{channel_id}`.
- `OPEN_WEBUI_CHATS_ALL_DB_PATH`: admin endpoint for all-user chat export. Default is `/api/v1/chats/all/db`.
- `OPEN_WEBUI_MODELS_PATH`: model listing endpoint path.
- `OPEN_WEBUI_MAX_MESSAGE_CHARS`: maximum Markdown post size before truncation.
- `OPEN_WEBUI_TIMEOUT_SECONDS`: HTTP timeout in seconds.
- `OPEN_WEBUI_MAX_RETRIES`: retry count for transient API failures.

For API-key setup, endpoint allowlisting, exact routes, and request/response details, read `REFERENCE.md`.

Capabilities:

- List models visible to the bot user.
- Fetch, create, update, and append to chats visible to the bot user.
- Fetch all users' chats through the admin export endpoint when the API key has admin access.
- Fetch knowledge bases visible to the bot user.
- List channels visible to the bot user.
- Resolve `#channel-name` or a plain channel name to a channel ID.
- Fetch channel metadata.
- Fetch channel messages, regardless of whether they were written by AI or humans.
- Fetch message threads.
- Fetch file metadata and extracted file content.
- Post Markdown messages to a channel.

## Commands

Models:

```bash
python3 /opt/inferlab/skills/open-webui-skill/scripts/client.py models list
python3 /opt/inferlab/skills/open-webui-skill/scripts/client.py models get --model llama3.2
```

Chats:

```bash
python3 /opt/inferlab/skills/open-webui-skill/scripts/client.py chats list --all
python3 /opt/inferlab/skills/open-webui-skill/scripts/client.py chats list --all-users
python3 /opt/inferlab/skills/open-webui-skill/scripts/client.py chats get --chat-id "<chat_id>"
python3 /opt/inferlab/skills/open-webui-skill/scripts/client.py chats post \
  --chat-id "<chat_id>" \
  --model llama3.2 \
  --content "追加メッセージ"
```

Knowledge:

```bash
python3 /opt/inferlab/skills/open-webui-skill/scripts/client.py knowledge list --details
python3 /opt/inferlab/skills/open-webui-skill/scripts/client.py knowledge get --knowledge-id "<knowledge_id>"
```

Channels:

```bash
python3 /opt/inferlab/skills/open-webui-skill/scripts/client.py channels list
python3 /opt/inferlab/skills/open-webui-skill/scripts/client.py channels resolve --channel report
```

Fetch channel messages:

```bash
python3 /opt/inferlab/skills/open-webui-skill/scripts/client.py channels messages \
  --channel report \
  --all \
  --include-threads
```

Fetch file content:

```bash
python3 /opt/inferlab/skills/open-webui-skill/scripts/client.py channels file-content --file-id "<file_id>"
```

Post to Channels:

```bash
python3 /opt/inferlab/skills/open-webui-skill/scripts/client.py channels post \
  --channel report \
  --content "## Report\n\nBody"
```

## Rules

- Never include `OPEN_WEBUI_API_KEY` in prompts, output, or saved cron definitions.
- Read `REFERENCE.md` before changing API paths, adding new Open WebUI endpoints, or diagnosing API-key endpoint restrictions.
- Do not use `--dry-run` for scheduled posting unless the user explicitly asks for preview only.
- For live posting, inspect command JSON. `"ok": true` means the API accepted the post. `"ok": false` means no post happened.
- Prefer explicit channel names or IDs. `report` and `#report` are both accepted.
- If file content is needed, first inspect message `data.files[*].id`, then call `file-content`.

## Composition

- For activity reports: fetch messages with this skill, summarize with `llm-activity-report-skill`, then post with this skill.
- For technology news: collect sources with RSS/search, format with `tech-news-report-skill`, then post with this skill.
