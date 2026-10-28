from odoo import api, models
from odoo.exceptions import AccessError, MissingError, UserError

from .melaya_exposed_model import MODEL_RE


class MelayaRecordPanel(models.AbstractModel):
    """Server side of the Melaya panel opened from a record.

    Every entry point checks, as the clicking user (never sudo): membership of
    Melaya / User, that the model is exposed, the model's allowed groups, and
    ``check_access('read')`` on the record itself.
    """

    _name = "melaya.record.panel"
    _description = "Melaya record panel"

    @api.model
    def _check_record(self, model_name, record_id):
        if not self.env.user.has_group("melaya_ai_agents.group_melaya_user"):
            raise AccessError(self.env._("Ask your administrator to add you to the Melaya / User group."))
        if not isinstance(model_name, str) or not MODEL_RE.match(model_name) or model_name not in self.env:
            raise UserError(self.env._("Melaya is not available on this kind of record."))
        exposed = self.env["melaya.exposed.model"].sudo().search(
            [("model", "=", model_name), ("button_active", "=", True)], limit=1
        )
        if not exposed:
            raise UserError(self.env._("Melaya is not available on this kind of record."))
        if not exposed._user_allowed(self.env.user):
            raise AccessError(self.env._("Melaya is not available to you on this kind of record. Ask your administrator."))
        unavailable = self.env._("This record does not exist or you cannot open it.")
        try:
            record_id = int(record_id)
        except (TypeError, ValueError):
            raise UserError(unavailable) from None
        record = self.env[model_name].browse(record_id)
        # Access first, existence second, with the same answer for both:
        # a forbidden id and a missing id cannot be told apart.
        try:
            record.check_access("read")
        except (AccessError, MissingError):
            raise UserError(unavailable) from None
        if not record.exists():
            raise UserError(unavailable)
        return record, exposed

    @api.model
    def _ui_config(self):
        """What the web client needs to show the Melaya button in the chatter
        and the Melaya menu in the top bar: nothing unless the database is
        connected and the user is in Melaya / User; then the exposed models
        whose allowed groups include the user and that the user can read.
        Every click is checked again by _check_record."""
        user = self.env.user
        if not user.has_group("melaya_ai_agents.group_melaya_user") or not self.env["melaya.client"]._is_connected():
            return {"enabled": False, "models": []}
        rows = self.env["melaya.exposed.model"].sudo().search([("button_active", "=", True)])
        models_ = sorted({
            row.model for row in rows
            if row.model and MODEL_RE.match(row.model) and row.model in self.env
            and row._user_allowed(user) and self.env[row.model].has_access("read")
        })
        return {"enabled": True, "models": models_}

    @api.model
    def _empty_state(self, model_name):
        """What the panel shows when this kind of record has no pipeline:
        Settings administrators of a connected database can add one from the
        templates (the dialog filtered to this model); others are told whom to ask."""
        user = self.env.user
        can_add = user.has_group("base.group_system") and self.env["melaya.client"]._is_connected()
        return {
            "can_add_templates": bool(can_add),
            "model": model_name if can_add and isinstance(model_name, str) and MODEL_RE.match(model_name) else "",
        }

    @api.model
    def action_open_record_panel(self, model_name, record_ids):
        """Called by the bound "Melaya" server action; returns the panel client action."""
        ids = record_ids if isinstance(record_ids, (list, tuple)) else [record_ids]
        if not ids:
            raise UserError(self.env._("Open a record first."))
        record, _exposed = self._check_record(model_name, ids[0])
        return {
            "type": "ir.actions.client",
            "tag": "melaya_ai_agents.record_panel",
            "params": {
                "model": model_name,
                "record_id": record.id,
                "record_name": record.display_name or "",
            },
        }
