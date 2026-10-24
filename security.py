"""
security.py
Combined link id (client_id + "-" + ad_id) and HMAC-SHA256 link signing.
"""

import env  # noqa: F401

import hashlib
import hmac
import os
import re
import uuid
from urllib.parse import parse_qs

COMBINED_ID_LENGTH = 73
SEPARATOR = "-"
SEPARATOR_INDEX = 36
MAX_ID_INPUT_LENGTH = 128
MIN_SECRET_CHARS = 64

_SIG_RE = re.compile(r"[0-9a-f]{64}")
_KID_RE = re.compile(r"[A-Za-z0-9_.]{1,32}")


class SigningConfigError(RuntimeError):
    pass


_keys_cache = None


def reset_key_cache():
    global _keys_cache
    _keys_cache = None


def _load_keys():
    global _keys_cache
    if _keys_cache is not None:
        return _keys_cache

    raw = os.environ.get("LINK_SIGNING_KEYS", "").strip()
    if not raw:
        raise SigningConfigError("LINK_SIGNING_KEYS is not set")

    keys = []
    seen = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        kid, sep, secret = item.partition(":")
        if not sep or not _KID_RE.fullmatch(kid):
            raise SigningConfigError("LINK_SIGNING_KEYS entry must look like kid:secret")
        if kid in seen:
            raise SigningConfigError("Duplicate key id in LINK_SIGNING_KEYS: " + kid)
        if len(secret) < MIN_SECRET_CHARS:
            raise SigningConfigError(
                "Secret for key '" + kid + "' is shorter than " + str(MIN_SECRET_CHARS) + " characters")
        seen.add(kid)
        keys.append((kid, secret.encode("utf-8")))

    if not keys:
        raise SigningConfigError("LINK_SIGNING_KEYS contains no usable keys")

    _keys_cache = keys
    return keys


def build_combined_id(client_id, ad_id):
    return str(uuid.UUID(str(client_id))) + SEPARATOR + str(uuid.UUID(str(ad_id)))


def split_combined_id(combined_id):
    if not isinstance(combined_id, str):
        raise ValueError("id must be a string")
    if len(combined_id) != COMBINED_ID_LENGTH:
        raise ValueError("bad id length")
    if combined_id[SEPARATOR_INDEX] != SEPARATOR:
        raise ValueError("bad id separator")
        
    client_id = combined_id[:SEPARATOR_INDEX]
    ad_id = combined_id[SEPARATOR_INDEX + 1:]
    
    try:
        uuid.UUID(client_id)
        uuid.UUID(ad_id)
    except ValueError:
        raise ValueError("invalid UUID format")
        
    return client_id, ad_id


def extract_client_hex(combined_id):
    try:
        return split_combined_id(combined_id)[0]
    except ValueError:
        return None


def parse_id_param(query_string):
    if isinstance(query_string, bytes):
        query_string = query_string.decode("utf-8", "replace")
    values = parse_qs(query_string or "").get("id")
    return values[0] if values else None


def _hmac_hex(secret, combined_id):
    return hmac.new(secret, combined_id.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_combined_id(combined_id):
    split_combined_id(combined_id)
    kid, secret = _load_keys()[0]
    return kid, _hmac_hex(secret, combined_id)


def build_signed_query(client_id, ad_id):
    combined = build_combined_id(client_id, ad_id)
    kid, sig = sign_combined_id(combined)
    return "id=" + combined + "&kid=" + kid + "&sig=" + sig


def verify_signature(combined_id, sig, kid=None):
    keys = _load_keys()

    if not isinstance(combined_id, str) or not isinstance(sig, str):
        return False
    if len(combined_id) > MAX_ID_INPUT_LENGTH or not _SIG_RE.fullmatch(sig):
        return False

    candidates = [k for k in keys if k[0] == kid] if kid else keys
    if not candidates:
        return False

    try:
        sig_bytes = sig.encode("ascii")
        message_ok = combined_id.encode("utf-8")
    except UnicodeEncodeError:
        return False

    ok = False
    for _, secret in candidates:
        expected = hmac.new(secret, message_ok, hashlib.sha256).hexdigest().encode("ascii")
        if hmac.compare_digest(expected, sig_bytes):
            ok = True
    return ok