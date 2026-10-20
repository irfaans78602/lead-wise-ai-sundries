"""
ask_ai.py
Handles POST /api/ask-ai/<instance_id> securely with CORS enforcement.
"""
import os
import re
import uuid
import hashlib
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse
from flask import Blueprint, request, jsonify
from db import get_conn, read_view, execute_write, get_tenant_config
from gemini import embed_query, generate_answer

bp = Blueprint("ask_ai", __name__)
CONTACT_LOG_IP_SALT = os.environ.get("CONTACT_LOG_IP_SALT", "")
MAX_QUESTION_LEN = 500
SIMILARITY_CUTOFF = 0.72

DEFAULT_RATE_LIMIT_MAX = 25
DEFAULT_RATE_LIMIT_WINDOW = 60
DEFAULT_CHUNK_LIMIT = 10

_settings_cache = {}

def get_settings(instance_id):
    now = time.time()
    if instance_id in _settings_cache:
        cache = _settings_cache[instance_id]
        if cache["data"] and (now - cache["timestamp"] < 300):
            return cache["data"]

    settings = {"rate_limit_max": DEFAULT_RATE_LIMIT_MAX, "rate_limit_window": DEFAULT_RATE_LIMIT_WINDOW, "chunk_limit": DEFAULT_CHUNK_LIMIT}
    try:
        conn = get_conn(instance_id)
        try:
            rows = read_view(conn, """
                SELECT setting_key, setting_value FROM active_settings 
                WHERE setting_key IN ('ask_ai_rate_limit_max', 'ask_ai_rate_limit_window_minutes', 'ask_ai_chunk_limit')
            """)
            db_settings = {row['setting_key']: int(row['setting_value']) for row in rows}
            if 'ask_ai_rate_limit_max' in db_settings: settings['rate_limit_max'] = db_settings['ask_ai_rate_limit_max']
            if 'ask_ai_rate_limit_window_minutes' in db_settings: settings['rate_limit_window'] = db_settings['ask_ai_rate_limit_window_minutes']
            if 'ask_ai_chunk_limit' in db_settings: settings['chunk_limit'] = db_settings['ask_ai_chunk_limit']
        finally:
            conn.close()
    except Exception as e:
        print(f"[Ask AI] Failed to fetch settings for {instance_id}: {e}")

    _settings_cache[instance_id] = {"data": settings, "timestamp": now}
    return settings

def _hash_ip(ip):
    if not ip: return "unknown"
    return hashlib.sha256((ip + CONTACT_LOG_IP_SALT).encode()).hexdigest()

@bp.route("/api/ask-ai/<instance_id>/config", methods=["GET"])
def get_public_config(instance_id):
    """Lightweight endpoint for the frontend to fetch UI settings."""
    try:
        config = get_tenant_config(instance_id)
        return jsonify({"brand_color": config.get("brand_color", "#3B82F6")}), 200
    except ValueError:
        return jsonify({"error": "Invalid instance ID"}), 404

@bp.route("/api/ask-ai/<instance_id>", methods=["POST"])
def ask_ai(instance_id):
    try:
        config = get_tenant_config(instance_id)
        
        # --- STRICT CORS ORIGIN ENFORCEMENT ---
        allowed_origins_str = config.get("allowed_origins")
        if allowed_origins_str:
            # 1. Clean allowed list: remove empty strings (trailing commas), strip whitespace, lowercase
            allowed_list = [o.strip().lower() for o in allowed_origins_str.split(",") if o.strip()]
            
            # 2. Get the request origin (Origin header is standard, Referer is fallback)
            raw_origin = request.headers.get("Origin") or request.headers.get("Referer", "")
            
            if raw_origin:
                # 3. Parse the URL to extract just the base (e.g., "https://acme.com")
                # This prevents "https://acme.com.evil.net" from matching "https://acme.com"
                parsed = urlparse(raw_origin)
                origin_base = f"{parsed.scheme}://{parsed.netloc}".lower()
                
                if origin_base not in allowed_list:
                    print(f"[Ask AI] Blocked unauthorized origin: {origin_base}")
                    return jsonify({"error": "Unauthorized domain"}), 403
            else:
                # If there is no Origin or Referer header, block the request.
                print(f"[Ask AI] Blocked request with missing Origin/Referer header")
                return jsonify({"error": "Missing Origin header"}), 403
        # -------------------------------

        api_key = config["gemini_api_key"]
        instance_type = config["instance_type"]
        scope = 'public' if instance_type == 'public' else 'internal'
        
    except ValueError:
        return jsonify({"error": "Invalid instance ID"}), 404

    data = request.get_json(silent=True) or {}
    question = re.sub(r'<[^>]+>', '', str(data.get("question", "")).strip())
    raw_locale = data.get("locale", "en")
    locale = raw_locale[:2].lower() if raw_locale else "en"
    
    # Convert empty string to None for UUID column
    session_id = data.get("session_id") or None
    section_path = data.get("section_path") 

    if not question or len(question) < 2: 
        return jsonify({"error": "Question is required"}), 400
    if len(question) > MAX_QUESTION_LEN: 
        return jsonify({"error": "Question too long"}), 400

    if question == "/start":
        options = []
        try:
            conn = get_conn(instance_id)
            try:
                rows = read_view(conn, """
                    SELECT DISTINCT split_part(section_path, ' > ', 1) AS root_section 
                    FROM site_content 
                    WHERE scope = %s AND section_path IS NOT NULL
                """, (scope,))
                
                for r in rows:
                    sec = r["root_section"]
                    if sec: options.append({"id": sec, "en": sec})
            finally:
                conn.close()
        except Exception as e:
            print(f"[Ask AI] Failed to fetch sections: {e}")
            
        if not options:
            options = [{"id": "General", "en": "General Questions"}]

        return jsonify({
            "answer": "Welcome! Which area can I help you with today?",
            "type": "radio",
            "options": options
        }), 200

    if question == section_path:
        return jsonify({
            "answer": f"Great. What would you like to know about {section_path}?",
            "type": "text"
        }), 200

    submission_id = str(uuid.uuid4())
    ip = request.headers.get("CF-Connecting-IP") or request.remote_addr
    ip_hash = _hash_ip(ip)

    status = "success"
    context_text = ""
    chunks_found = 0
    history_rows = []

    settings = get_settings(instance_id)
    rate_limit_max, rate_limit_window, chunk_limit = settings['rate_limit_max'], settings['rate_limit_window'], settings['chunk_limit']

    # Rate Limiting
    try:
        conn = get_conn(instance_id)
        try:
            cutoff = datetime.utcnow() - timedelta(minutes=rate_limit_window)
            row = read_view(conn, "SELECT count(*) FROM ask_ai_log WHERE ip_hash = %s AND submitted_at > %s", (ip_hash, cutoff), one=True)
            if row["count"] >= rate_limit_max:
                return jsonify({"error": "You have reached the limit for AI questions."}), 429
        finally: conn.close()
    except Exception as e: print(f"[Ask AI] Rate limit check failed: {e}")

    # Session History
    if session_id:
        try:
            conn = get_conn(instance_id)
            try:
                history_rows = read_view(conn, "SELECT question, answer FROM ask_ai_log WHERE session_id = %s AND ip_hash = %s AND status = 'success' ORDER BY submitted_at DESC LIMIT 3", (session_id, ip_hash))
                history_rows = history_rows[::-1]
            finally: conn.close()
        except Exception: pass

    # Small Talk Bypass
    lower_q = question.lower().strip("!.,? ")
    is_small_talk = False
    if len(lower_q.split()) <= 4:
        fillers = ["hi", "hello", "hey", "thanks", "thank you", "my name is", "i am", "i'm", "ok", "okay"]
        if any(lower_q.startswith(f) for f in fillers): is_small_talk = True
            
    if is_small_talk:
        context_text = "No database context needed for this conversational response."
    else:
        try:
            embed_text = question
            if history_rows and len(question.split()) < 6:
                embed_text = f"{history_rows[-1]['question']} {question}"
            vec = embed_query(embed_text, api_key)
            vec_str = "[" + ",".join(f"{v:.8f}" for v in vec) + "]"
        except Exception as e:
            return jsonify({"error": "Processing failed"}), 500

        try:
            conn = get_conn(instance_id)
            try:
                if section_path:
                    query = f"""
                        SELECT chunk_text, embedding <=> %s::vector AS dist 
                        FROM site_content 
                        WHERE locale = %s AND scope = %s AND section_path LIKE %s
                        ORDER BY dist LIMIT {int(chunk_limit)}
                    """
                    rows = read_view(conn, query, (vec_str, locale, scope, f"{section_path}%"))
                else:
                    query = f"""
                        SELECT chunk_text, embedding <=> %s::vector AS dist 
                        FROM site_content 
                        WHERE locale = %s AND scope = %s
                        ORDER BY dist LIMIT {int(chunk_limit)}
                    """
                    rows = read_view(conn, query, (vec_str, locale, scope))
                
                rows = [r for r in rows if r["dist"] < SIMILARITY_CUTOFF]
                
                if not rows and locale != "en":
                    if section_path:
                        query = f"SELECT chunk_text, embedding <=> %s::vector AS dist FROM site_content WHERE locale = 'en' AND scope = %s AND section_path LIKE %s ORDER BY dist LIMIT {int(chunk_limit)}"
                        rows = read_view(conn, query, (vec_str, scope, f"{section_path}%"))
                    else:
                        query = f"SELECT chunk_text, embedding <=> %s::vector AS dist FROM site_content WHERE locale = 'en' AND scope = %s ORDER BY dist LIMIT {int(chunk_limit)}"
                        rows = read_view(conn, query, (vec_str, scope))
                    rows = [r for r in rows if r["dist"] < SIMILARITY_CUTOFF]
                
                chunks_found = len(rows)
                context_text = "\n\n---\n\n".join(r["chunk_text"] for r in rows)
            finally: 
                conn.close()
        except Exception as e: 
            status = "db_error"

    if not context_text.strip():
        answer = "I don't have that information, but you can contact us directly."
        if not is_small_talk: status = "no_context"
    else:
        try:
            answer = generate_answer(question, context_text, api_key, history_rows)
            if "having trouble answering" in answer: status = "gen_error"
            elif "I don't have that information" in answer and not is_small_talk: status = "unanswered"
        except Exception:
            status = "gen_error"
            answer = "Sorry, I am having trouble answering right now."

    # Log using execute_write wrapper
    try:
        conn = get_conn(instance_id)
        try:
            execute_write(conn, """
                INSERT INTO ask_ai_log (submission_id, session_id, question, answer, locale, ip_hash, status, chunks_found)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (submission_id, session_id, question, answer, locale, ip_hash, status, chunks_found))
        finally: 
            conn.close()
    except Exception: pass

    return jsonify({"answer": answer, "submission_id": submission_id})