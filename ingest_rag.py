"""
ingest_rag.py
Reads the site's JSON files, chunks the values, embeds them via Gemini, 
and inserts them into the site_content table.
"""

import os
import json
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

from gemini import embed_document

# --- Config ---
SITE_REPO = Path(r"C:\Irfaan\leadwiseai\lead-wise-ai.com")
LOCALES_DIR = SITE_REPO / "src" / "data" / "locales"

DATABASE_URL = os.environ.get("DATABASE_URL")
META_KEYS = {"_comment", "_filename", "_blog"}
LOCALES = ["en", "es", "ar", "fr", "zh"]

def to_vector_literal(values):
    """Format as [f1,f2,...] for pgvector."""
    return "[" + ",".join(f"{v:.8f}" for v in values) + "]"

def collect_chunks():
    """Walk the locale folders and extract string values."""
    chunks = []
    for locale in LOCALES:
        lang_dir = LOCALES_DIR / locale
        if not lang_dir.exists():
            continue
            
        for json_file in lang_dir.rglob("*.json"):
            rel_path = json_file.relative_to(LOCALES_DIR).as_posix()
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                
            for key, val in data.items():
                if key in META_KEYS:
                    continue
                if not isinstance(val, str):
                    continue
                if len(val.strip()) < 20: 
                    continue
                    
                chunks.append({
                    "locale": locale,
                    "source": f"{rel_path}:{key}",
                    "chunk_text": val,
                })
    return chunks

def main():
    if not DATABASE_URL:
        print("Error: DATABASE_URL must be set.")
        return

    chunks = collect_chunks()
    print(f"Found {len(chunks)} chunks to embed.")

    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    cur = conn.cursor()

    # Locale-scoped delete instead of TRUNCATE to avoid downtime
    for locale in LOCALES:
        cur.execute("DELETE FROM site_content WHERE locale = %s;", (locale,))
    conn.commit()
    print("Cleared existing site_content rows for all configured locales.")
    
    inserted = 0
    for i, chunk in enumerate(chunks):
        try:
            # Use the shared embedding function for consistency
            vector = embed_document(chunk["chunk_text"])
            vec_str = to_vector_literal(vector)
            
            cur.execute("""
                INSERT INTO site_content (locale, source, chunk_text, embedding)
                VALUES (%s, %s, %s, %s::vector)
            """, (chunk["locale"], chunk["source"], chunk["chunk_text"], vec_str))
            
            inserted += 1
            if (i + 1) % 10 == 0:
                print(f"  Processed {i + 1}/{len(chunks)}")
                conn.commit()
                
        except Exception as e:
            print(f"  Error embedding {chunk['source']}: {e}")
            conn.rollback()
            
    conn.commit()
    cur.close()
    conn.close()
    print(f"Done. Inserted {inserted} rows into site_content.")

if __name__ == "__main__":
    main()