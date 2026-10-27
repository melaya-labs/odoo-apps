from odoo import api, fields, models, release
from odoo.exceptions import UserError

from ..lib import guards
from ..models.melaya_client import P_LAST_ERROR, P_PROJECT_NAME, P_TOKEN_DB


def _text(value):
    """Render a diagnostics value (str, number, dict or list) as one line."""
    if value in (None, False, ""):
        return ""
    if isinstance(value, dict):
        if "used" in value and "limit" in value:
            return "%s / %s" % (value.get("used"), value.get("limit"))
        if "name" in value:
            return str(value.get("name"))
        return ", ".join("%s: %s" % (k, v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return ", ".join(_text(v) for v in value)
    return str(value)


class MelayaDiagnostics(models.TransientModel):
    _name = "melaya.diagnostics"
    _description = "Melaya diagnostics"

    connected = fields.Boolean(string="Connected", readonly=True)
    api_url = fields.Char(string="Melaya API URL", readonly=True)
    database_uuid = fields.Char(string="Database UUID", readonly=True)
    token_database_uuid = fields.Char(string="Database UUID of the connection", readonly=True)
    odoo_version = fields.Char(string="Odoo version", readonly=True)
    project = fields.Char(string="Melaya project", readonly=True)
    owner = fields.Char(string="Project owner", readonly=True)
    seats = fields.Char(string="Seats used", readonly=True)
    last_seen_at = fields.Char(string="Last call seen by Melaya", readonly=True)
    server_last_error = fields.Char(string="Last error seen by Melaya", readonly=True)
    local_last_error = fields.Char(string="Last error seen by Odoo", readonly=True)
    clock_offset = fields.Char(string="Clock difference with Melaya", readonly=True)
    error = fields.Text(string="Problem", readonly=True)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        client = self.env["melaya.client"]
        client._check_admin()
        res.update({
            "connected": client._is_connected(),
            "database_uuid": client._database_uuid(),
            "token_database_uuid": client._param_get(P_TOKEN_DB),
            "odoo_version": release.version,
            "project": client._param_get(P_PROJECT_NAME),
            "local_last_error": client._param_get(P_LAST_ERROR),
        })
        try:
            res["api_url"] = client._api_url()
        except UserError as exc:
            res["error"] = exc.args[0] if exc.args else str(exc)
            return res
        if client._is_database_copy():
            res["error"] = client._copy_message()
            return res
        if not res["connected"]:
            res["error"] = self.env._("This database is not connected to Melaya.")
            return res
        try:
            data = client._diagnostics()
        except UserError as exc:
            res["error"] = exc.args[0] if exc.args else str(exc)
            details = getattr(exc, "details", None) or {}
            if getattr(exc, "code", None) == "stale_timestamp":
                # Melaya refused the signature as too old or too new: show by how much.
                res["clock_offset"] = self._clock_offset(details.get("server_time"))
            return res
        res.update({
            "connected": bool(data.get("connected", True)),
            "project": _text(data.get("project")) or res["project"],
            "owner": _text(data.get("owner")),
            "seats": _text(data.get("seats")),
            "last_seen_at": _text(data.get("last_seen_at")),
            "server_last_error": _text(data.get("last_error")),
        })
        server_time = data.get("server_time")
        res["clock_offset"] = self._clock_offset(server_time)
        return res

    @api.model
    def _clock_offset(self, server_time):
        """Melaya refuses signatures more than 300 s off its clock, so a large offset explains 401 errors."""
        seconds = guards.clock_offset_seconds(server_time)
        if seconds is None:
            return ""
        if abs(seconds) < 120:
            return self.env._("%(seconds)s seconds", seconds=seconds)
        return self.env._(
            "%(seconds)s seconds (about %(minutes)s minutes): fix this server's time",
            seconds=seconds,
            minutes=guards.clock_offset_minutes(seconds),
        )
