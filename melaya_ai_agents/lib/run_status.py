"""Run status vocabulary of the Melaya server, without Odoo imports (tested
inside Odoo and outside it, see integrations/odoo/tests).

Melaya answers ``runs/status`` (and ``card``) with, per run:

* ``approval_needed``: the run waits for an approval in Melaya;
* ``running`` / ``pending`` (``started`` when Melaya has no status yet,
  ``unknown`` when the row has none);
* when the run is over, its outcome: ``success``, ``failed`` or
  ``canceled``; ``error`` when the run row itself failed.

The other words are older or alternative spellings the record panel also
accepts (record_panel.js keeps the same lists).
"""
import re

APPROVAL = frozenset({"approval_needed", "awaiting_approval", "waiting_approval"})
COMPLETED = frozenset({"success", "completed", "succeeded", "done", "finished"})
FAILED = frozenset({"failed", "error"})
CANCELED = frozenset({"canceled", "cancelled", "rejected", "expired"})
TERMINAL = COMPLETED | FAILED | CANCELED
# Placeholders: Melaya has no status for the run (yet).
NO_NEWS = frozenset({"started", "starting", "unknown"})

# melaya.run.state
RUNNING = "running"
WAITING = "approval"
DONE = "done"
STALE = "stale"

_STATUS_RE = re.compile(r"^[a-z][a-z_]{0,39}$")


def clean_status(value, default="unknown"):
    """A status word as Melaya sends it, lower case, or ``default``."""
    if not isinstance(value, str):
        return default
    value = value.strip().lower()
    return value if _STATUS_RE.match(value) else default


def state_for(status):
    """melaya.run.state for a Melaya status."""
    if status in APPROVAL:
        return WAITING
    if status in TERMINAL:
        return DONE
    return RUNNING


def outcome(status):
    """``completed``, ``failed`` or ``canceled`` for a terminal status, else None."""
    if status in COMPLETED:
        return "completed"
    if status in FAILED:
        return "failed"
    if status in CANCELED:
        return "canceled"
    return None
