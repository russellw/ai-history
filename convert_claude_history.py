#!/usr/bin/env python3
"""
Convert Claude chat history exports into a grep-friendly text format.

This script works directly from the Claude data export *zip files* as
downloaded.  A single export is now split across multiple zips (batches),
each of which contains its own ``conversations.json`` (and other files) with
the *same names* — so they cannot simply be unzipped into one directory.

Workflow
--------
1. Drop the freshly downloaded ``data-*.zip`` files (and the
   ``manifest-*.json``) into the ``Claude/`` directory.
2. Run this script.  It will:
     a. Determine the download's date (from the manifest, else file mtimes).
     b. Move the zips + manifest into a dated subdirectory
        ``Claude/<YYYY-MM-DD>/`` and extract each zip into its own
        ``extracted/<batch>/`` subtree (avoiding the name collisions).
     c. Merge the conversations from every batch of that download.
     d. Compare against the most recent *previous* download and refuse to
        overwrite the output unless the new download is a superset (no
        conversations lost, no conversation shorter than before).  This
        guards against a truncated/incomplete download clobbering good data.
     e. Write ``conversations/Claude.txt``.

Re-running with no new zips present simply re-converts the latest download.

Usage:
    python3 convert_claude_history.py                 # full pipeline
    python3 convert_claude_history.py -o output.txt   # custom output file
    python3 convert_claude_history.py --stdout        # print to stdout
    python3 convert_claude_history.py --force         # ignore superset check
"""

import json
import os
import re
import sys
import shutil
import zipfile
import argparse
from datetime import datetime, timezone

DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(-\d+)?$")


# --------------------------------------------------------------------------
# Text formatting (unchanged conversion logic)
# --------------------------------------------------------------------------
def fmt_time(ts: str) -> str:
    """Format ISO timestamp to a compact form: 2025-02-10 15:00"""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts


def extract_text(content_blocks: list, text_fallback: str) -> str:
    """
    Extract human-readable text from a list of content blocks.
    Falls back to the top-level 'text' field if no blocks yield text.
    """
    if not content_blocks:
        return text_fallback or ""

    parts = []
    for block in content_blocks:
        btype = block.get("type", "")
        if btype == "text":
            t = block.get("text", "").strip()
            if t:
                parts.append(t)
        elif btype == "tool_use":
            name = block.get("name", "unknown_tool")
            inp = block.get("input", {})
            parts.append(f"[TOOL_USE: {name}]")
            if inp:
                try:
                    parts.append(json.dumps(inp, indent=2))
                except (TypeError, ValueError):
                    parts.append(str(inp))
        elif btype == "tool_result":
            sub = block.get("content", [])
            result_parts = []
            if isinstance(sub, list):
                for sb in sub:
                    if sb.get("type") == "text":
                        t = sb.get("text", "").strip()
                        if t:
                            result_parts.append(t)
            elif isinstance(sub, str):
                result_parts.append(sub)
            if result_parts:
                parts.append(f"[TOOL_RESULT]\n" + "\n".join(result_parts))
            else:
                parts.append("[TOOL_RESULT: (empty)]")

    return "\n\n".join(parts) if parts else (text_fallback or "")


def write_conversations(conversations: list, output_file):
    """Write a list of conversation dicts to output_file in text format."""
    total = len(conversations)
    for i, conv in enumerate(conversations, 1):
        name = conv.get("name") or "(untitled)"
        created = fmt_time(conv.get("created_at", ""))
        updated = fmt_time(conv.get("updated_at", ""))
        uuid = conv.get("uuid", "")

        # Conversation header — single line, easy to grep as an anchor
        output_file.write(f"=== [{created}] {name} ===\n")
        output_file.write(f"    uuid: {uuid}  updated: {updated}\n\n")

        for msg in conv.get("chat_messages", []):
            sender = msg.get("sender", "unknown").upper()
            ts = fmt_time(msg.get("created_at", ""))
            text = extract_text(msg.get("content", []), msg.get("text", ""))

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
def manifest_date(claude_dir: str):
    """Return the download date (YYYY-MM-DD) from the newest manifest, or None."""
    manifests = sorted(
        f for f in os.listdir(claude_dir)
        if f.startswith("manifest-") and f.endswith(".json")
    )
    if not manifests:
        return None
    path = os.path.join(claude_dir, manifests[-1])
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        created = data.get("created_at", "")
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except (ValueError, OSError):
        return None


def download_date(claude_dir: str, zips: list) -> str:
    """Best-effort date for the current download: manifest, else newest zip mtime."""
    date = manifest_date(claude_dir)
    if date:
        return date
    newest = max(os.path.getmtime(os.path.join(claude_dir, z)) for z in zips)
    return datetime.fromtimestamp(newest, tz=timezone.utc).strftime("%Y-%m-%d")


def unique_dir(base: str) -> str:
    """Return base, or base-2/base-3/... if it already exists."""
    if not os.path.exists(base):
        return base
    n = 2
    while os.path.exists(f"{base}-{n}"):
        n += 1
    return f"{base}-{n}"


def ingest_new_download(claude_dir: str):
    """
    If loose data-*.zip files are present in claude_dir, move them (plus the
    manifest) into a dated subdirectory and extract each into its own subtree.
    Returns the path of the created dated directory, or None if nothing to do.
    """
    zips = sorted(f for f in os.listdir(claude_dir)
                  if f.startswith("data-") and f.endswith(".zip"))
    if not zips:
        return None

    date = download_date(claude_dir, zips)
    dated_dir = unique_dir(os.path.join(claude_dir, date))
    extracted_dir = os.path.join(dated_dir, "extracted")
    os.makedirs(extracted_dir, exist_ok=True)
    print(f"Ingesting download into {dated_dir} ...", file=sys.stderr)

    for z in zips:
        src = os.path.join(claude_dir, z)
        dst = os.path.join(dated_dir, z)
        # Extract into a per-batch subdir so identically named files don't clash
        stem = z[:-4]  # strip .zip
        target = os.path.join(extracted_dir, stem)
        os.makedirs(target, exist_ok=True)
        with zipfile.ZipFile(src) as zf:
            zf.extractall(target)
        shutil.move(src, dst)
        print(f"  extracted {z}", file=sys.stderr)

    # Archive the manifest(s) alongside the zips
    for f in os.listdir(claude_dir):
        if f.startswith("manifest-") and f.endswith(".json"):
            shutil.move(os.path.join(claude_dir, f), os.path.join(dated_dir, f))

    return dated_dir


def list_download_dirs(claude_dir: str) -> list:
    """Return dated download subdirectories, sorted oldest -> newest by name."""
    dirs = [
        os.path.join(claude_dir, d)
        for d in os.listdir(claude_dir)
        if DATE_DIR_RE.match(d) and os.path.isdir(os.path.join(claude_dir, d))
    ]
    return sorted(dirs, key=os.path.basename)


def load_download(dated_dir: str) -> dict:
    """
    Merge every batch's conversations.json under dated_dir into a dict
    keyed by conversation uuid.  On a uuid collision keep the variant with
    more messages.
    """
    merged = {}
    extracted_dir = os.path.join(dated_dir, "extracted")
    if not os.path.isdir(extracted_dir):
        return merged

    for root, _dirs, files in os.walk(extracted_dir):
        if "conversations.json" not in files:
            continue
        path = os.path.join(root, "conversations.json")
        with open(path, encoding="utf-8") as f:
            convs = json.load(f)
        for conv in convs:
            uuid = conv.get("uuid")
            if not uuid:
                continue
            existing = merged.get(uuid)
            if existing is None or \
                    len(conv.get("chat_messages", [])) > len(existing.get("chat_messages", [])):
                merged[uuid] = conv
    return merged


def check_superset(current: dict, previous: dict) -> list:
    """
    Return a list of human-readable problems explaining why `current` is NOT a
    superset of `previous`.  Empty list means current is a safe superset.
    """
    problems = []
    missing = [u for u in previous if u not in current]
    if missing:
        problems.append(f"{len(missing)} conversation(s) present before are now missing")
        for u in missing[:10]:
            name = previous[u].get("name") or "(untitled)"
            problems.append(f"    missing: {u}  {name}")

    shrunk = []
    for u, prev in previous.items():
        cur = current.get(u)
        if cur is None:
            continue
        if len(cur.get("chat_messages", [])) < len(prev.get("chat_messages", [])):
            shrunk.append((u, len(prev.get("chat_messages", [])),
                           len(cur.get("chat_messages", []))))
    if shrunk:
        problems.append(f"{len(shrunk)} conversation(s) have fewer messages than before")
        for u, before, after in shrunk[:10]:
            problems.append(f"    shrank: {u}  {before} -> {after} messages")

    return problems


def sort_conversations(conversations: list) -> list:
    """Stable sort by created_at so output ordering is deterministic."""
    return sorted(conversations, key=lambda c: c.get("created_at", "") or "")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Convert Claude history zip exports to grep-friendly text.")
    parser.add_argument("--claude-dir", default="Claude",
                        help="Directory holding downloads (default: Claude)")
    parser.add_argument("-o", "--output", default="conversations/Claude.txt",
                        help="Output text file (default: conversations/Claude.txt)")
    parser.add_argument("--stdout", action="store_true",
                        help="Write to stdout instead of a file")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite output even if the superset check fails")
    args = parser.parse_args()

    if not os.path.isdir(args.claude_dir):
        print(f"Error: {args.claude_dir} does not exist", file=sys.stderr)
        return 1

    # 1. Ingest any freshly downloaded zips into a dated subdirectory.
    ingest_new_download(args.claude_dir)

    # 2. Identify the latest download (and the one before it).
    downloads = list_download_dirs(args.claude_dir)
    if not downloads:
        print(f"Error: no download subdirectories found under {args.claude_dir}.\n"
              f"       Place the downloaded data-*.zip files in {args.claude_dir} and re-run.",
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
