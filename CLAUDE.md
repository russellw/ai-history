Because of the Windows/Python code page nonsense, all Python code that opens any data file for reading or writing, must always explicitly specify encoding as UTF-8
Every program that opens any text file for output, must specify UNIX line endings

# Project overview

This repo converts AI chat history exports into a grep-friendly plain text format, one file per provider.

# Directory structure

- `convert_claude_history.py` — converts Claude export (`Claude/data-*.zip`) → `conversations/Claude.txt`
- `convert_chatgpt_history.py` — converts ChatGPT export (`ChatGPT/*.zip`) → `conversations/ChatGPT.txt`
- `convert_grok_history.py` — converts Grok export (`Grok/<uuid>.zip`) → `conversations/Grok.txt`
- `convert_qwen_history.py` — converts Qwen export (`Qwen/chat-export-<epoch-ms>.json`) → `conversations/Qwen.txt`
- `Claude/` — Claude export data (gitignored)
- `ChatGPT/` — ChatGPT export data (gitignored); a single export zip containing sharded `conversations-*.json` plus `chat.html` and supporting assets
- `Grok/` — Grok export data (gitignored)
- `Qwen/` — Qwen export data (gitignored)
- `conversations/` — output directory (gitignored)

# Scripts live in the root to avoid being gitignored along with their data directories

# Ingest + superset safeguard (all four converters)

All four converters share a pipeline. You drop the freshly downloaded file(s) into the provider directory and run the script, which:

1. Determines the download's date (Claude: from `manifest-*.json`; ChatGPT: from the zip filename's embedded date; Grok: from the zip's internal file timestamps; Qwen: from the `chat-export-<epoch-ms>.json` filename — each falling back to file mtime).
2. Moves the download into a dated subdirectory `<Provider>/<YYYY-MM-DD>/` (zips are also extracted under `extracted/<batch>/`; `unique_dir` appends `-2`, `-3`, … on a same-day collision).
3. Merges that download's conversations into a dict keyed by conversation id/uuid.
4. Before overwriting the output, checks that the latest download is a **superset** of the most recent previous dated download — no conversations dropped and none with fewer messages — and refuses unless `--force` is given (exit code 2). This guards against an incomplete download clobbering good data.

Re-running with no new download present just re-converts the latest dated subdirectory.

# Output format (all converters)

Each conversation starts with a header line:
    === [YYYY-MM-DD HH:MM] Title ===
        uuid/id: ...  updated: ...

Then each message:
    ROLE [YYYY-MM-DD HH:MM]:
      indented body text

# Provider-specific notes

Claude: a single export is downloaded as multiple `data-*.zip` batch files (plus a `manifest-*.json`) dropped into `Claude/`. Each batch zip contains its own `conversations.json` (same filename across batches, so they can't share a directory). `convert_claude_history.py` moves each download into a dated subdirectory `Claude/<YYYY-MM-DD>/` (date from the manifest), extracts each batch under `extracted/<batch>/`, and merges the per-batch conversation lists by `uuid`. Before overwriting the output it checks that the latest download is a superset of the most recent previous one (no conversations dropped, none with fewer messages) and refuses unless `--force` is given — a guard against an incomplete download clobbering good data. Within a conversation: messages are a flat list with a `sender` field; timestamps are ISO strings; content is a list of typed blocks (`text`, `tool_use`, `tool_result`).

ChatGPT: a single download zip whose name embeds the export date (e.g. `<hash>-2026-06-26-00-59-25-<hash>.zip`), no manifest with a usable date. Inside, conversations are sharded across numbered files (`conversations-000.json` … `conversations-NNN.json`) plus `chat.html` and other assets that are ignored; `convert_chatgpt_history.py` extracts under `extracted/<zip>/` and merges the shards by conversation `id`. Within a conversation, messages are stored as a tree (`mapping` dict), walked from `current_node` via parent links; timestamps are Unix epoch floats; `system` role messages are skipped; content types include `text`, `code`, `execution_output`, `tether_quote`, `tether_browsing_display`, `multimodal_text`, `system_error`.

Grok: a single download zip named `<uuid>.zip`, no manifest. Conversations live in `ttl/30d/export_data/<user_id>/prod-grok-backend.json` (the sibling auth/billing JSON files are ignored); `convert_grok_history.py` locates it by recursive glob. That file is a dict whose `conversations` is a list of `{conversation: {meta}, responses: [...]}`. Responses form a tree linked by `parent_response_id`; the active thread is reconstructed by walking back from the conversation's `leaf_response_id`. A response's text is `message`, its role is `sender` (`human`/`assistant`), and its `create_time` is MongoDB extended JSON (`{"$date": {"$numberLong": "<epoch-ms>"}}`) while the conversation meta's `create_time`/`modify_time` are ISO strings. Inline `<grok:render …>…</grok:render>` citation markers are stripped from message text.

Qwen: a single download JSON file `chat-export-<epoch-ms>.json` (no zip, no manifest); shape is `{success, request_id, data: [conversations]}`. Each conversation has `id`, `title`, `created_at`/`updated_at` (epoch seconds) and a `chat` object with `messages` (flat list) and `history` (`{messages: {id: msg}, currentId}`). Messages form a tree (`parentId`/`childrenIds`); the active thread is reconstructed by walking back from `currentId`. Role is `role` (`user`/`assistant`); message `timestamp` is epoch seconds. A user message's text is its top-level `content`, but an assistant's `content` is empty — its answer lives in `content_list` blocks with `phase == "answer"` (the `thinking_summary`/`web_search`/`web_extractor` blocks are skipped).
