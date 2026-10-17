"""
ingest_rag.py
Reads the site's English JSON files, plus built HTML files, chunks the values, 
embeds them via Gemini, and inserts them into the site_content table.

Usage:
    python ingest_rag.py json
    python ingest_rag.py html
    python ingest_rag.py extra
    python ingest_rag.py json html extra
"""
import os
import sys
import json
import math
import re
import html as html_lib
from pathlib import Path

from gemini import embed_document
from db import get_conn

# --- Config ---
SITE_REPO = Path(r"C:\Irfaan\leadwiseai\lead-wise-ai.com")
LOCALES_DIR = SITE_REPO / "src" / "data" / "locales"
PUBLIC_DIR = SITE_REPO / "public"
EXTRA_DATA_DIR = Path(__file__).parent / "extra_data"

LOCALES = ["en"]
META_KEYS = {"_comment", "_filename", "_blog"}
SEMANTIC_DISTANCE_THRESHOLD = 0.08

# --- Strict Argument Parsing ---
ARGS = [a.lower() for a in sys.argv[1:]]

if not ARGS:
    print("ERROR: No parameters provided.")
    print("Usage: python ingest_rag.py [json] [html] [extra]")
    print("Example: python ingest_rag.py json html")
    sys.exit(1)

VALID_ARGS = {"json", "html", "extra"}
INVALID_ARGS = set(ARGS) - VALID_ARGS

if INVALID_ARGS:
    print(f"ERROR: Invalid parameter(s): {', '.join(INVALID_ARGS)}")
    print("Allowed parameters are: json, html, extra")
    sys.exit(1)

DO_JSON  = "json" in ARGS
DO_HTML  = "html" in ARGS
DO_EXTRA = "extra" in ARGS


def to_vector_literal(values):
    return "[" + ",".join(f"{v:.8f}" for v in values) + "]"

def cosine_distance(vec1, vec2):
    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    magnitude1 = math.sqrt(sum(a * a for a in vec1))
    magnitude2 = math.sqrt(sum(b * b for b in vec2))
    if magnitude1 == 0 or magnitude2 == 0:
        return 1.0
    similarity = dot_product / (magnitude1 * magnitude2)
    similarity = max(min(similarity, 1.0), -1.0)
    return 1.0 - similarity

def _load_json(file_path):
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            with open(file_path, "r", encoding=enc) as f:
                return json.load(f)
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"Could not decode {file_path}")

def _html_to_text(html_content):
    """Strip HTML tags, scripts, styles, and boilerplate to get clean text."""
    text = re.sub(r'<(script|style)\b[^>]*>[\s\S]*?</\1>', ' ', html_content, flags=re.IGNORECASE)
    text = re.sub(r'<(nav|header|footer|svg)\b[^>]*>[\s\S]*?</\1>', ' ', text, flags=re.IGNORECASE)
    main_match = re.search(r'<main\b[^>]*>([\s\S]*?)</main>', text, flags=re.IGNORECASE)
    if main_match:
        text = main_match.group(1)
    text = re.sub(r'<(p|h[1-6]|li|div|br|tr)\b[^>]*>', '\n\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</(p|h[1-6]|li|div|tr)>', '\n\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html_lib.unescape(text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    return text.strip()

def extract_json_chunks(file_path, locale, source_prefix, seen_texts):
    chunks = []
    try:
        data = _load_json(file_path)
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return chunks

    items = data.items() if isinstance(data, dict) else enumerate(data)
    for key, val in items:
        if isinstance(key, str) and key in META_KEYS: continue
        if not isinstance(val, str): continue
            
        clean_val = val.strip()
        if len(clean_val) < 20 or clean_val in seen_texts: continue
            
        seen_texts.add(clean_val)
        chunks.append({
            "locale": locale,
            "source": f"{source_prefix}:{key}",
            "chunk_text": clean_val,
        })
    return chunks

def extract_html_chunks(file_path, source_prefix, seen_texts):
    chunks = []
    try:
        with open(file_path, "r", encoding="utf-8-sig") as f:
            html_content = f.read()
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return chunks

    text = _html_to_text(html_content)
    if not text: return chunks

    blocks = text.split('\n\n')
    for i, block in enumerate(blocks):
        clean_val = block.strip()
        if len(clean_val) < 20 or clean_val in seen_texts: continue
            
        seen_texts.add(clean_val)
        suffix = f"#p{i+1}" if len(blocks) > 1 else ""
        chunks.append({
            "locale": "en",
            "source": f"{source_prefix}{suffix}",
            "chunk_text": clean_val,
        })
    return chunks

def collect_chunks(seen_texts):
    chunks = []
    
    if DO_JSON:
        print("Scanning main JSON files...")
        for locale in LOCALES:
            lang_dir = LOCALES_DIR / locale
            if lang_dir.exists():
                for json_file in lang_dir.rglob("*.json"):
                    rel_path = json_file.relative_to(LOCALES_DIR).as_posix()
                    chunks.extend(extract_json_chunks(json_file, locale, f"json/{rel_path}", seen_texts))

    if DO_EXTRA:
        if EXTRA_DATA_DIR.exists():
            print(f"Scanning extra data folder: {EXTRA_DATA_DIR}")
            for json_file in EXTRA_DATA_DIR.rglob("*.json"):
                chunks.extend(extract_json_chunks(json_file, "en", f"extra_data/{json_file.name}", seen_texts))
        else:
            print("No extra_data folder found.")

    if DO_HTML:
        if PUBLIC_DIR.exists():
            print(f"Scanning public HTML folder: {PUBLIC_DIR}")
            folders_to_scrape = [
                PUBLIC_DIR,
                PUBLIC_DIR / "blogs",
                PUBLIC_DIR / "legals",
                PUBLIC_DIR / "agency-reviews",
                PUBLIC_DIR / "client-reviews",
            ]
            for folder in folders_to_scrape:
                if not folder.exists(): continue
                for html_file in folder.glob("*.html"):
                    rel_path = html_file.relative_to(PUBLIC_DIR).as_posix()
                    chunks.extend(extract_html_chunks(html_file, f"html/{rel_path}", seen_texts))

    return chunks


def main():
    if not os.environ.get("DATABASE_URL"):
        print("Error: DATABASE_URL must be set in your environment or .env file.")
        return

    print(f"Starting ingestion (JSON: {DO_JSON}, HTML: {DO_HTML}, EXTRA: {DO_EXTRA})...")
    print("Verifying Gemini embedding API...")
    try:
        test_vec = embed_document("Health check.")
        print(f"  OK - got {len(test_vec)}-dim vector.\n")
    except Exception as e:
        print(f"  FAILED: {e}")
        print("  Aborting.")
        return

    conn = get_conn()
    cur = conn.cursor()

    # --- Fetch existing chunks AND embeddings safely ---
    print("Fetching existing text and embeddings from database...")
    cur.execute("SELECT chunk_text, embedding::text FROM site_content")
    existing_rows = cur.fetchall()
    
    seen_texts = set()
    seen_embeddings = []

    for row in existing_rows:
        text = row['chunk_text']
        seen_texts.add(text)
        
        emb_str = row['embedding']
        if emb_str:
            # Parse the pgvector string "[0.1, 0.2, ...]" into a Python list of floats
            emb_vec = [float(x) for x in emb_str.strip('[]').split(',')]
            seen_embeddings.append((emb_vec, "existing_db", text))

    print(f"Loaded {len(seen_texts)} existing texts and {len(seen_embeddings)} embeddings.\n")

    chunks = collect_chunks(seen_texts)
    print(f"\nFound {len(chunks)} NEW chunks to ingest.")
    print("Generating embeddings and performing semantic deduplication...\n")

    if len(chunks) == 0:
        print("No new chunks to add. Exiting.")
        cur.close()
        conn.close()
        return

    inserted = 0
    skipped_semantic = 0
    failed = 0
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 5

    for i, chunk in enumerate(chunks):
        try:
            print(f"  -> Embedding: {chunk['source']}")
            vector = embed_document(chunk["chunk_text"])

            is_duplicate = False
            # Check against ALL previously seen embeddings (from DB and this run)
            for existing_vec, existing_source, existing_text in seen_embeddings:
                if cosine_distance(vector, existing_vec) < SEMANTIC_DISTANCE_THRESHOLD:
                    print(f"     [SKIP] Semantic duplicate of {existing_source}")
                    print(f"            New: '{chunk['chunk_text'][:60]}...'")
                    is_duplicate = True
                    break

            if is_duplicate:
                skipped_semantic += 1
                continue

            vec_str = to_vector_literal(vector)

            cur.execute("""
                INSERT INTO site_content (locale, source, chunk_text, embedding)
                VALUES (%s, %s, %s, %s::vector)
            """, (chunk["locale"], chunk["source"], chunk["chunk_text"], vec_str))

            conn.commit()
            seen_embeddings.append((vector, chunk["source"], chunk["chunk_text"]))
            inserted += 1
            consecutive_failures = 0

        except Exception as e:
            print(f"  Error processing {chunk['source']}: {e}")
            conn.rollback()
            failed += 1
            consecutive_failures += 1
            
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print(f"\nAborting: {MAX_CONSECUTIVE_FAILURES} consecutive failures.")
                break

    cur.close()
    conn.close()

    print("\n=== Ingestion Summary ===")
    print(f"New chunks found        : {len(chunks)}")
    print(f"Inserted into database  : {inserted}")
    print(f"Skipped (Semantic Dupes): {skipped_semantic}")
    print(f"Failed                  : {failed}")
    print("=========================")


if __name__ == "__main__":
    main()