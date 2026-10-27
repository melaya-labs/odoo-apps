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
