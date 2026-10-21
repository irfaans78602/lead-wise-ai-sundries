"""
ingest_rag.py
Generic ingestion pipeline (JSON, HTML, PDF, Web URL).
"""
import os
import sys
import json
import math
import re
import urllib.request
import urllib.error
from pathlib import Path
from bs4 import BeautifulSoup
from pypdf import PdfReader

from gemini import embed_document
from db import get_conn, get_tenant_config, release_conn

def _clean_heading(text):
    if not text: return ""
    clean = re.sub(r'[>/]', '-', text.strip())
    return clean[:50].strip()

def html_list_to_md(ul_or_ol, level=0):
    md = []
    for li in ul_or_ol.find_all('li', recursive=False):
        parts = []
        for child in li.contents:
            if getattr(child, 'name', None) in ('ul', 'ol'):
                continue
            if isinstance(child, str):
                parts.append(child.strip())
            else:
                parts.append(child.get_text(separator=" ", strip=True))
        text = " ".join(parts).strip()
        prefix = "  " * level + "- "
        md.append(f"{prefix}{text}")
        for nested in li.find_all(['ul', 'ol'], recursive=False):
            md.append(html_list_to_md(nested, level + 1))
    return "\n".join(md)

def extract_hierarchical_chunks(html_content, base_section):
    soup = BeautifulSoup(html_content, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "svg", "form", "aside", "iframe", "noscript"]):
        tag.decompose()
    for el in soup.find_all(True, class_=re.compile(r'(cookie|popup|modal|sidebar|menu|banner|newsletter)', re.I)):
        el.decompose()
    for el in soup.find_all(True, id=re.compile(r'(cookie|popup|modal|sidebar|menu|banner|newsletter)', re.I)):
        el.decompose()

    current_path = [base_section]
    chunks = []

    title_tag = soup.find('title')
    meta_desc = soup.find('meta', attrs={'name': 'description'})
    if title_tag or meta_desc:
        meta_text = ""
        if title_tag: meta_text += f"Page Title: {title_tag.get_text(strip=True)}. "
        if meta_desc: meta_text += f"Description: {meta_desc.get('content', '')}"
        if len(meta_text) > 10:
            chunks.append({"text": meta_text.strip(), "section_path": base_section})

    elements = soup.find_all(['h1', 'h2', 'h3', 'h4', 'p', 'ul', 'ol', 'td', 'th', 'blockquote', 'dt', 'dd'])

    for el in elements:
        if el.name == 'li': continue
        if el.name in ('ul', 'ol'):
            text = html_list_to_md(el)
        else:
            text = el.get_text(separator=" ", strip=True)
            
        if not text or len(text) < 20 or text.lower() in ['read more', 'click here', 'learn more']:
            continue

        if el.name == 'h1':
            h_text = _clean_heading(text)
            if h_text: current_path = [base_section, h_text]
        elif el.name == 'h2':
            h_text = _clean_heading(text)
            if h_text: current_path = current_path[:2] + [h_text]
        elif el.name == 'h3':
            h_text = _clean_heading(text)
            if h_text: current_path = current_path[:3] + [h_text]
        elif el.name == 'h4':
            h_text = _clean_heading(text)
            if h_text: current_path = current_path[:4] + [h_text]
        else:
            if len(text) > 1500: text = text[:1500] + "..."
            chunks.append({"text": text, "section_path": " > ".join(current_path)})
    return chunks

def is_pdf_heading(line):
    line = line.strip()
    if not line or len(line) > 80: return False
    if line.endswith('.') or line.endswith(','): return False
    if re.match(r'^\d+(\.\d+)*\s+[A-Z]', line): return True
    if line.isupper() and len(line.split()) <= 8 and len(line) > 3: return True
    if line.istitle() and len(line.split()) <= 5: return True
    return False

def is_pdf_list_item(line):
    match = re.match(r'^(\s*)([\-\*\u2022\u25AA\u25E6\u2023\u2043\u25A0\u25CF]|\d+[\.\)]|[a-zA-Z][\.\)]|\([a-zA-Z0-9]+\))\s+(.*)$', line)
    if match:
        spaces = match.group(1)
        text = match.group(3)
        level = max(0, len(spaces) // 2)
        return True, level, text
    return False, 0, ""

def extract_pdf_chunks(file_path, section_target, scope_target, seen_texts):
    chunks = []
    try:
        reader = PdfReader(file_path)
        full_text = "".join(page.extract_text() + "\n" for page in reader.pages)
    except Exception as e:
        print(f"Error reading PDF {file_path}: {e}")
        return chunks

    doc_name = Path(file_path).stem
    current_path = [section_target, doc_name]
    lines = full_text.split('\n')
    
    current_block = []
    is_list_block = False

    def flush_block():
        nonlocal is_list_block, current_block
        if not current_block: return
        text = "\n".join(current_block) if is_list_block else " ".join(current_block)
        text = re.sub(r'\s+', ' ', text).strip() if not is_list_block else text.strip()
        if len(text) >= 30 and text not in seen_texts:
            seen_texts.add(text)
            if len(text) > 1500: text = text[:1500] + "..."
            chunks.append({
                "locale": "en", "scope": scope_target, 
                "section_path": " > ".join(current_path),
                "source": f"pdf/{Path(file_path).name}", 
                "chunk_text": text
            })
        current_block.clear()
        is_list_block = False

    for line in lines:
        line_str = line.rstrip()
        line_stripped = line_str.strip()
        if not line_stripped:
            flush_block()
            continue

        if is_pdf_heading(line_stripped):
            flush_block()
            h_text = _clean_heading(line_stripped)
            current_path = [section_target, doc_name, h_text]
            continue

        is_list, level, text = is_pdf_list_item(line_str)
        if is_list:
            if not is_list_block:
                flush_block()
                is_list_block = True
            prefix = "  " * level + "- "
            current_block.append(f"{prefix}{text}")
        else:
            if is_list_block:
                if current_block:
                    current_block[-1] += " " + line_stripped
            else:
                current_block.append(line_stripped)

    flush_block()
    return chunks

def extract_url_chunks(url, section_target, scope_target, seen_texts):
    chunks = []
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            html_content = response.read().decode('utf-8', errors='replace')
    except Exception as e:
        print(f"Error fetching URL {url}: {e}")
        return chunks

    parsed_chunks = extract_hierarchical_chunks(html_content, section_target)
    for i, pc in enumerate(parsed_chunks):
        clean_val = pc["text"]
        if clean_val in seen_texts: continue
        seen_texts.add(clean_val)
        chunks.append({
            "locale": "en", "scope": scope_target, "section_path": pc["section_path"],
            "source": f"url/{url}#chunk{i+1}", "chunk_text": clean_val
        })
    return chunks

def run_ingest(instance_id, source, target=None, scope="public", section="General", refresh=False):
    if scope not in ("public", "internal"):
        print("ERROR: You must specify --scope public OR --scope internal")
        return

    REPO_PATH = os.environ.get("LW_SITE_REPO")
    if source in ("html", "json") and not REPO_PATH:
        print("ERROR: LW_SITE_REPO environment variable is not set.")
        return

    SITE_REPO = Path(REPO_PATH).resolve() if REPO_PATH else None
    LOCALES_DIR = SITE_REPO / "src" / "data" / "locales" if SITE_REPO else None
    PUBLIC_DIR = SITE_REPO / "public" if SITE_REPO else None
    META_KEYS = {"_comment", "_filename", "_blog"}
    SEMANTIC_DISTANCE_THRESHOLD = 0.08

    try:
        config = get_tenant_config(instance_id)
        api_key = config["gemini_api_key"]
    except ValueError as e:
        print(f"Error: {e}")
        return

    print(f"Starting ingestion for {instance_id} (Source: {source}, Scope: {scope}, Root Section: {section}, Refresh: {refresh})...")
    try:
        embed_document("Health check.", api_key)
        print("  Gemini API OK.\n")
    except Exception as e:
        print(f"  FAILED: {e}")
        return

    conn = get_conn(instance_id)
    try:
        cur = conn.cursor()

        if refresh:
            print("Refresh mode enabled. Deleting old chunks for this source...")
            if source == "url" and target:
                cur.execute("DELETE FROM site_content WHERE section_path LIKE %s AND source LIKE %s", (f"{section}%", f"url/{target}%"))
            elif source == "pdf" and target:
                filename = os.path.basename(target)
                cur.execute("DELETE FROM site_content WHERE section_path LIKE %s AND source LIKE %s", (f"{section}%", f"pdf/{filename}%"))
            elif source == "html":
                cur.execute("DELETE FROM site_content WHERE section_path LIKE %s AND source LIKE 'html/%'", (f"{section}%",))
            elif source == "json":
                cur.execute("DELETE FROM site_content WHERE section_path LIKE %s AND source LIKE 'json/%'", (f"{section}%",))
            conn.commit()
            print(f"  Deleted existing chunks for {target or source}.")

        cur.execute("SELECT chunk_text, embedding::text FROM site_content")
        seen_texts = set()
        seen_embeddings = []
        for row in cur.fetchall():
            seen_texts.add(row['chunk_text'])
            if row['embedding']:
                seen_embeddings.append(([float(x) for x in row['embedding'].strip('[]').split(',')], "db", row['chunk_text']))

        chunks = []
        if source == "json" and LOCALES_DIR and LOCALES_DIR.exists():
            for json_file in (LOCALES_DIR / "en").rglob("*.json"):
                data = json.loads(json_file.read_text(encoding="utf-8-sig"))
                for k, v in data.items():
                    if k in META_KEYS or not isinstance(v, str) or len(v.strip()) < 20 or v in seen_texts: continue
                    seen_texts.add(v)
                    chunks.append({
                        "locale": "en", "scope": scope, "section_path": section,
                        "source": f"json/{json_file.name}:{k}", "chunk_text": v.strip()
                    })
        elif source == "html" and PUBLIC_DIR and PUBLIC_DIR.exists():
            for html_file in PUBLIC_DIR.glob("*.html"):
                html_content = html_file.read_text(encoding="utf-8-sig")
                parsed_chunks = extract_hierarchical_chunks(html_content, section)
                for i, pc in enumerate(parsed_chunks):
                    clean_val = pc["text"]
                    if clean_val in seen_texts: continue
                    seen_texts.add(clean_val)
                    chunks.append({
                        "locale": "en", "scope": scope, "section_path": pc["section_path"],
                        "source": f"html/{html_file.name}#chunk{i+1}", "chunk_text": clean_val
                    })
        elif source == "pdf" and target:
            chunks.extend(extract_pdf_chunks(target, section, scope, seen_texts))
        elif source == "url" and target:
            chunks.extend(extract_url_chunks(target, section, scope, seen_texts))
        else:
            print(f"Invalid source '{source}' or missing target.")
            return

        print(f"Found {len(chunks)} NEW chunks to ingest.\n")
        if not chunks:
            print("No new chunks to add. Exiting.")
            return

        inserted = 0
        for i, chunk in enumerate(chunks):
            try:
                print(f"  -> Embedding: {chunk['source']} [{chunk['section_path']}]")
                vector = embed_document(chunk["chunk_text"], api_key)

                if any(cosine_distance(vector, vec) < SEMANTIC_DISTANCE_THRESHOLD for vec, _, _ in seen_embeddings):
                    print(f"     [SKIP] Semantic duplicate.")
                    continue

                cur.execute("""
                    INSERT INTO site_content (locale, scope, section_path, source, chunk_text, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s::vector)
                """, (
                    chunk["locale"], chunk["scope"], chunk["section_path"], 
                    chunk["source"], chunk["chunk_text"], to_vector_literal(vector)
                ))
                conn.commit()
                seen_embeddings.append((vector, chunk["source"], chunk["chunk_text"]))
                inserted += 1
            except Exception as e:
                print(f"  Error processing {chunk['source']}: {e}")
                conn.rollback()

        cur.close()
        print(f"\n=== Summary: {inserted} inserted, {len(chunks)-inserted} skipped/failed ===")
    finally:
        release_conn(conn)

def to_vector_literal(values):
    return "[" + ",".join(f"{v:.8f}" for v in values) + "]"

def cosine_distance(vec1, vec2):
    dot = sum(a * b for a, b in zip(vec1, vec2))
    mag1 = math.sqrt(sum(a * a for a in vec1))
    mag2 = math.sqrt(sum(b * b for b in vec2))
    if mag1 == 0 or mag2 == 0: return 1.0
    return 1.0 - (dot / (mag1 * mag2))

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("instance_id")
    parser.add_argument("source", choices=["url", "pdf", "html", "json"])
    parser.add_argument("target", nargs="?", default=None)
    parser.add_argument("--scope", choices=["public", "internal"], default="public")
    parser.add_argument("--section", default="General")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    
    run_ingest(args.instance_id, args.source, args.target, args.scope, args.section, args.refresh)