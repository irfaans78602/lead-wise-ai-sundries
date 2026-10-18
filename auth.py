"""
auth.py
Google OAuth sign-in for the two portals.

Two OAuth clients, one callback path parameterised by portal. The
portal is enforced by Google: a token issued to the agency client
cannot be replayed against the client callback, because the aud claim
won't match. That makes the portal distinction a fact about the token,
not a hint from a URL parameter.

Sign-in is invite-only. There is no signup form. If the Google sub is
not in client_users (client portal) or agency_users (agency portal),
sign-in is rejected and the operator is emailed the sub and email so
they can add the user via the loader.

The loader owns writes to client_users and agency_users. This module
reads them and establishes a session; it never creates rows.
"""

import os
import json
import secrets
import urllib.parse
import urllib.request

from flask import (
    Blueprint, request, redirect, session, jsonify, current_app,
)
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

from common import get_conn

bp = Blueprint("auth", __name__)

# -------------------------------------------------------------
# Config
# -------------------------------------------------------------

_REDIRECT_BASE   = os.environ.get("GOOGLE_REDIRECT_BASE", "")
_PORTAL_URL      = {
    "client": os.environ.get("PORTAL_URL_CLIENT", "/"),
    "agency": os.environ.get("PORTAL_URL_AGENCY", "/"),
}
_ACCESS_DENIED   = os.environ.get("ACCESS_DENIED_URL", "/access-denied")

_CREDENTIALS = {
    "client": (
        os.environ.get("GOOGLE_CLIENT_ID_CLIENT"),
        os.environ.get("GOOGLE_CLIENT_SECRET_CLIENT"),
    ),
    "agency": (
        os.environ.get("GOOGLE_CLIENT_ID_AGENCY"),
        os.environ.get("GOOGLE_CLIENT_SECRET_AGENCY"),
    ),
}

_RESEND_KEY   = os.environ.get("RESEND_API_KEY")
_NOTIFY_TO    = os.environ.get("SIGNUP_NOTIFY_TO", "contact@lead-wise-ai.com")
_NOTIFY_FROM  = os.environ.get("SIGNUP_NOTIFY_FROM",
                               "LeadWise AI <noreply@lead-wise-ai.com>")

_GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


# -------------------------------------------------------------
# Start — redirect the browser to Google
# -------------------------------------------------------------

@bp.route("/auth/google/start/<portal>")
def google_start(portal):
    if portal not in _CREDENTIALS:
        return jsonify({"error": "unknown portal"}), 404

    client_id, _ = _CREDENTIALS[portal]
    if not client_id:
        return jsonify({"error": f"portal {portal} not configured"}), 500

    # OAuth CSRF nonce. Stored in the signed session cookie; the
    # callback compares it against what Google echoes back.
    nonce = secrets.token_urlsafe(24)
    session[f"oauth_nonce_{portal}"] = nonce

    params = {
        "client_id":     client_id,
        "redirect_uri":  f"{_REDIRECT_BASE}/{portal}",
        "response_type": "code",
        "scope":         "openid email profile",
        "state":         nonce,
        "prompt":        "select_account",
    }
    return redirect(f"{_GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}")


# -------------------------------------------------------------
# Callback — one route, parameterised by portal
# -------------------------------------------------------------

@bp.route("/auth/google/callback/<portal>")
def google_callback(portal):
    if portal not in _CREDENTIALS:
        return jsonify({"error": "unknown portal"}), 404

    client_id, client_secret = _CREDENTIALS[portal]
    if not (client_id and client_secret):
        return jsonify({"error": f"portal {portal} not configured"}), 500

    # 1. CSRF check.
    expected_nonce = session.pop(f"oauth_nonce_{portal}", None)
    provided_nonce = request.args.get("state", "")
    if not expected_nonce or not secrets.compare_digest(expected_nonce, provided_nonce):
        return jsonify({"error": "invalid state"}), 400

    code = request.args.get("code")
    if not code:
        return jsonify({"error": "missing code"}), 400

    # 2. Exchange the code for tokens.
    try:
        tokens = _exchange_code(code, client_id, client_secret,
                                f"{_REDIRECT_BASE}/{portal}")
    except Exception as e:
        print(f"[auth:{portal}] token exchange failed: {e}")
        return jsonify({"error": "token exchange failed"}), 502

    raw_id_token = tokens.get("id_token")
    if not raw_id_token:
        return jsonify({"error": "no id_token in response"}), 502

    # 3. Verify the ID token. This checks signature, iss, aud, exp.
    #    It is the authorization-critical step — never skip it.
    try:
        claims = id_token.verify_oauth2_token(
            raw_id_token,
            google_requests.Request(),
            audience=client_id,
        )
    except Exception as e:
        print(f"[auth:{portal}] id_token verification failed: {e}")
        return jsonify({"error": "invalid id_token"}), 401

    sub   = claims["sub"]
    email = claims.get("email")
    name  = claims.get("name")

    if not claims.get("email_verified"):
        print(f"[auth:{portal}] unverified email for sub={sub}")
        return jsonify({"error": "email not verified"}), 403

    # 4. Look up in the table the portal owns.
    user = _lookup_user(portal, sub)

    if user is None:
        # Invite-only. Notify the operator, reject the sign-in.
        _email_unknown_signup(portal, sub, email, name)
        return redirect(_ACCESS_DENIED)

    # 5. Establish the session. Everything the rest of the app needs
    #    to authorise requests lives here.
    session.clear()
    session["portal"]     = portal
    session["google_sub"] = sub
    session["email"]      = email
    session["display_name"] = name
    if portal == "client":
        session["client_user_id"] = user["client_user_id"]
        session["client_id"]      = user["client_id"]     # may be None
        session["role"]           = user["role"]
    else:
        session["agency_user_id"] = user["agency_user_id"]
        session["agency_id"]      = user["agency_id"]     # may be None
        session["role"]           = user["role"]

    return redirect(_PORTAL_URL[portal])


# -------------------------------------------------------------
# Sign-out
# -------------------------------------------------------------

@bp.route("/auth/signout", methods=["POST", "GET"])
def signout():
    session.clear()
    return redirect("/")


# -------------------------------------------------------------
# Token exchange (raw HTTP, matching embedded_signup.py's style)
# -------------------------------------------------------------

def _exchange_code(code, client_id, client_secret, redirect_uri):
    body = urllib.parse.urlencode({
        "code":          code,
        "client_id":     client_id,
        "client_secret": client_secret,
        "redirect_uri":  redirect_uri,
        "grant_type":    "authorization_code",
    }).encode()

    req = urllib.request.Request(
        _GOOGLE_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


# -------------------------------------------------------------
# DB lookups
# -------------------------------------------------------------

def _lookup_user(portal, sub):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if portal == "client":
                cur.execute(
                    """SELECT client_user_id, client_id, role
                       FROM client_users
                       WHERE google_sub = %s
                         AND end_date = '9999-12-31'""",
                    (sub,),
                )
            else:
                cur.execute(
                    """SELECT agency_user_id, agency_id, role
                       FROM agency_users
                       WHERE google_sub = %s
                         AND end_date = '9999-12-31'""",
                    (sub,),
                )
            return cur.fetchone()
    finally:
        conn.close()


# -------------------------------------------------------------
# Operator email for unknown sign-ups
# -------------------------------------------------------------

def _email_unknown_signup(portal, sub, email, name):
    if not _RESEND_KEY:
        print(f"[auth:{portal}] unknown signup sub={sub} email={email} "
              f"(RESEND_API_KEY unset — no email sent)")
        return

    json_file = "10-clients.json" if portal == "client" else "10-agency.json"
    block     = "client_users"    if portal == "client" else "agency_users"
    table     = "client_users"    if portal == "client" else "agency_users"

    subject = f"Unknown {portal} sign-in: {email or sub}"

    body = f"""Someone tried to sign in to the {portal} portal who is
not yet in the database. Sign-in was rejected.

Portal:       {portal}
Google sub:   {sub}
Email:        {email or "(not provided)"}
Display name: {name or "(not provided)"}

To allow this user, add to LeadWiseAI/{json_file}:

  "{block}": [
    {{
      "google_sub": "{sub}",
      "email": "{email or ''}",
      "display_name": "{name or ''}",
      "role": "owner"
    }}
  ]

Then run:

  loader LeadWiseAI/{json_file} --commit

Until then, this user cannot sign in. The row is written to
{table}; nothing else is created by a rejected sign-in.
"""

    try:
        payload = json.dumps({
            "from":    _NOTIFY_FROM,
            "to":      [_NOTIFY_TO],
            "subject": subject,
            "text":    body,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=payload,
            headers={
                "Authorization": f"Bearer {_RESEND_KEY}",
                "Content-Type":  "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as e:
        print(f"[auth:{portal}] operator email failed: {e}")