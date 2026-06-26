#!/usr/bin/env python3
"""
Convert Qwen chat history exports into a grep-friendly text format.

This mirrors convert_claude_history.py / convert_grok_history.py but for
Qwen's export format, which differs again:

  * A download is a single JSON file named ``chat-export-<epoch-ms>.json``
    (no zip, no manifest).
  * The file is ``{"success": ..., "request_id": ..., "data": [conversations]}``.
  * Each conversation has ``id``, ``title``, ``created_at``/``updated_at``
    (epoch seconds) and a ``chat`` object holding ``messages`` (a flat list)
    and ``history`` (``{"messages": {id: msg}, "currentId": ...}``).
  * Messages form a *tree* (parentId/childrenIds); the active thread is
    reconstructed by walking back from ``currentId``.
  * A user message's text is its top-level ``content``.  An assistant
    message's ``content`` is empty — its answer lives in ``content_list``
    blocks whose ``phase`` is ``answer`` (the thinking/web_search blocks are
    skipped).

Workflow (same safeguards as the other converters)
--------------------------------------------------
1. Drop the freshly downloaded ``chat-export-*.json`` into ``Qwen/``.
2. Run this script.  It will:
     a. Determine the download's date (from the filename's epoch-ms, else
        the file mtime).
     b. Move the file(s) into a dated subdirectory ``Qwen/<YYYY-MM-DD>/``.
     c. Merge the conversations of that download (keyed by conversation id).
     d. Compare against the most recent *previous* download and refuse to
        overwrite the output unless the new download is a superset (no
        conversations lost, none with fewer messages).
     e. Write ``conversations/Qwen.txt``.

Re-running with no new export present simply re-converts the latest download.

Usage:
    python3 convert_qwen_history.py                 # full pipeline
    python3 convert_qwen_history.py -o output.txt   # custom output file
    python3 convert_qwen_history.py --stdout        # print to stdout
    python3 convert_qwen_history.py --force         # ignore superset check
"""

import json
import os
import re
import sys
import shutil
import argparse
from datetime import datetime, timezone

DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(-\d+)?$")
EXPORT_RE = re.compile(r"chat-export-(\d+)\.json$")


# --------------------------------------------------------------------------
# Text / time formatting
# --------------------------------------------------------------------------
def fmt_time(value) -> str:
    """
    Format a Qwen timestamp to a compact form: 2026-06-26 18:33.

    Accepts an epoch number (seconds or milliseconds) or an ISO string.
    """
    if not value:
        return ""
    if isinstance(value, str) and value.lstrip("-").isdigit():
        value = int(value)
    if isinstance(value, (int, float)):
        seconds = value / 1000.0 if value >= 1e12 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError, OverflowError):
            return str(value)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return value
    return str(value)


def message_text(msg: dict) -> str:
    """
    Extract the human-readable text of a message.

    User messages carry their text in the top-level ``content``.  Assistant
    messages have an empty ``content``; their answer lives in ``content_list``
    blocks with ``phase == 'answer'`` (thinking/web_search blocks are skipped).
    """
    top = (msg.get("content") or "").strip()
    if top:
        return top

    answer_parts = []
    for block in msg.get("content_list") or []:
        if block.get("phase") == "answer":
            c = (block.get("content") or "").strip()
            if c:
                answer_parts.append(c)
    if answer_parts:
        return "\n\n".join(answer_parts)

    # Last resort: any block content at all.
    for block in msg.get("content_list") or []:
        c = (block.get("content") or "").strip()
        if c:
            return c
    return ""


def active_thread(conv: dict) -> list:
    """
    Return the active list of messages for a conversation, in order.

    Walks back from ``currentId`` via ``parentId`` through ``chat.history``
    (handling branches/regenerations).  Falls back to the flat ``chat.messages``
    list if the history can't be followed.
    """
    chat = conv.get("chat", {})
    history = chat.get("history", {}) or {}
    by_id = history.get("messages", {}) or {}
    current = history.get("currentId") or conv.get("currentId")

    chain = []
    seen = set()
    node = by_id.get(current)
    while node is not None and node.get("id") not in seen:
        seen.add(node.get("id"))
        chain.append(node)
        node = by_id.get(node.get("parentId"))
    if chain:
        chain.reverse()
        return chain

    return chat.get("messages", []) or []


def write_conversations(conversations: list, output_file):
    """Write a list of Qwen conversation dicts to output_file in text format."""
    total = len(conversations)
    for i, conv in enumerate(conversations, 1):
        name = conv.get("title") or "(untitled)"
        created = fmt_time(conv.get("created_at", ""))
        updated = fmt_time(conv.get("updated_at", ""))
        conv_id = conv.get("id", "")

        output_file.write(f"=== [{created}] {name} ===\n")
        output_file.write(f"    id: {conv_id}  updated: {updated}\n\n")

        for msg in active_thread(conv):
            role = (msg.get("role") or "unknown").upper()
            ts = fmt_time(msg.get("timestamp", ""))
            text = message_text(msg)

            output_file.write(f"{role} [{ts}]:\n")
            if text:
                indented = "\n".join("  " + line for line in text.splitlines())
                output_file.write(indented + "\n")
            else:
                output_file.write("  (no text content)\n")
            output_file.write("\n")

        output_file.write("\n")

        if i % 100 == 0:
            print(f"  {i}/{total} conversations processed...", file=sys.stderr)

    print(f"Done. {total} conversations written.", file=sys.stderr)


# --------------------------------------------------------------------------
# Download ingestion
# --------------------------------------------------------------------------
def download_date(qwen_dir: str, exports: list) -> str:
    """Best-effort date for the current download: filename epoch-ms, else mtime."""
    stamps = []
    for name in exports:
        m = EXPORT_RE.search(name)
        if m:
            stamps.append(int(m.group(1)) / 1000.0)
        else:
            stamps.append(os.path.getmtime(os.path.join(qwen_dir, name)))
    newest = max(stamps)
    return datetime.fromtimestamp(newest, tz=timezone.utc).strftime("%Y-%m-%d")


def unique_dir(base: str) -> str:
    """Return base, or base-2/base-3/... if it already exists."""
    if not os.path.exists(base):
        return base
    n = 2
    while os.path.exists(f"{base}-{n}"):
        n += 1
    return f"{base}-{n}"


def ingest_new_download(qwen_dir: str):
    """
    If loose chat-export-*.json files are present in qwen_dir, move them into
    a dated subdirectory.  Returns the path of the created dated directory, or
    None if nothing to do.
    """
    exports = sorted(
        f for f in os.listdir(qwen_dir)
        if f.startswith("chat-export-") and f.endswith(".json")
        and os.path.isfile(os.path.join(qwen_dir, f))
    )
    if not exports:
        return None

    date = download_date(qwen_dir, exports)
    dated_dir = unique_dir(os.path.join(qwen_dir, date))
    os.makedirs(dated_dir, exist_ok=True)
    print(f"Ingesting download into {dated_dir} ...", file=sys.stderr)

    for f in exports:
        shutil.move(os.path.join(qwen_dir, f), os.path.join(dated_dir, f))
        print(f"  moved {f}", file=sys.stderr)

    return dated_dir


def list_download_dirs(qwen_dir: str) -> list:
    """Return dated download subdirectories, sorted oldest -> newest by name."""
    dirs = [
        os.path.join(qwen_dir, d)
        for d in os.listdir(qwen_dir)
        if DATE_DIR_RE.match(d) and os.path.isdir(os.path.join(qwen_dir, d))
    ]
    return sorted(dirs, key=os.path.basename)


def conv_msg_count(conv: dict) -> int:
    return len(conv.get("chat", {}).get("messages", []) or [])


def load_download(dated_dir: str) -> dict:
    """
    Merge every chat-export-*.json under dated_dir into a dict keyed by
    conversation id.  On a collision keep the variant with more messages.
    """
    merged = {}
    for root, _dirs, files in os.walk(dated_dir):
        for fn in files:
            if not (fn.startswith("chat-export-") and fn.endswith(".json")):
                continue
            with open(os.path.join(root, fn), encoding="utf-8") as f:
                payload = json.load(f)
            for conv in payload.get("data", []):
                conv_id = conv.get("id")
                if not conv_id:
                    continue
                existing = merged.get(conv_id)
                if existing is None or conv_msg_count(conv) > conv_msg_count(existing):
                    merged[conv_id] = conv
    return merged


def check_superset(current: dict, previous: dict) -> list:
    """
    Return a list of human-readable problems explaining why `current` is NOT a
    superset of `previous`.  Empty list means current is a safe superset.
    """
    def title(conv):
        return conv.get("title") or "(untitled)"

    problems = []
    missing = [u for u in previous if u not in current]
    if missing:
        problems.append(f"{len(missing)} conversation(s) present before are now missing")
        for u in missing[:10]:
            problems.append(f"    missing: {u}  {title(previous[u])}")

    shrunk = []
    for u, prev in previous.items():
        cur = current.get(u)
        if cur is not None and conv_msg_count(cur) < conv_msg_count(prev):
            shrunk.append((u, conv_msg_count(prev), conv_msg_count(cur)))
    if shrunk:
        problems.append(f"{len(shrunk)} conversation(s) have fewer messages than before")
        for u, before, after in shrunk[:10]:
            problems.append(f"    shrank: {u}  {before} -> {after} messages")

    return problems


def sort_conversations(conversations: list) -> list:
    """Stable sort by created_at so output ordering is deterministic."""
    return sorted(conversations, key=lambda c: c.get("created_at", 0) or 0)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Convert Qwen history JSON exports to grep-friendly text.")
    parser.add_argument("--qwen-dir", default="Qwen",
                        help="Directory holding downloads (default: Qwen)")
    parser.add_argument("-o", "--output", default="conversations/Qwen.txt",
                        help="Output text file (default: conversations/Qwen.txt)")
    parser.add_argument("--stdout", action="store_true",
                        help="Write to stdout instead of a file")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite output even if the superset check fails")
    args = parser.parse_args()

    if not os.path.isdir(args.qwen_dir):
        print(f"Error: {args.qwen_dir} does not exist", file=sys.stderr)
        return 1

    # 1. Ingest any freshly downloaded exports into a dated subdirectory.
    ingest_new_download(args.qwen_dir)

    # 2. Identify the latest download (and the one before it).
    downloads = list_download_dirs(args.qwen_dir)
    if not downloads:
        print(f"Error: no download subdirectories found under {args.qwen_dir}.\n"
              f"       Place the downloaded chat-export-*.json in {args.qwen_dir} and re-run.",
              file=sys.stderr)
        return 1

    latest_dir = downloads[-1]
    print(f"Latest download: {os.path.basename(latest_dir)}", file=sys.stderr)
    current = load_download(latest_dir)
    if not current:
        print(f"Error: no conversations found in {latest_dir}", file=sys.stderr)
        return 1

    # 3. Superset safeguard against the most recent previous download.
    if len(downloads) > 1:
        previous_dir = downloads[-2]
        previous = load_download(previous_dir)
        problems = check_superset(current, previous)
        if problems:
            print(f"\nSAFEGUARD: latest download ({os.path.basename(latest_dir)}) is NOT a "
                  f"superset of previous ({os.path.basename(previous_dir)}):", file=sys.stderr)
            for p in problems:
                print(f"  {p}", file=sys.stderr)
            if not args.force:
                print("\nRefusing to overwrite output. Re-run with --force to override.",
                      file=sys.stderr)
                return 2
            print("\n--force given: overwriting anyway.", file=sys.stderr)
        else:
            print(f"Superset check passed against {os.path.basename(previous_dir)} "
                  f"({len(previous)} -> {len(current)} conversations).", file=sys.stderr)

    # 4. Convert.
    conversations = sort_conversations(list(current.values()))
    if args.stdout:
        write_conversations(conversations, sys.stdout)
    else:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        print(f"Writing {len(conversations)} conversations -> {args.output} ...",
              file=sys.stderr)
        with open(args.output, "w", encoding="utf-8", newline="\n") as out:
            write_conversations(conversations, out)
        print(f"Output written to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
