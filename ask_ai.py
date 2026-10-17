"""
ask_ai.py
Handles POST /api/ask-ai
"""
import os
import uuid
import hashlib
from flask import Blueprint, request, jsonify
from db import get_conn
from gemini import embed_query, generate_answer

bp = Blueprint("ask_ai", __name__)

CONTACT_LOG_IP_SALT = os.environ.get("CONTACT_LOG_IP_SALT", "")
MAX_QUESTION_LEN = 1000
SIMILARITY_CUTOFF = 0.8

def _hash_ip(ip):
    if not ip: return None
    return hashlib.sha256((ip + CONTACT_LOG_IP_SALT).encode()).hexdigest()

@bp.route("/api/ask-ai", methods=["POST"])
def ask_ai():
    data = request.get_json(silent=True) or {}
    question = data.get("question", "").strip()
    
    raw_locale = data.get("locale", "en")
    locale = raw_locale[:2].lower() if raw_locale else "en"

    if not question:
        return jsonify({"error": "Question is required"}), 400
    if len(question) > MAX_QUESTION_LEN:
        return jsonify({"error": "Question too long"}), 400

    submission_id = str(uuid.uuid4())
    ip_hash = _hash_ip(request.headers.get("CF-Connecting-IP") or request.remote_addr)
    
    status = "success"
    context_text = ""
    chunks_found = 0

    print(f"[Ask AI] [{submission_id[:8]}] Received query: '{question[:50]}...' (locale: {locale})")

    # 1. Embed the query
    try:
        print(f"[Ask AI] [{submission_id[:8]}] Embedding query...")
        vec = embed_query(question)
        vec_str = "[" + ",".join(f"{v:.8f}" for v in vec) + "]"
    except Exception as e:
        error_msg = str(e)
        if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
            print(f"[Ask AI] [{submission_id[:8]}] Embed failed: Gemini Quota Exceeded (429).")
            status = "embed_error_quota"
            # Return a 503 so the frontend knows it's a temporary server issue, not a bad request
            return jsonify({
                "error": "The AI is currently resting. Please try again later.",
                "submission_id": submission_id
            }), 503
        
        print(f"[Ask AI] [{submission_id[:8]}] Embed failed: {error_msg}")
        status = "embed_error"
        return jsonify({"error": "Processing failed", "submission_id": submission_id}), 500

    # 2. Retrieve context from DB with locale fallback
    try:
        print(f"[Ask AI] [{submission_id[:8]}] Searching database for context...")
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                # Primary locale search
                cur.execute(
                    "SELECT chunk_text, embedding <=> %s::vector AS dist "
                    "FROM site_content WHERE locale = %s "
                    "ORDER BY dist LIMIT 5",
                    (vec_str, locale),
                )
                rows = [r for r in cur.fetchall() if r["dist"] < SIMILARITY_CUTOFF]
                
                # Fallback to English if primary locale yielded nothing
                if not rows and locale != "en":
                    print(f"[Ask AI] [{submission_id[:8]}] No local matches, falling back to 'en'...")
                    cur.execute(
                        "SELECT chunk_text, embedding <=> %s::vector AS dist "
                        "FROM site_content WHERE locale = 'en' "
                        "ORDER BY dist LIMIT 5",
                        (vec_str,),
                    )
                    rows = [r for r in cur.fetchall() if r["dist"] < SIMILARITY_CUTOFF]
                
                chunks_found = len(rows)
                context_text = "\n\n---\n\n".join(r["chunk_text"] for r in rows)
                print(f"[Ask AI] [{submission_id[:8]}] Found {chunks_found} relevant chunks.")
        finally:
            conn.close()
    except Exception as e:
        print(f"[Ask AI] [{submission_id[:8]}] DB retrieve error: {e}")
        status = "db_error"

    # 3. Generate answer
    if not context_text.strip():
        print(f"[Ask AI] [{submission_id[:8]}] No context found. Using fallback message.")
        answer = "I don't have that information, but you can contact us at contact@lead-wise-ai.com"
    else:
        print(f"[Ask AI] [{submission_id[:8]}] Generating answer via Gemini...")
        try:
            answer = generate_answer(question, context_text)
            # If the generation function returns our fallback error, mark the status
            if "having trouble answering" in answer:
                status = "gen_error"
        except Exception as e:
            print(f"[Ask AI] [{submission_id[:8]}] Gen error: {e}")
            status = "gen_error"
            answer = "Sorry, I am having trouble answering right now."

    # 4. Log the interaction
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO ask_ai_log (submission_id, question, answer, locale, ip_hash, status, chunks_found)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (submission_id, question, answer, locale, ip_hash, status, chunks_found))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[Ask AI] [{submission_id[:8]}] DB log error: {e}")

    return jsonify({"answer": answer, "submission_id": submission_id})