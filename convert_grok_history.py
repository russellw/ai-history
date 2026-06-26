#!/usr/bin/env python3
"""
Convert Grok chat history exports into a grep-friendly text format.

This mirrors convert_claude_history.py but for Grok's export format, which is
different in several ways:

  * A download is a single zip named ``<uuid>.zip`` (no manifest file).
  * Inside, the conversations live in
    ``ttl/30d/export_data/<user_id>/prod-grok-backend.json`` (alongside
    auth/billing JSON files that we ignore).
  * That file is a dict; ``conversations`` is a list of
    ``{"conversation": {...meta...}, "responses": [...]}`` objects.
  * Responses form a *tree* (like ChatGPT) linked by ``parent_response_id``;
    the active thread is reconstructed by walking back from the
    conversation's ``leaf_response_id``.
  * A response's text is in ``message``, its role in ``sender``
    (``human``/``assistant``), and its ``create_time`` is MongoDB extended
    JSON: ``{"$date": {"$numberLong": "<epoch-ms>"}}``.

Workflow (same safeguards as the Claude converter)
--------------------------------------------------
1. Drop the freshly downloaded ``*.zip`` into the ``Grok/`` directory.
2. Run this script.  It will:
     a. Determine the download's date (from the zip's internal file
        timestamps, else the zip mtime).
     b. Move the zip into a dated subdirectory ``Grok/<YYYY-MM-DD>/`` and
        extract it under ``extracted/<zip>/``.
     c. Merge the conversations of that download (keyed by conversation id).
     d. Compare against the most recent *previous* download and refuse to
        overwrite the output unless the new download is a superset (no
        conversations lost, none with fewer messages) — guarding against a
        truncated/incomplete download clobbering good data.
     e. Write ``conversations/Grok.txt``.

Re-running with no new zip present simply re-converts the latest download.

Usage:
    python3 convert_grok_history.py                 # full pipeline
    python3 convert_grok_history.py -o output.txt   # custom output file
    python3 convert_grok_history.py --stdout        # print to stdout
    python3 convert_grok_history.py --force         # ignore superset check
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
BACKEND_FILENAME = "prod-grok-backend.json"


# --------------------------------------------------------------------------
# Text / time formatting
# --------------------------------------------------------------------------
# Grok answers embed inline citation markers like
#   <grok:render card_id="5300f0" type="render_inline_citation">...</grok:render>
# which are noise in a grep-friendly transcript. Strip them out.
GROK_TAG_RE = re.compile(r"<grok:render\b[^>]*?/>|<grok:render\b.*?</grok:render>",
                         re.DOTALL)


def strip_grok_tags(text: str) -> str:
    """Remove inline <grok:render>...</grok:render> citation markers."""
    cleaned = GROK_TAG_RE.sub("", text)
    # Tidy up spaces/newlines left where a tag was removed.
    cleaned = re.sub(r"[ \t]+(?=\n)", "", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned


def fmt_time(value) -> str:
    """
    Format a Grok timestamp to a compact form: 2026-06-26 18:33.

    Accepts an ISO string (conversation meta), a MongoDB extended-JSON dict
    ``{"$date": {"$numberLong": "<ms>"}}`` (response create_time), or a raw
    epoch number (seconds or milliseconds).
    """
    if not value:
        return ""
    # MongoDB extended JSON: {"$date": {"$numberLong": "<ms>"}} or {"$date": <ms|iso>}
    if isinstance(value, dict):
        inner = value.get("$date", value)
        if isinstance(inner, dict):
            inner = inner.get("$numberLong")
        value = inner
    if isinstance(value, str) and value.lstrip("-").isdigit():
        value = int(value)
    if isinstance(value, (int, float)):
        # Heuristic: values past ~year 2001 in ms are >= 1e12
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


def epoch_ms(value) -> int:
    """Best-effort sort key (epoch ms) for a response create_time, else 0."""
    if isinstance(value, dict):
        inner = value.get("$date", value)
        if isinstance(inner, dict):
            inner = inner.get("$numberLong")
        value = inner
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    if isinstance(value, (int, float)):
        return int(value if value >= 1e12 else value * 1000)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except ValueError:
            return 0
    return 0


def reconstruct_thread(conv: dict) -> list:
    """
    Return the active list of responses for a conversation, in order.

    Walks back from ``leaf_response_id`` via ``parent_response_id`` (handling
    branches/regenerations the way the UI's active thread does).  Falls back
    to every response sorted by create_time if the leaf can't be followed.
    """
    responses = [r.get("response", {}) for r in conv.get("responses", [])]
    by_id = {r.get("_id"): r for r in responses if r.get("_id")}

    leaf = conv.get("conversation", {}).get("leaf_response_id")
    chain = []
    seen = set()
    node = by_id.get(leaf)
    while node is not None and node.get("_id") not in seen:
        seen.add(node.get("_id"))
        chain.append(node)
        node = by_id.get(node.get("parent_response_id"))
    if chain:
        chain.reverse()
        return chain

    return sorted(responses, key=lambda r: epoch_ms(r.get("create_time")))


def write_conversations(conversations: list, output_file):
    """Write a list of Grok conversation dicts to output_file in text format."""
    total = len(conversations)
    for i, conv in enumerate(conversations, 1):
        meta = conv.get("conversation", {})
        name = meta.get("title") or "(untitled)"
        created = fmt_time(meta.get("create_time", ""))
        updated = fmt_time(meta.get("modify_time", ""))
        conv_id = meta.get("id", "")

        output_file.write(f"=== [{created}] {name} ===\n")
        output_file.write(f"    id: {conv_id}  updated: {updated}\n\n")

        for resp in reconstruct_thread(conv):
            sender = (resp.get("sender") or "unknown").upper()
            ts = fmt_time(resp.get("create_time", ""))
            text = strip_grok_tags(resp.get("message") or "").strip()

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
    """Return the export date (YYYY-MM-DD) from the zip's newest internal file."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            dates = [info.date_time for info in zf.infolist() if info.date_time[0] >= 1980]
        if dates:
            y, m, d = max(dates)[:3]
            return f"{y:04d}-{m:02d}-{d:02d}"
    except (zipfile.BadZipFile, OSError):
        pass
    return None


def download_date(grok_dir: str, zips: list) -> str:
    """Best-effort date for the current download: zip internal dates, else mtime."""
    internal = [d for d in (zip_export_date(os.path.join(grok_dir, z)) for z in zips) if d]
    if internal:
        return max(internal)
    newest = max(os.path.getmtime(os.path.join(grok_dir, z)) for z in zips)
    return datetime.fromtimestamp(newest, tz=timezone.utc).strftime("%Y-%m-%d")


def unique_dir(base: str) -> str:
    """Return base, or base-2/base-3/... if it already exists."""
    if not os.path.exists(base):
        return base
    n = 2
    while os.path.exists(f"{base}-{n}"):
        n += 1
    return f"{base}-{n}"


def ingest_new_download(grok_dir: str):
    """
    If loose *.zip files are present in grok_dir, move them into a dated
    subdirectory and extract each into its own subtree.  Returns the path of
    the created dated directory, or None if nothing to do.
    """
    zips = sorted(f for f in os.listdir(grok_dir)
                  if f.endswith(".zip") and os.path.isfile(os.path.join(grok_dir, f)))
    if not zips:
        return None

    date = download_date(grok_dir, zips)
    dated_dir = unique_dir(os.path.join(grok_dir, date))
    extracted_dir = os.path.join(dated_dir, "extracted")
    os.makedirs(extracted_dir, exist_ok=True)
    print(f"Ingesting download into {dated_dir} ...", file=sys.stderr)

    for z in zips:
        src = os.path.join(grok_dir, z)
        dst = os.path.join(dated_dir, z)
        stem = z[:-4]  # strip .zip
        target = os.path.join(extracted_dir, stem)
        os.makedirs(target, exist_ok=True)
        with zipfile.ZipFile(src) as zf:
            zf.extractall(target)
        shutil.move(src, dst)
        print(f"  extracted {z}", file=sys.stderr)

    return dated_dir


def list_download_dirs(grok_dir: str) -> list:
    """Return dated download subdirectories, sorted oldest -> newest by name."""
    dirs = [
        os.path.join(grok_dir, d)
        for d in os.listdir(grok_dir)
        if DATE_DIR_RE.match(d) and os.path.isdir(os.path.join(grok_dir, d))
    ]
    return sorted(dirs, key=os.path.basename)


def load_download(dated_dir: str) -> dict:
    """
    Merge every prod-grok-backend.json under dated_dir into a dict keyed by
    conversation id.  On a collision keep the variant with more responses.
    """
    merged = {}
    extracted_dir = os.path.join(dated_dir, "extracted")
    if not os.path.isdir(extracted_dir):
        return merged

    for root, _dirs, files in os.walk(extracted_dir):
        if BACKEND_FILENAME not in files:
            continue
        with open(os.path.join(root, BACKEND_FILENAME), encoding="utf-8") as f:
            data = json.load(f)
        for conv in data.get("conversations", []):
            conv_id = conv.get("conversation", {}).get("id")
            if not conv_id:
                continue
            existing = merged.get(conv_id)
            if existing is None or \
                    len(conv.get("responses", [])) > len(existing.get("responses", [])):
                merged[conv_id] = conv
    return merged


def check_superset(current: dict, previous: dict) -> list:
    """
    Return a list of human-readable problems explaining why `current` is NOT a
    superset of `previous`.  Empty list means current is a safe superset.
    """
    def title(conv):
        return conv.get("conversation", {}).get("title") or "(untitled)"

    def nmsgs(conv):
        return len(conv.get("responses", []))

    problems = []
    missing = [u for u in previous if u not in current]
    if missing:
        problems.append(f"{len(missing)} conversation(s) present before are now missing")
        for u in missing[:10]:
            problems.append(f"    missing: {u}  {title(previous[u])}")

    shrunk = []
    for u, prev in previous.items():
        cur = current.get(u)
        if cur is not None and nmsgs(cur) < nmsgs(prev):
            shrunk.append((u, nmsgs(prev), nmsgs(cur)))
    if shrunk:
        problems.append(f"{len(shrunk)} conversation(s) have fewer messages than before")
        for u, before, after in shrunk[:10]:
            problems.append(f"    shrank: {u}  {before} -> {after} messages")

    return problems


def sort_conversations(conversations: list) -> list:
    """Stable sort by create_time so output ordering is deterministic."""
    return sorted(conversations,
                  key=lambda c: c.get("conversation", {}).get("create_time", "") or "")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Convert Grok history zip exports to grep-friendly text.")
    parser.add_argument("--grok-dir", default="Grok",
                        help="Directory holding downloads (default: Grok)")
    parser.add_argument("-o", "--output", default="conversations/Grok.txt",
                        help="Output text file (default: conversations/Grok.txt)")
    parser.add_argument("--stdout", action="store_true",
                        help="Write to stdout instead of a file")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite output even if the superset check fails")
    args = parser.parse_args()

    if not os.path.isdir(args.grok_dir):
        print(f"Error: {args.grok_dir} does not exist", file=sys.stderr)
        return 1

    # 1. Ingest any freshly downloaded zips into a dated subdirectory.
    ingest_new_download(args.grok_dir)

    # 2. Identify the latest download (and the one before it).
    downloads = list_download_dirs(args.grok_dir)
    if not downloads:
        print(f"Error: no download subdirectories found under {args.grok_dir}.\n"
              f"       Place the downloaded *.zip in {args.grok_dir} and re-run.",
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
