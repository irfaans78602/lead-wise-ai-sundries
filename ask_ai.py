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
# Cosine distance threshold: only use chunks closer than this (lower = more similar).
# Tune this value against your data.
SIMILARITY_CUTOFF = 0.5

def _hash_ip(ip):
    if not ip: return None
    return hashlib.sha256((ip + CONTACT_LOG_IP_SALT).encode()).hexdigest()

@bp.route("/api/ask-ai", methods=["POST"])
def ask_ai():
    data = request.get_json(silent=True) or {}
    question = data.get("question", "").strip()
    locale = data.get("locale", "en")

    if not question:
        return jsonify({"error": "Question is required"}), 400
    if len(question) > MAX_QUESTION_LEN:
        return jsonify({"error": "Question too long"}), 400

    submission_id = str(uuid.uuid4())
    ip_hash = _hash_ip(request.headers.get("CF-Connecting-IP") or request.remote_addr)
    status = "success"
    context_text = ""

    # 1. Embed the query
    try:
        vec = embed_query(question)
        vec_str = "[" + ",".join(f"{v:.8f}" for v in vec) + "]"
    except Exception as e:
        print(f"Embed error: {e}")
        return jsonify({"error": "Processing failed", "submission_id": submission_id}), 500

    # 2. Retrieve context from DB with locale fallback
    try:
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
                    cur.execute(
                        "SELECT chunk_text, embedding <=> %s::vector AS dist "
                        "FROM site_content WHERE locale = 'en' "
                        "ORDER BY dist LIMIT 5",
                        (vec_str,),
                    )
                    rows = [r for r in cur.fetchall() if r["dist"] < SIMILARITY_CUTOFF]
                
                context_text = "\n\n---\n\n".join(r["chunk_text"] for r in rows)
        finally:
            conn.close()
    except Exception as e:
        print(f"DB retrieve error: {e}")
        status = "db_error"

    # 3. Generate answer (or use a safe fallback)
    if not context_text.strip():
        answer = "I don't have that information, but you can contact us at contact@lead-wise-ai.com"
    else:
        answer = generate_answer(question, context_text)

    # 4. Log the interaction
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO ask_ai_log (submission_id, question, answer, locale, ip_hash, status)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (submission_id, question, answer, locale, ip_hash, status))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"DB log error: {e}")

    return jsonify({"answer": answer, "submission_id": submission_id})