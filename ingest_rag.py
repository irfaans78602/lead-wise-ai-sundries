"""
ingest_rag.py
Ingests text into the site_content table from ONE chosen source per run.

Usage:
    python ingest_rag.py json    # website English JSON files
    python ingest_rag.py html    # built HTML files in public/
    python ingest_rag.py extra   # extra_data/*.json

Features:
- Source is a mandatory argument.
- Append-only: this script NEVER deletes or updates existing rows.
- Deduplication against both this run AND what is already in the database:
  1. Skips exact string matches.
  2. Skips semantic duplicates (e.g., "We charge R500" vs "Our fee is R500").
- Encoding-tolerant JSON loader (UTF-8 BOM, UTF-8, cp1252, latin-1).
- Per-insert commit, so one bad chunk cannot nuke its whole batch.
- Pre-flight embedding check so a broken API key / bad model name aborts early.
"""
import os
import re
import sys
import json
import argparse
import html as html_lib
from pathlib import Path

from gemini import embed_document
from db import get_conn

# --- Config ---
# Adjust this path to point to your lead-wise-ai.com website repo
SITE_REPO = Path(r"C:\Irfaan\leadwiseai\lead-wise-ai.com")
LOCALES_DIR = SITE_REPO / "src" / "data" / "locales"
PUBLIC_DIR = SITE_REPO / "public"

# Path to the extra data folder in the sundries project
EXTRA_DATA_DIR = Path(__file__).parent / "extra_data"

# Only ingest English
LOCALES = ["en"]
META_KEYS = {"_comment", "_filename", "_blog"}

# Allowed values for the mandatory CLI argument
SOURCES = ("json", "html", "extra")

# Semantic deduplication threshold.
# 0.0 means identical. 1.0 means completely opposite/unrelated.
SEMANTIC_DISTANCE_THRESHOLD = 0.08


def to_vector_literal(values):
    """Format as [f1,f2,...] for pgvector."""
    return "[" + ",".join(f"{v:.8f}" for v in values) + "]"


def _load_json(file_path):
    """Load a JSON file, trying multiple encodings."""
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            with open(file_path, "r", encoding=enc) as f:
                return json.load(f)
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"Could not decode {file_path}")


def _html_to_text(html_content):
    """Strip HTML tags, scripts, styles, and boilerplate to get clean text."""
    # Remove script and style elements
    text = re.sub(r'<(script|style)\b[^>]*>[\s\S]*?</\1>', ' ', html_content, flags=re.IGNORECASE)
    # Remove nav, header, footer, svg
    text = re.sub(r'<(nav|header|footer|svg)\b[^>]*>[\s\S]*?</\1>', ' ', text, flags=re.IGNORECASE)

    # Try to isolate <main> if it exists
    main_match = re.search(r'<main\b[^>]*>([\s\S]*?)</main>', text, flags=re.IGNORECASE)
    if main_match:
        text = main_match.group(1)

    # Replace block tags with double newlines for paragraph breaks
    text = re.sub(r'<(p|h[1-6]|li|div|br|tr)\b[^>]*>', '\n\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</(p|h[1-6]|li|div|tr)>', '\n\n', text, flags=re.IGNORECASE)

    # Strip remaining tags
    text = re.sub(r'<[^>]+>', ' ', text)

    # Decode HTML entities (e.g. &amp; -> &)
    text = html_lib.unescape(text)

    # Collapse whitespace
    text = re.sub(r'[ \t]+', ' ', text)
    # Collapse multiple newlines into exactly two
    text = re.sub(r'\n\s*\n+', '\n\n', text)

    return text.strip()


def extract_json_chunks(file_path, locale, source_prefix, seen_texts):
    """Extract string values from a JSON file."""
    chunks = []
    try:
        data = _load_json(file_path)
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return chunks

    items = data.items() if isinstance(data, dict) else enumerate(data)

    for key, val in items:
        if isinstance(key, str) and key in META_KEYS:
            continue
        if not isinstance(val, str):
            continue

        clean_val = val.strip()
        if len(clean_val) < 20 or clean_val in seen_texts:
            continue

        seen_texts.add(clean_val)
        chunks.append({
            "locale": locale,
            "source": f"{source_prefix}:{key}",
            "chunk_text": clean_val,
        })
    return chunks


def extract_html_chunks(file_path, source_prefix, seen_texts):
    """Extract paragraph text from a built HTML file."""
    chunks = []
    try:
        # Built HTML is minified, so read it as utf-8
        with open(file_path, "r", encoding="utf-8-sig") as f:
            html_content = f.read()
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return chunks

    text = _html_to_text(html_content)
    if not text:
        return chunks

    # Split into blocks by double newlines (which represent paragraphs)
    blocks = text.split('\n\n')

    for i, block in enumerate(blocks):
        clean_val = block.strip()
        if len(clean_val) < 20 or clean_val in seen_texts:
            continue

        seen_texts.add(clean_val)

        # Add the source path, appending a block number if there are many
        suffix = f"#p{i+1}" if len(blocks) > 1 else ""
        chunks.append({
            "locale": "en",
            "source": f"{source_prefix}{suffix}",
            "chunk_text": clean_val,
        })
    return chunks


def collect_chunks(source, seen_texts):
    """Collect chunks from exactly one source."""
    chunks = []

    if source == "json":
        for locale in LOCALES:
            lang_dir = LOCALES_DIR / locale
            if not lang_dir.exists():
                print(f"Warning: {lang_dir} not found.")
                continue
            for json_file in lang_dir.rglob("*.json"):
                rel_path = json_file.relative_to(LOCALES_DIR).as_posix()
                chunks.extend(extract_json_chunks(json_file, locale, f"json/{rel_path}", seen_texts))

    elif source == "extra":
        if not EXTRA_DATA_DIR.exists():
            print(f"Warning: {EXTRA_DATA_DIR} not found.")
        else:
            print(f"Reading extra data folder: {EXTRA_DATA_DIR}")
            for json_file in EXTRA_DATA_DIR.rglob("*.json"):
                chunks.extend(extract_json_chunks(json_file, "en", f"extra_data/{json_file.name}", seen_texts))

    elif source == "html":
        if not PUBLIC_DIR.exists():
            print(f"Warning: {PUBLIC_DIR} not found.")
        else:
            print(f"Reading public HTML folder: {PUBLIC_DIR}")

            # Define exactly which folders to pull HTML from
            folders_to_scrape = [
                PUBLIC_DIR,                         # public/*.html (root pages)
                PUBLIC_DIR / "blogs",               # public/blogs/*.html
                PUBLIC_DIR / "legals",              # public/legals/*.html
                PUBLIC_DIR / "agency-reviews",      # public/agency-reviews/*.html
                PUBLIC_DIR / "client-reviews",      # public/client-reviews/*.html
            ]

            for folder in folders_to_scrape:
                if not folder.exists():
                    continue

                # Only files directly inside these folders (not recursive into es/, ar/, etc.)
                for html_file in folder.glob("*.html"):
                    rel_path = html_file.relative_to(PUBLIC_DIR).as_posix()
                    chunks.extend(extract_html_chunks(html_file, f"html/{rel_path}", seen_texts))

    return chunks


def main():
    parser = argparse.ArgumentParser(
        description="Append site content to site_content (never deletes)."
    )
    parser.add_argument(
        "source",
        choices=SOURCES,
        help="Which source to ingest: json, html, or extra",
    )
    args = parser.parse_args()

    if not os.environ.get("DATABASE_URL"):
        print("Error: DATABASE_URL must be set in your environment or .env file.")
        return

    # --- PRE-FLIGHT: verify the embedding API before doing any work ---
    print("Verifying Gemini embedding API...")
    try:
        test_vec = embed_document("Health check for the embedding API.")
        print(f"  OK - got {len(test_vec)}-dim vector.\n")
    except Exception as e:
        print(f"  FAILED: {e}")
        print("  Aborting. Nothing was written.")
        return

    conn = get_conn()
    cur = conn.cursor()

    # Preload existing texts so exact duplicates already in the DB are skipped
    cur.execute("SELECT chunk_text FROM site_content")
    seen_texts = {row[0] for row in cur.fetchall()}
    print(f"Database already holds {len(seen_texts)} chunks (these will be left untouched).")

    chunks = collect_chunks(args.source, seen_texts)
    print(f"[{args.source}] Found {len(chunks)} new chunks (exact matches removed).")

    if not chunks:
        print("Nothing to ingest.")
        cur.close()
        conn.close()
        return

    print("Generating embeddings and performing semantic deduplication...\n")

    inserted = 0
    skipped_semantic = 0
    failed = 0
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 5

    for chunk in chunks:
        try:
            # Write out exactly which file/key it is busy with right now
            print(f"  -> Embedding: {chunk['source']}")
            vector = embed_document(chunk["chunk_text"])
            vec_str = to_vector_literal(vector)

            # Semantic dedupe against EVERYTHING in the table (all sources, prior runs)
            cur.execute("""
                SELECT source, embedding <=> %s::vector AS dist
                FROM site_content
                ORDER BY dist
                LIMIT 1
            """, (vec_str,))
            nearest = cur.fetchone()

            if nearest and nearest[1] is not None and nearest[1] < SEMANTIC_DISTANCE_THRESHOLD:
                print(f"     [SKIP] Semantic duplicate of {nearest[0]}")
                skipped_semantic += 1
                consecutive_failures = 0
                continue

            cur.execute("""
                INSERT INTO site_content (locale, source, chunk_text, embedding)
                VALUES (%s, %s, %s, %s::vector)
            """, (chunk["locale"], chunk["source"], chunk["chunk_text"], vec_str))

            conn.commit()
            inserted += 1
            consecutive_failures = 0

        except Exception as e:
            print(f"  Error processing {chunk['source']}: {e}")
            conn.rollback()
            failed += 1
            consecutive_failures += 1

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print(f"\nAborting: {MAX_CONSECUTIVE_FAILURES} consecutive failures.")
                print("This usually indicates a database schema/index error, not a bad chunk.")
                break

    cur.close()
    conn.close()

    print(f"\n=== Ingestion Summary ({args.source}) ===")
    print(f"Total new chunks found  : {len(chunks)}")
    print(f"Inserted into database  : {inserted}")
    print(f"Skipped (Semantic Dupes): {skipped_semantic}")
    print(f"Failed                  : {failed}")
    print("=========================")


if __name__ == "__main__":
    main()
