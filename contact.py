"""
contact.py
Handles POST /api/contact. Replaces the old Cloudflare Pages Function.
"""
import os
import json
import re
import uuid
import urllib.request
import hashlib
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
from db import get_conn

bp = Blueprint("contact", __name__)

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
CONTACT_TO = "contact@lead-wise-ai.com"
CONTACT_FROM = "LeadWise AI <noreply@lead-wise-ai.com>"
TURNSTILE_SECRET = os.environ.get("TURNSTILE_SECRET_KEY")
CONTACT_LOG_IP_SALT = os.environ.get("CONTACT_LOG_IP_SALT", "")

# --- Validation limits ---
MAX_NAME_LEN = 200
MAX_EMAIL_LEN = 254
MAX_COMPANY_LEN = 200
MAX_PHONE_LEN = 40
MAX_ENQUIRY_LEN = 100
MAX_MESSAGE_LEN = 5000
MIN_MESSAGE_LEN = 10

# Accept these enquiry values (case-insensitive match against label text).
# Anything else is rejected so a bot can't inject arbitrary text.
ALLOWED_ENQUIRIES = {
    "demo request",
    "pricing",
    "agency partnership",
    "general enquiry",
}

# Simple email shape check. Resend does the real validation; this just
# rejects obvious junk before we spend an API call.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def esc(s):
    """HTML-escape user input for email templates."""
    return (
        str(s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _hash_ip(ip):
    if not ip:
        return None
    return hashlib.sha256((ip + CONTACT_LOG_IP_SALT).encode()).hexdigest()


def _send_email(payload):
    """Send via Resend. Raises on non-2xx so callers can catch failures."""
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if not (200 <= resp.status < 300):
            raise RuntimeError(f"Resend returned {resp.status}")
        return True


def build_notify_html(fields, received_at, submission_id):
    return f"""
    <h2>New contact form message</h2>
    <p><strong>Submission ID:</strong> {esc(submission_id)}</p>
    <p><strong>Name:</strong> {esc(fields.get('name'))}</p>
    <p><strong>Email:</strong> {esc(fields.get('email'))}</p>
    <p><strong>Company:</strong> {esc(fields.get('company')) or '-'}</p>
    <p><strong>Phone:</strong> {esc(fields.get('phone')) or '-'}</p>
    <p><strong>Enquiry type:</strong> {esc(fields.get('enquiry'))}</p>
    <p><strong>Message:</strong></p>
    <p>{esc(fields.get('message')).replace(chr(10), '<br>')}</p>
    <hr>
    <p><small>Consent given: yes. Received {received_at}</small></p>
    """


def build_ack_html(fields, submission_id):
    rows = ""
    for label, key in [
        ("Name", "name"),
        ("Email", "email"),
        ("Company", "company"),
        ("Phone", "phone"),
        ("Enquiry type", "enquiry"),
    ]:
        if fields.get(key):
            rows += f"<p><strong>{label}:</strong> {esc(fields.get(key))}</p>"
    msg = (
        f"<p><strong>Message:</strong></p><p>{esc(fields.get('message')).replace(chr(10), '<br>')}</p>"
        if fields.get("message")
        else ""
    )

    return f"""
    <p>Hi {esc(fields.get('name'))},</p>
    <p>Thanks for getting in touch with LeadWise AI. Your message has been received.</p>
    <p><strong>Reference:</strong> {esc(submission_id)}</p>
    <p><strong>What happens next</strong></p>
    <ul>
      <li><strong>Now</strong> &mdash; This email is your acknowledgement that we have your message.</li>
      <li><strong>Within a few hours, any day</strong> &mdash; A person reads your enquiry and replies.</li>
      <li><strong>Same day</strong> &mdash; Where it needs pricing, scoping, or a call slot, you get that too.</li>
    </ul>
    <p>If your matter is urgent, you can reach us directly at <a href="mailto:contact@lead-wise-ai.com">contact@lead-wise-ai.com</a> or on +27 74 939 786 2.</p>
    <hr>
    <p><small>Here is a copy of what you sent us:</small></p>
    {rows}{msg}
    <p>&mdash; The LeadWise AI team</p>
    """


def _validate_and_normalise(data):
    """Extract, trim, validate. Returns (fields, consent, error_or_None)."""
    raw = {
        "name": (data.get("name") or "").strip(),
        "email": (data.get("email") or "").strip(),
        "company": (data.get("company") or "").strip(),
        "phone": (data.get("phone") or "").strip(),
        "enquiry": (data.get("enquiry") or "").strip(),
        "message": (data.get("message") or "").strip(),
    }

    # Length caps (prevent DB bloat / payload abuse)
    if len(raw["name"]) > MAX_NAME_LEN:
        return None, False, "Name is too long."
    if len(raw["email"]) > MAX_EMAIL_LEN:
        return None, False, "Email is too long."
    if len(raw["company"]) > MAX_COMPANY_LEN:
        return None, False, "Company is too long."
    if len(raw["phone"]) > MAX_PHONE_LEN:
        return None, False, "Phone is too long."
    if len(raw["enquiry"]) > MAX_ENQUIRY_LEN:
        return None, False, "Enquiry type is too long."
    if len(raw["message"]) > MAX_MESSAGE_LEN:
        return None, False, "Message is too long."
    if len(raw["message"]) < MIN_MESSAGE_LEN:
        return None, False, "Message is too short."

    # Required fields
    if not raw["name"]:
        return None, False, "Name is required."
    if not raw["email"] or not EMAIL_RE.match(raw["email"]):
        return None, False, "A valid email is required."
    if not raw["message"]:
        return None, False, "Message is required."
    if not raw["enquiry"]:
        return None, False, "Please choose an enquiry type."

    # Enquiry must be one of the known options (case-insensitive)
    if raw["enquiry"].lower() not in ALLOWED_ENQUIRIES:
        return None, False, "Unknown enquiry type."

    # Consent must be explicitly true
    consent_raw = str(data.get("consent", "")).strip().lower()
    consent = consent_raw in ("true", "on", "1", "yes")
    if not consent:
        return None, False, "Consent is required."

    return raw, consent, None


def _log_contact(submission_id, fields, consent, locale, ip_hash, ua, ref,
                 status="new", honeypot_hit=False):
    """Best-effort insert. Logs and swallows errors."""
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO contacts
                        (submission_id, name, email, company, phone, enquiry, message,
                         consent, locale, ip_hash, user_agent, referer, status, honeypot_hit)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        submission_id,
                        fields.get("name") or "",
                        fields.get("email") or "",
                        fields.get("company") or None,
                        fields.get("phone") or None,
                        fields.get("enquiry") or None,
                        fields.get("message") or "",
                        bool(consent),
                        locale,
                        ip_hash,
                        ua,
                        ref,
                        status,
                        bool(honeypot_hit),
                    ),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"DB log error: {e}")


@bp.route("/api/contact", methods=["POST"])
def contact_submit():
    data = request.form if request.form else (request.get_json(silent=True) or {})
    wants_json = "application/json" in request.headers.get("Accept", "")

    def fail(msg, http=400):
        if wants_json:
            return jsonify({"ok": False, "message": msg}), http
        return "", 303, {"Location": "/contact?error=1"}

    # --- Submission ID generated before any side effect ---
    submission_id = str(uuid.uuid4())

    # --- Request metadata (single source of truth) ---
    cf_ip = request.headers.get("CF-Connecting-IP")
    ip_hash = _hash_ip(cf_ip) if cf_ip else None
    ua = request.user_agent.string if request.user_agent else None
    ref = request.referrer
    # Only `en` is ingested; store it explicitly so analytics can filter later.
    locale = "en"

    honeypot = data.get("website")

    # --- Validate & normalise ---
    fields, consent, err = _validate_and_normalise(data)

    # Honeypot first: log as spam, return generic success, never email.
    if honeypot:
        _log_contact(
            submission_id,
            fields or {},
            consent,
            locale,
            ip_hash,
            ua,
            ref,
            status="spam",
            honeypot_hit=True,
        )
        if wants_json:
            return jsonify({"ok": True, "message": "Sent"})
        return "", 303, {"Location": "/contact-message"}

    if err:
        return fail(err, 422)

    # --- Turnstile: mandatory whenever the secret is configured ---
    turnstile_token = data.get("cf-turnstile-response")
    if TURNSTILE_SECRET:
        if not turnstile_token:
            return fail("Captcha required.", 400)
        try:
            req = urllib.request.Request(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data=json.dumps(
                    {"secret": TURNSTILE_SECRET, "response": turnstile_token}
                ).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                if not json.loads(r.read()).get("success"):
                    raise ValueError("Turnstile failed")
        except Exception as e:
            print(f"Turnstile error: {e}")
            return fail("Captcha verification failed.", 400)

    received_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # --- Notification email (hard failure: user sees an error) ---
    try:
        _send_email(
            {
                "from": CONTACT_FROM,
                "to": [CONTACT_TO],
                "reply_to": fields["email"],
                "subject": (
                    f"[Website] {fields['enquiry']} - {fields['name']}"
                    f" [{submission_id}]"
                ).replace("\n", " "),
                "html": build_notify_html(fields, received_at, submission_id),
            }
        )
    except Exception as e:
        print(f"Resend error (notification): {e}")
        return fail("Could not send message right now. Please email us directly.", 503)

    # --- Auto-reply (soft failure: log and continue) ---
    try:
        _send_email(
            {
                "from": CONTACT_FROM,
                "to": [fields["email"]],
                "reply_to": CONTACT_TO,
                "subject": "We've received your message \u2014 LeadWise AI",
                "html": build_ack_html(fields, submission_id),
            }
        )
    except Exception as e:
        print(f"Resend error (auto-reply): {e}")

    # --- Persist (best-effort; user already has a valid acknowledgement) ---
    _log_contact(
        submission_id,
        fields,
        consent,
        locale,
        ip_hash,
        ua,
        ref,
        status="new",
        honeypot_hit=False,
    )

    if wants_json:
        return jsonify({"ok": True, "message": "Sent", "submission_id": submission_id})
    return "", 303, {"Location": "/contact-message"}