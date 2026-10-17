"""
ask_ai.py
Handles POST /api/ask-ai securely.
"""
import os
import re
import uuid
import hashlib
import urllib.request
import urllib.parse
import json
import time
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify
from db import get_conn
from gemini import embed_query, generate_answer

bp = Blueprint("ask_ai", __name__)

CONTACT_LOG_IP_SALT = os.environ.get("CONTACT_LOG_IP_SALT", "")
TURNSTILE_SECRET = os.environ.get("TURNSTILE_SECRET_KEY", "")

MAX_QUESTION_LEN = 500
SIMILARITY_CUTOFF = 0.72  # Lowered to catch more relevant chunks

# Fallbacks
DEFAULT_RATE_LIMIT_MAX = 25
DEFAULT_RATE_LIMIT_WINDOW = 60
DEFAULT_CHUNK_LIMIT = 10

# --- Settings Cache ---
_settings_cache = {
    "data": None,
    "timestamp": 0
}
CACHE_TTL_SECONDS = 300

def get_settings():
    now = time.time()
    if _settings_cache["data"] and (now - _settings_cache["timestamp"] < CACHE_TTL_SECONDS):
        return _settings_cache["data"]

    settings = {
        "rate_limit_max": DEFAULT_RATE_LIMIT_MAX,
        "rate_limit_window": DEFAULT_RATE_LIMIT_WINDOW,
        "chunk_limit": DEFAULT_CHUNK_LIMIT
    }
    
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT setting_key, setting_value 
                    FROM settings 
                    WHERE setting_key IN ('ask_ai_rate_limit_max', 'ask_ai_rate_limit_window_minutes', 'ask_ai_chunk_limit')
                    AND CURRENT_DATE BETWEEN start_date AND end_date
                """)
                rows = cur.fetchall()
                db_settings = {row['setting_key']: int(row['setting_value']) for row in rows}
                
                if 'ask_ai_rate_limit_max' in db_settings:
                    settings['rate_limit_max'] = db_settings['ask_ai_rate_limit_max']
                if 'ask_ai_rate_limit_window_minutes' in db_settings:
                    settings['rate_limit_window'] = db_settings['ask_ai_rate_limit_window_minutes']
                if 'ask_ai_chunk_limit' in db_settings:
                    settings['chunk_limit'] = db_settings['ask_ai_chunk_limit']
        finally:
            conn.close()
    except Exception as e:
        print(f"[Ask AI] Failed to fetch settings from DB: {e}")

    _settings_cache["data"] = settings
    _settings_cache["timestamp"] = now
    return settings

def _hash_ip(ip):
    if not ip: return "unknown"
    return hashlib.sha256((ip + CONTACT_LOG_IP_SALT).encode()).hexdigest()

def verify_turnstile(token, ip):
    if not token: return False
    if not TURNSTILE_SECRET: return True
    
    url = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    data = urllib.parse.urlencode({
        "secret": TURNSTILE_SECRET,
        "response": token,
        "remoteip": ip
    }).encode("utf-8")
    
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
            return result.get("success", False)
    except Exception as e:
        print(f"[Ask AI] Turnstile verify error: {e}")
        return False

@bp.route("/api/ask-ai", methods=["POST"])
def ask_ai():
    data = request.get_json(silent=True) or {}
    
    question = str(data.get("question", "")).strip()
    question = re.sub(r'<[^>]+>', '', question) 
    
    raw_locale = data.get("locale", "en")
    locale = raw_locale[:2].lower() if raw_locale else "en"
    session_id = data.get("session_id", "")
    turnstile_token = data.get("turnstile_token", "")

    if not question or len(question) < 2:
        return jsonify({"error": "Question is required"}), 400
    if len(question) > MAX_QUESTION_LEN:
        return jsonify({"error": "Question too long"}), 400

    submission_id = str(uuid.uuid4())
    ip = request.headers.get("CF-Connecting-IP") or request.remote_addr
    ip_hash = _hash_ip(ip)

    print(f"[Ask AI] [{submission_id[:8]}] Received query: '{question[:50]}...' (locale: {locale})")

    if not verify_turnstile(turnstile_token, ip):
        print(f"[Ask AI] [{submission_id[:8]}] Turnstile failed for IP {ip}")
        return jsonify({"error": "Security check failed. Please refresh and try again."}), 403

    status = "success"
    context_text = ""
    chunks_found = 0
    history_rows = []

    settings = get_settings()
    rate_limit_max = settings['rate_limit_max']
    rate_limit_window = settings['rate_limit_window']
    chunk_limit = settings['chunk_limit']

    # Rate Limiting
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cutoff = datetime.utcnow() - timedelta(minutes=rate_limit_window)
                cur.execute(
                    "SELECT count(*) FROM ask_ai_log WHERE ip_hash = %s AND submitted_at > %s",
                    (ip_hash, cutoff)
                )
                count = cur.fetchone()["count"]
                if count >= rate_limit_max:
                    print(f"[Ask AI] [{submission_id[:8]}] Rate limit exceeded ({count}/{rate_limit_max} per {rate_limit_window}m)")
                    return jsonify({"error": "You have reached the limit for AI questions. Please try again later.", "submission_id": submission_id}), 429
        finally:
            conn.close()
    except Exception as e:
        print(f"[Ask AI] [{submission_id[:8]}] Rate limit check failed: {e}")

    # Session History
    if session_id:
        try:
            conn = get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT question, answer FROM ask_ai_log "
                        "WHERE session_id = %s AND ip_hash = %s AND status = 'success' "
                        "ORDER BY submitted_at DESC LIMIT 3",
                        (session_id, ip_hash)
                    )
                    history_rows = cur.fetchall()[::-1]
                    print(f"[Ask AI] [{submission_id[:8]}] Fetched {len(history_rows)} history messages.")
            finally:
                conn.close()
        except Exception as e:
            print(f"[Ask AI] [{submission_id[:8]}] Failed to fetch history: {e}")

    # Small Talk Bypass
    lower_q = question.lower().strip("!.,? ")
    is_small_talk = False
    if len(lower_q.split()) <= 4:
        fillers = ["hi", "hello", "hey", "thanks", "thank you", "my name is", "i am", "i'm", "ok", "okay"]
        if any(lower_q.startswith(f) for f in fillers):
            is_small_talk = True
            
    if is_small_talk:
        print(f"[Ask AI] [{submission_id[:8]}] Detected small talk. Bypassing DB search.")
        context_text = "No database context needed for this conversational response."
    else:
        # Embed and Retrieve
        try:
            print(f"[Ask AI] [{submission_id[:8]}] Embedding query...")
            embed_text = question
            if history_rows and len(question.split()) < 6:
                embed_text = f"{history_rows[-1]['question']} {question}"
                print(f"[Ask AI] [{submission_id[:8]}] Augmented embed text: '{embed_text}'")
                
            vec = embed_query(embed_text)
            vec_str = "[" + ",".join(f"{v:.8f}" for v in vec) + "]"
        except Exception as e:
            error_msg = str(e)
            if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
                print(f"[Ask AI] [{submission_id[:8]}] Embed failed: Gemini Quota Exceeded (429).")
                status = "embed_error_quota"
                return jsonify({"error": "The AI is currently resting. Please try again later.", "submission_id": submission_id}), 503
            
            print(f"[Ask AI] [{submission_id[:8]}] Embed failed: {error_msg}")
            status = "embed_error"
            return jsonify({"error": "Processing failed", "submission_id": submission_id}), 500

        try:
            print(f"[Ask AI] [{submission_id[:8]}] Searching database for context (limit: {chunk_limit})...")
            conn = get_conn()
            try:
                with conn.cursor() as cur:
                    query_primary = f"""
                        SELECT chunk_text, embedding <=> %s::vector AS dist 
                        FROM site_content WHERE locale = %s 
                        ORDER BY dist LIMIT {int(chunk_limit)}
                    """
                    cur.execute(query_primary, (vec_str, locale))
                    rows = [r for r in cur.fetchall() if r["dist"] < SIMILARITY_CUTOFF]
                    
                    if not rows and locale != "en":
                        print(f"[Ask AI] [{submission_id[:8]}] No local matches, falling back to 'en'...")
                        query_fallback = f"""
                            SELECT chunk_text, embedding <=> %s::vector AS dist 
                            FROM site_content WHERE locale = 'en' 
                            ORDER BY dist LIMIT {int(chunk_limit)}
                        """
                        cur.execute(query_fallback, (vec_str,))
                        rows = [r for r in cur.fetchall() if r["dist"] < SIMILARITY_CUTOFF]
                    
                    chunks_found = len(rows)
                    context_text = "\n\n---\n\n".join(r["chunk_text"] for r in rows)
                    print(f"[Ask AI] [{submission_id[:8]}] Found {chunks_found} relevant chunks.")
            finally:
                conn.close()
        except Exception as e:
            print(f"[Ask AI] [{submission_id[:8]}] DB retrieve error: {e}")
            status = "db_error"

    # Generate Answer
    if not context_text.strip():
        print(f"[Ask AI] [{submission_id[:8]}] No context found. Using fallback message.")
        answer = "I don't have that information, but you can contact us at contact@lead-wise-ai.com"
        if not is_small_talk:
            status = "no_context"
    else:
        print(f"[Ask AI] [{submission_id[:8]}] Generating answer via Gemini...")
        try:
            answer = generate_answer(question, context_text, history_rows)
            if "having trouble answering" in answer:
                status = "gen_error"
            elif "I don't have that information" in answer and not is_small_talk:
                status = "unanswered"
        except Exception as e:
            print(f"[Ask AI] [{submission_id[:8]}] Gen error: {e}")
            status = "gen_error"
            answer = "Sorry, I am having trouble answering right now."

    # Log the interaction
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO ask_ai_log (submission_id, session_id, question, answer, locale, ip_hash, status, chunks_found)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (submission_id, session_id, question, answer, locale, ip_hash, status, chunks_found))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[Ask AI] [{submission_id[:8]}] DB log error: {e}")

    return jsonify({"answer": answer, "submission_id": submission_id})