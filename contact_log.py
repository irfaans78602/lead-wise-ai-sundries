import os
import hashlib
import uuid as _uuid
from flask import Blueprint, request, jsonify
from db import get_conn

bp = Blueprint("contact_log", __name__)

INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")
CONTACT_LOG_IP_SALT = os.environ.get("CONTACT_LOG_IP_SALT", "")

MAX_FIELD_LEN = {
    "name": 200, "email": 200, "company": 200,
    "phone": 50, "enquiry": 100, "message": 5000,
}

def _hash_ip(ip):
    if not ip:
        return None
    return hashlib.sha256((ip + CONTACT_LOG_IP_SALT).encode()).hexdigest()

def _clean(value, max_len):
    return str(value or "").strip()[:max_len] if value is not None else ""

@bp.route("/api/contact-log", methods=["POST"])
def contact_log():
    if not INTERNAL_SECRET:
        print("contact-log: INTERNAL_SECRET not configured")
        return jsonify({"ok": False, "error": "misconfigured"}), 500

    if request.headers.get("X-Internal-Secret") != INTERNAL_SECRET:
        return jsonify({"ok": False, "error": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    submission_id = str(data.get("submission_id") or "").strip()
    
    try:
        _uuid.UUID(submission_id)
    except (ValueError, AttributeError):
        return jsonify({"ok": False, "error": "invalid submission_id"}), 400

    fields = data.get("fields") or {}
    clean = {k: _clean(fields.get(k), m) for k, m in MAX_FIELD_LEN.items()}
    consent = fields.get("consent") is True
    locale = _clean(data.get("locale") or "en", 5) or "en"
    ip_hash = _hash_ip(data.get("remoteip"))
    ua = _clean(data.get("user_agent"), 500) or None
    ref = _clean(data.get("referer"), 500) or None

    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO contacts
                        (submission_id, name, email, company, phone, enquiry, message,
                         consent, locale, ip_hash, user_agent, referer, status, honeypot_hit)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'new', FALSE)
                    ON CONFLICT (submission_id) DO NOTHING
                    """,
                    (
                        submission_id, clean["name"], clean["email"],
                        clean["company"] or None, clean["phone"] or None,
                        clean["enquiry"] or None, clean["message"],
                        consent, locale, ip_hash, ua, ref,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"contact-log DB error: {e}")
        return jsonify({"ok": False, "error": "db_error"}), 500

    return jsonify({"ok": True}), 200