"""
embedded_signup.py
Flask blueprint for Meta's Embedded Signup callback.

When a client completes the Embedded Signup flow, Meta redirects back
to this route with query parameters identifying the assets they
authorised. The handler:

  1. Exchanges the authorisation code for a business token.
  2. Fetches the display phone number via the Graph API.
  3. Emails the operator with the provisioned number and the next
     step (add to the client's 10-clients.json, run the loader).

It does NOT write to the database. Config rows for client_whatsapps
and the WhatsApp placeholder ad are loader-owned; the webhook only
notifies the operator that a new number needs to be added to the
client's JSON file.

This keeps the split between runtime and config clean: the loader
writes config, the webhook is a runtime event.

Note: any inbound WhatsApp message on the new number is dropped
until the operator adds it to the client JSON and runs the loader.
The email says so explicitly.

Environment (loaded from .env when present):
  META_APP_ID, META_APP_SECRET   LeadWise AI App credentials
  META_GRAPH_VERSION             e.g. "v20.0" (default if unset)
  RESEND_API_KEY                 Resend API key (required to send)
  SIGNUP_NOTIFY_TO               recipient, default contact@lead-wise-ai.com
  SIGNUP_NOTIFY_FROM             sender, default "LeadWise AI <noreply@lead-wise-ai.com>"
"""

import env  # noqa: F401 — loads .env before the env vars below are read

import os
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify

bp = Blueprint("embedded_signup", __name__)

META_APP_ID       = os.environ.get("META_APP_ID")
META_APP_SECRET   = os.environ.get("META_APP_SECRET")
GRAPH_VERSION     = os.environ.get("META_GRAPH_VERSION", "v20.0")
GRAPH_BASE        = f"https://graph.facebook.com/{GRAPH_VERSION}"

RESEND_API_KEY    = os.environ.get("RESEND_API_KEY")
SIGNUP_NOTIFY_TO  = os.environ.get("SIGNUP_NOTIFY_TO", "contact@lead-wise-ai.com")
SIGNUP_NOTIFY_FROM = os.environ.get(
    "SIGNUP_NOTIFY_FROM", "LeadWise AI <noreply@lead-wise-ai.com>"
)


# -------------------------------------------------------------
# Graph API helpers
# -------------------------------------------------------------

def _graph_get(path, params):
    url = f"{GRAPH_BASE}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _exchange_code_for_token(code):
    return _graph_get(
        "/oauth/access_token",
        {
            "client_id": META_APP_ID,
            "client_secret": META_APP_SECRET,
            "code": code,
        },
    )


def _fetch_phone_info(phone_number_id, access_token):
    return _graph_get(
        f"/{phone_number_id}",
        {
            "fields": "display_phone_number,verified_name",
            "access_token": access_token,
        },
    )


# -------------------------------------------------------------
# Email
# -------------------------------------------------------------

def _send_email(subject, body):
    """POST to Resend. Returns True on success, False otherwise.
    Failures are logged and swallowed: email is a notification, not
    a state change, so the callback still returns 200."""
    if not RESEND_API_KEY:
        print("[embedded_signup] RESEND_API_KEY not set — email skipped")
        return False

    payload = json.dumps({
        "from": SIGNUP_NOTIFY_FROM,
        "to": [SIGNUP_NOTIFY_TO],
        "subject": subject,
        "text": body,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception as e:
        print(f"[embedded_signup] email failed: {e}")
        return False


def _compose_email(client_id, phone_number_id, phone_info):
    """Return (subject, body)."""
    display   = phone_info.get("display_phone_number") or "(could not fetch)"
    verified  = phone_info.get("verified_name") or "(none)"
    received  = datetime.now(timezone.utc).isoformat(timespec="seconds")

    subject = f"Embedded Signup: WhatsApp number {phone_number_id}"

    body = f"""New WhatsApp number provisioned via Embedded Signup.

Client (state):   {client_id}
Phone number ID:  {phone_number_id}
Display number:   {display}
Verified name:    {verified}
Received at:      {received}

Next steps
----------

1. Add this number to clients/<ClientFolder>/10-clients.json:

     "client_whatsapps": [
       {{
         "phone_number_id": "{phone_number_id}",
         "display_phone_number": "{display}"
       }}
     ]

2. Run the loader to apply:

     python -m loader <ClientFolder>/10-clients.json --commit

3. If the number should route to a specific ad, add a leaf to
   60-instance.json (ad_matrix.channels) with
   whatsapp_phone_number_id set to "{phone_number_id}",
   then run the loader for that file too.

Until step 2 completes, any inbound message on this number is
dropped silently (logged as "phone_number_id not registered").
"""
    return subject, body


# -------------------------------------------------------------
# Callback
# -------------------------------------------------------------

@bp.route("/embedded-signup/callback", methods=["GET"])
def embedded_signup_callback():
    code             = request.args.get("code")
    phone_number_id  = request.args.get("phone_number_id")
    client_id        = request.args.get("state")

    if not (code and phone_number_id and client_id):
        return jsonify({"error": "missing required parameters"}), 400

    if not (META_APP_ID and META_APP_SECRET):
        return jsonify({"error": "Meta app credentials not configured"}), 500

    # 1. Exchange the authorisation code for a token.
    try:
        token_result = _exchange_code_for_token(code)
        access_token = token_result["access_token"]
    except Exception as e:
        print(f"[embedded_signup] code exchange failed: {e}")
        return jsonify({"error": "code exchange failed"}), 502

    # 2. Fetch the display phone number. Best-effort: if this fails,
    #    the operator can still read the phone_number_id from the email
    #    and look the number up in Meta Business Suite.
    try:
        phone_info = _fetch_phone_info(phone_number_id, access_token)
    except Exception as e:
        print(f"[embedded_signup] phone lookup failed: {e}")
        phone_info = {}

    # 3. Notify the operator. Best-effort: the number is already
    #    provisioned on Meta's side regardless of whether this email
    #    lands. The operator can retry by inspecting the log.
    subject, body = _compose_email(client_id, phone_number_id, phone_info)
    sent = _send_email(subject, body)

    return jsonify({
        "success":            True,
        "phone_number_id":    phone_number_id,
        "display_phone_number": phone_info.get("display_phone_number"),
        "client_id":          client_id,
        "email_sent":         sent,
    }), 200