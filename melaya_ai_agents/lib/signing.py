"""Request signing for the Melaya Odoo app endpoints (contract v1).

Pure Python on purpose: no Odoo import, so the same code is tested inside
Odoo (tests/test_signing.py) and outside it (integrations/odoo/tests).

Signature: ``v1=`` + hex HMAC-SHA256, key = the install token secret (UTF-8),
message = ``POST\\n<path>\\n<timestamp>\\n<nonce>\\n<sha256 hex of the raw body>``.
"""
import hashlib
import hmac
import json
import re
import secrets
import time

TOKEN_PREFIX = "mel_odoo_"
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


class InvalidInstallToken(ValueError):
    """The stored install token does not have the expected shape."""


def parse_install_token(token):
    """Split ``mel_odoo_<install_id>.<secret>`` into ``(install_id, secret)``."""
    if not token or not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
        raise InvalidInstallToken("install token has an unexpected format")
    install_id, sep, secret = token[len(TOKEN_PREFIX):].partition(".")
    if not sep or not install_id or not secret:
        raise InvalidInstallToken("install token has an unexpected format")
    return install_id, secret


def body_bytes(payload):
    """Serialize a JSON body once; the same bytes are hashed and sent."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def make_nonce():
    """32 characters of [A-Za-z0-9_-], single use."""
    return secrets.token_urlsafe(24)


def string_to_sign(path, timestamp, nonce, body):
    body_hash = hashlib.sha256(body).hexdigest()
    return "POST\n%s\n%s\n%s\n%s" % (path, timestamp, nonce, body_hash)


def sign(secret, path, timestamp, nonce, body):
    """Return the ``X-Melaya-Signature`` header value (``v1=<hex>``)."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    message = string_to_sign(path, str(timestamp), nonce, body)
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return "v1=" + digest


def signed_headers(token, path, body, timestamp=None, nonce=None):
    """All headers of a signed call. ``path`` is the request path without query string."""
    install_id, secret = parse_install_token(token)
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    nonce = nonce or make_nonce()
    if not NONCE_RE.match(nonce):
        raise ValueError("nonce must be 16 to 64 characters of [A-Za-z0-9_-]")
    return {
        "Content-Type": "application/json",
        "X-Melaya-Install": install_id,
        "X-Melaya-Timestamp": timestamp,
        "X-Melaya-Nonce": nonce,
        "X-Melaya-Signature": sign(secret, path, timestamp, nonce, body),
    }
