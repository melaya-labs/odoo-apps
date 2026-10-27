"""Pure helpers of the Melaya client: no Odoo import, so they are tested
inside Odoo and outside it (integrations/odoo/tests).

* addon_version(): the module version, read once from __manifest__.py.
* is_database_copy(): the database-copy guard (the install token belongs to
  the database.uuid it was issued for).
* clock_offset_seconds() / clock_offset_minutes(): how far this server's clock
  is from Melaya's, from the ``server_time`` Melaya returns.
* ErrorThrottle: decides when the last error is worth writing again.
"""
import ast
import time
from datetime import datetime, timezone
from pathlib import Path

_MANIFEST = Path(__file__).resolve().parents[1] / "__manifest__.py"
_VERSION = None


def addon_version():
    """Version of the module, from __manifest__.py (the single source)."""
    global _VERSION
    if _VERSION is None:
        try:
            _VERSION = str(ast.literal_eval(_MANIFEST.read_text(encoding="utf-8")).get("version") or "0")
        except (OSError, ValueError, SyntaxError):
            _VERSION = "0"
    return _VERSION


def is_database_copy(token_database_uuid, current_database_uuid):
    """True when the stored token was issued for another database.

    An empty ``token_database_uuid`` means a connection made before this
    check existed: not a copy (the caller binds it to the current database).
    """
    if not token_database_uuid:
        return False
    return (current_database_uuid or "") != token_database_uuid


def parse_server_time(value):
    """ISO 8601 string, or Unix seconds / milliseconds, to an aware datetime (UTC), else None."""
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc)
        if isinstance(value, str) and value.strip():
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return None


def clock_offset_seconds(server_time, now=None):
    """Seconds this server is ahead of Melaya (negative: behind), or None."""
    remote = parse_server_time(server_time)
    if remote is None:
        return None
    now = now or datetime.now(timezone.utc)
    return int(round((now - remote).total_seconds()))


def clock_offset_minutes(offset_seconds):
    """Whole minutes for the message, at least 1."""
    return max(1, int(round(abs(offset_seconds) / 60.0)))


class ErrorThrottle:
    """Remember the last recorded error per database; record again only when
    it changed or the stored one is older than ``interval`` seconds."""

    def __init__(self, interval=60.0, clock=time.monotonic):
        self.interval = interval
        self.clock = clock
        self._last = {}

    def should_record(self, scope, key):
        now = self.clock()
        previous = self._last.get(scope)
        if previous and previous[0] == key and now - previous[1] < self.interval:
            return False
        self._last[scope] = (key, now)
        return True
