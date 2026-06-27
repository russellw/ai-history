#!/usr/bin/env python3
"""
Convert ChatGPT chat history exports into a grep-friendly text format.

This mirrors the Claude/Grok/Qwen converters: it works directly from the
ChatGPT export *zip* as downloaded, organizes downloads into dated
subdirectories, and refuses to overwrite the output unless the new download
is a superset of the previous one.

ChatGPT's format:
  * A download is a single zip whose name embeds the export date, e.g.
    ``<hash>-2026-06-26-00-59-25-<hash>.zip``.
  * Inside, the conversations are sharded across ``conversations-000.json`` …
    ``conversations-NNN.json`` (plus chat.html and other assets we ignore).
  * Each shard is a list of conversations.  A conversation stores its
    messages as a tree (``mapping`` dict) walked from ``current_node`` via
    parent links; timestamps are Unix epoch floats; ``system`` role messages
    are skipped.

Workflow
--------
1. Drop the freshly downloaded ``*.zip`` into the ``ChatGPT/`` directory.
2. Run this script.  It will:
     a. Determine the download's date (from the zip filename, else the zip's
        internal file timestamps, else mtime).
     b. Move the zip into a dated subdirectory ``ChatGPT/<YYYY-MM-DD>/`` and
        extract it under ``extracted/<zip>/``.
     c. Merge the conversations of that download (keyed by conversation id).
     d. Compare against the most recent *previous* download and refuse to
        overwrite the output unless the new download is a superset (no
        conversations lost, none with fewer messages).
     e. Write ``conversations/ChatGPT.txt``.

Re-running with no new zip present simply re-converts the latest download.

Usage:
    python3 convert_chatgpt_history.py                 # full pipeline
    python3 convert_chatgpt_history.py -o output.txt   # custom output file
    python3 convert_chatgpt_history.py --stdout        # print to stdout
    python3 convert_chatgpt_history.py --force          # ignore superset check
"""

import json
import os
import re
import sys
import glob
import shutil
import zipfile
import argparse
from datetime import datetime, timezone

DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(-\d+)?$")
# Date(-time) embedded in the export zip filename, e.g. ...-2026-06-26-00-59-25-...
FILENAME_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:-\d{2}-\d{2}-\d{2})?")


# --------------------------------------------------------------------------
# Conversation parsing (ChatGPT-specific)
# --------------------------------------------------------------------------
def fmt_time(ts) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OSError):
        return str(ts)


def extract_text(content: dict) -> str:
    if not content:
        return ""
    ctype = content.get("content_type", "text")
    parts = content.get("parts", [])

    if ctype == "text":
        texts = [p for p in parts if isinstance(p, str) and p.strip()]
        return "\n\n".join(texts)

    if ctype == "multimodal_text":
        result_parts = []
        for p in parts:
            if isinstance(p, str) and p.strip():
                result_parts.append(p.strip())
            elif isinstance(p, dict):
                dalle = p.get("metadata", {}).get("dalle")
                if dalle and dalle.get("prompt"):
                    result_parts.append(f"[IMAGE: {dalle['prompt']}]")
                else:
                    ptr = p.get("asset_pointer", "")
                    result_parts.append(f"[IMAGE: {ptr}]")
        return "\n\n".join(result_parts)

    if ctype == "code":
        lang = content.get("language", "")
        text = content.get("text", "").strip()
        label = f"[CODE: {lang}]" if lang and lang != "unknown" else "[CODE]"
        return f"{label}\n{text}" if text else label

    if ctype == "execution_output":
        text = content.get("text", "").strip()
        return f"[CODE_OUTPUT]\n{text}" if text else "[CODE_OUTPUT: (empty)]"

    if ctype == "tether_quote":
        domain = content.get("domain", "")
        text = content.get("text", "").strip()
        header = f"[WEB_QUOTE: {domain}]" if domain else "[WEB_QUOTE]"
        return f"{header}\n{text}" if text else header

    if ctype == "tether_browsing_display":
        result = content.get("result", "").strip()
        return f"[BROWSING_RESULT]\n{result}" if result else "[BROWSING_RESULT: (empty)]"

    if ctype == "system_error":
        name = content.get("name", "")
        text = content.get("text", "").strip()
        header = f"[SYSTEM_ERROR: {name}]" if name else "[SYSTEM_ERROR]"
        return f"{header}\n{text}" if text else header

    # Fallback: dump as JSON
    return f"[{ctype.upper()}]\n{json.dumps(content, indent=2)}"


def get_message_chain(mapping: dict, current_node: str) -> list:
    """Walk parent links from current_node to root, then reverse to chronological order."""
    chain = []
    node_id = current_node
    seen = set()
    while node_id and node_id not in seen:
        seen.add(node_id)
        node = mapping.get(node_id)
        if not node:
            break
        msg = node.get("message")
        if msg:
            chain.append(msg)
        node_id = node.get("parent")
    chain.reverse()
    return chain


def conv_id(conv: dict) -> str:
    return conv.get("id") or conv.get("conversation_id", "")


def conv_msg_count(conv: dict) -> int:
    """Number of message-bearing nodes — a completeness proxy for the superset check."""
    return sum(1 for node in conv.get("mapping", {}).values() if node.get("message"))


def write_conversations(conversations: list, output_file):
    """Write a list of ChatGPT conversation dicts to output_file in text format."""
    total = len(conversations)
    for i, conv in enumerate(conversations, 1):
        title = conv.get("title") or "(untitled)"
        created = fmt_time(conv.get("create_time"))
        updated = fmt_time(conv.get("update_time"))

        output_file.write(f"=== [{created}] {title} ===\n")
        output_file.write(f"    id: {conv_id(conv)}  updated: {updated}\n\n")

        messages = get_message_chain(conv.get("mapping", {}), conv.get("current_node", ""))
        for msg in messages:
            role = msg.get("author", {}).get("role", "unknown")
            if role == "system":
                continue
            ts = fmt_time(msg.get("create_time"))
            sender = role.upper()
            text = extract_text(msg.get("content", {}))

            output_file.write(f"{sender} [{ts}]:\n")
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
# Download ingestion / extraction
# --------------------------------------------------------------------------
def zip_export_date(zip_path: str):
    """Return the export date (YYYY-MM-DD): from the filename, else the zip's contents."""
    m = FILENAME_DATE_RE.search(os.path.basename(zip_path))
    if m:
        return m.group(1)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            dates = [info.date_time for info in zf.infolist() if info.date_time[0] >= 1980]
        if dates:
            y, mo, d = max(dates)[:3]
            return f"{y:04d}-{mo:02d}-{d:02d}"
    except (zipfile.BadZipFile, OSError):
        pass
    return None


def download_date(chatgpt_dir: str, zips: list) -> str:
    """Best-effort date for the current download: zip filename/contents, else mtime."""
    internal = [d for d in (zip_export_date(os.path.join(chatgpt_dir, z)) for z in zips) if d]
    if internal:
        return max(internal)
    newest = max(os.path.getmtime(os.path.join(chatgpt_dir, z)) for z in zips)
    return datetime.fromtimestamp(newest, tz=timezone.utc).strftime("%Y-%m-%d")


def unique_dir(base: str) -> str:
    """Return base, or base-2/base-3/... if it already exists."""
    if not os.path.exists(base):
        return base
    n = 2
    while os.path.exists(f"{base}-{n}"):
        n += 1
    return f"{base}-{n}"


def ingest_new_download(chatgpt_dir: str):
    """
    If loose *.zip files are present in chatgpt_dir, move them into a dated
    subdirectory and extract each into its own subtree.  Returns the path of
    the created dated directory, or None if nothing to do.
    """
    zips = sorted(f for f in os.listdir(chatgpt_dir)
                  if f.endswith(".zip") and os.path.isfile(os.path.join(chatgpt_dir, f)))
    if not zips:
        return None

    date = download_date(chatgpt_dir, zips)
    dated_dir = unique_dir(os.path.join(chatgpt_dir, date))
    extracted_dir = os.path.join(dated_dir, "extracted")
    os.makedirs(extracted_dir, exist_ok=True)
    print(f"Ingesting download into {dated_dir} ...", file=sys.stderr)

    for z in zips:
        src = os.path.join(chatgpt_dir, z)
        dst = os.path.join(dated_dir, z)
        stem = z[:-4]  # strip .zip
        target = os.path.join(extracted_dir, stem)
        os.makedirs(target, exist_ok=True)
        with zipfile.ZipFile(src) as zf:
            zf.extractall(target)
        shutil.move(src, dst)
        print(f"  extracted {z}", file=sys.stderr)

    return dated_dir


def list_download_dirs(chatgpt_dir: str) -> list:
    """Return dated download subdirectories, sorted oldest -> newest by name."""
    dirs = [
        os.path.join(chatgpt_dir, d)
        for d in os.listdir(chatgpt_dir)
        if DATE_DIR_RE.match(d) and os.path.isdir(os.path.join(chatgpt_dir, d))
    ]
    return sorted(dirs, key=os.path.basename)


def load_download(dated_dir: str) -> dict:
    """
    Merge every conversations-*.json shard under dated_dir into a dict keyed by
    conversation id.  On a collision keep the variant with more messages.
    """
    merged = {}
    pattern = os.path.join(dated_dir, "extracted", "**", "conversations-*.json")
    for path in sorted(glob.glob(pattern, recursive=True)):
        with open(path, encoding="utf-8") as f:
            conversations = json.load(f)
        for conv in conversations:
            cid = conv_id(conv)
            if not cid:
                continue
            existing = merged.get(cid)
            if existing is None or conv_msg_count(conv) > conv_msg_count(existing):
                merged[cid] = conv
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
    """Stable sort by create_time so output ordering is deterministic."""
    return sorted(conversations, key=lambda c: c.get("create_time") or 0)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Convert ChatGPT history zip exports to grep-friendly text.")
    parser.add_argument("--chatgpt-dir", default="ChatGPT",
                        help="Directory holding downloads (default: ChatGPT)")
    parser.add_argument("-o", "--output", default="conversations/ChatGPT.txt",
                        help="Output text file (default: conversations/ChatGPT.txt)")
    parser.add_argument("--stdout", action="store_true",
                        help="Write to stdout instead of a file")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite output even if the superset check fails")
    args = parser.parse_args()

    if not os.path.isdir(args.chatgpt_dir):
        print(f"Error: {args.chatgpt_dir} does not exist", file=sys.stderr)
        return 1

    # 1. Ingest any freshly downloaded zips into a dated subdirectory.
    ingest_new_download(args.chatgpt_dir)

    # 2. Identify the latest download (and the one before it).
    downloads = list_download_dirs(args.chatgpt_dir)
    if not downloads:
        print(f"Error: no download subdirectories found under {args.chatgpt_dir}.\n"
              f"       Place the downloaded *.zip in {args.chatgpt_dir} and re-run.",
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
