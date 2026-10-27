import json
import re

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.fields import Command

from .melaya_client import P_AUTO_JOIN, P_PIPELINES

MODEL_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
MAX_MODELS = 50
MAX_PIPELINES = 50
# Technical and security models never get a Melaya action.
BLOCKED_PREFIXES = (
    "ir.", "res.users", "res.groups", "res.config", "res.device", "base.", "bus.",
    "auth_", "melaya.", "mail.message", "mail.mail", "payment.token",
)


class MelayaExposedModel(models.Model):
    _name = "melaya.exposed.model"
    _description = "Odoo model exposed to Melaya"
    _order = "model"

    model_id = fields.Many2one(
        "ir.model",
        string="Odoo model",
        required=True,
        ondelete="cascade",
        domain=[("transient", "=", False), ("abstract", "=", False)],
    )
    model = fields.Char(string="Technical name", related="model_id.model", store=True)
    pipeline_names = fields.Text(
        string="Pipelines",
        help="One Melaya pipeline per line, by id or by name, as listed in Pipelines in the Melaya project. Users can run only these pipelines from this kind of record.",
    )
    button_active = fields.Boolean(
        string="Show the Melaya action",
        default=True,
        help="Adds a Melaya entry to the Actions menu of this model's form view for Melaya / User members.",
    )
    group_ids = fields.Many2many(
        "res.groups",
        "melaya_exposed_model_group_rel",
        "exposed_id",
        "group_id",
        string="Allowed groups",
        domain=[("share", "=", False)],
        help="Leave empty for every Melaya / User member. When set, only members of these groups see the Melaya action and can use Melaya on this kind of record.",
    )
    action_id = fields.Many2one("ir.actions.server", string="Melaya action", readonly=True, ondelete="set null", copy=False)
    available_pipelines = fields.Text(string="Pipelines in the Melaya project", compute="_compute_available_pipelines")

    _model_uniq = models.Constraint("unique(model_id)", "This model is already exposed to Melaya.")

    def _compute_available_pipelines(self):
        pipelines = self._available_pipelines()
        text = "\n".join("%s (%s)" % (p["name"], p["id"]) if p["name"] != p["id"] else p["id"] for p in pipelines)
        for rec in self:
            rec.available_pipelines = text

    @api.model
    def _available_pipelines(self):
        """Pipelines of the Melaya project as last synced: [{"id": slug, "name": display name}]."""
        raw = self.env["melaya.client"]._param_get(P_PIPELINES)
        try:
            items = json.loads(raw) if raw else []
        except ValueError:
            items = []
        return [
            {"id": p["id"], "name": p["name"] if isinstance(p.get("name"), str) else p["id"]}
            for p in items
            if isinstance(p, dict) and isinstance(p.get("id"), str)
        ]

    @api.model
    def _resolve_pipeline(self, value):
        """Map a line typed by the admin (id or display name) to the pipeline id Melaya expects."""
        pipelines = self._available_pipelines()
        if any(p["id"] == value for p in pipelines):
            return value
        matches = [p["id"] for p in pipelines if p["name"].strip().lower() == value.strip().lower()]
        return matches[0] if len(matches) == 1 else value

    def _pipeline_list(self):
        self.ensure_one()
        seen = []
        for line in (self.pipeline_names or "").splitlines():
            name = line.strip()
            if name and name not in seen:
                seen.append(name)
        return seen

    @api.constrains("model_id", "pipeline_names")
    def _check_exposure(self):
        for rec in self:
            name = rec.model_id.model or ""
            if not MODEL_RE.match(name) or name.startswith(BLOCKED_PREFIXES):
                raise ValidationError(self.env._("Melaya cannot be enabled on the model %(model)s.", model=name))
            if len(rec._pipeline_list()) > MAX_PIPELINES:
                raise ValidationError(self.env._("A model can expose at most %(count)s pipelines.", count=MAX_PIPELINES))
        if self.search_count([]) > MAX_MODELS:
            raise ValidationError(self.env._("At most %(count)s models can be exposed to Melaya.", count=MAX_MODELS))

    # ------------------------------------------------------------------
    # Bound "Melaya" action (Actions menu of the model's form view)
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._sync_bindings()
        return records

    def write(self, vals):
        res = super().write(vals)
        if {"model_id", "button_active", "group_ids"} & set(vals):
            self._sync_bindings()
        return res

    def unlink(self):
        self._remove_bindings()
        return super().unlink()

    def _sync_bindings(self):
        melaya_users = self.env.ref("melaya_ai_agents.group_melaya_user")
        Action = self.env["ir.actions.server"].sudo()
        for rec in self.sudo():
            model_name = rec.model_id.model
            wanted = rec.button_active and model_name in self.env and MODEL_RE.match(model_name or "")
            if not wanted:
                rec._remove_bindings()
                continue
            vals = {
                "name": "Melaya",
                "model_id": rec.model_id.id,
                "binding_model_id": rec.model_id.id,
                "binding_view_types": "form",
                "state": "code",
                # model_name is checked against MODEL_RE above, so it is safe in a string literal.
                "code": "action = env['melaya.record.panel'].action_open_record_panel('%s', records.ids)" % model_name,
                # Bindings show to members of ANY listed group: with allowed groups set, the
                # panel itself still requires Melaya / User (see melaya.record.panel).
                "group_ids": [Command.set((rec.group_ids or melaya_users).ids)],
            }
            if rec.action_id:
                rec.action_id.write(vals)
            else:
                rec.action_id = Action.create(vals)

    def _user_allowed(self, user):
        """Allowed groups of this row (empty = every Melaya / User member)."""
        self.ensure_one()
        groups = self.sudo().group_ids
        return not groups or bool(groups & user.all_group_ids)

    def _remove_bindings(self):
        actions = self.sudo().mapped("action_id")
        if actions:
            actions.unlink()  # action_id is ondelete="set null"

    # ------------------------------------------------------------------
    # Sync with Melaya (settings/get, settings/set), admin only
    # ------------------------------------------------------------------
    @api.model
    def _exposed_payload(self):
        payload = {}
        for rec in self.search([("button_active", "=", True)]):
            payload[rec.model] = list(dict.fromkeys(self._resolve_pipeline(p) for p in rec._pipeline_list()))
        return payload

    @api.model
    def _push_to_melaya(self, auto_join=None):
        client = self.env["melaya.client"]
        if auto_join is None:
            auto_join = client._param_get_bool(P_AUTO_JOIN, True)
        client._settings_set(auto_join, self._exposed_payload())

    def action_sync_from_melaya(self):
        client = self.env["melaya.client"]
        # A failed call raises before anything local changes: the cached pipeline list is kept.
        data = client._settings_get()
        if isinstance(data.get("pipelines"), list):
            pipelines = [
                {"id": str(p.get("id") or p.get("name")), "name": str(p.get("name") or p.get("id"))}
                for p in data["pipelines"]
                if isinstance(p, dict) and (p.get("id") or p.get("name"))
            ]
            client._param_set(P_PIPELINES, json.dumps(pipelines))
        if "auto_join" in data:
            client._param_set_bool(P_AUTO_JOIN, bool(data.get("auto_join")))
        exposed = data.get("exposed")
        if not isinstance(exposed, dict):
            raise UserError(self.env._("Melaya returned settings this version of the module cannot read."))
        IrModel = self.env["ir.model"].sudo()
        skipped = []
        for model_name, pipeline_ids in exposed.items():
            ir_model = IrModel.search([("model", "=", model_name)], limit=1) if isinstance(model_name, str) else IrModel
            if not ir_model:
                skipped.append(str(model_name))
                continue
            text = "\n".join(p for p in pipeline_ids or [] if isinstance(p, str))
            row = self.search([("model_id", "=", ir_model.id)], limit=1)
            if row:
                row.write({"pipeline_names": text, "button_active": True})
            else:
                self.create({"model_id": ir_model.id, "pipeline_names": text, "button_active": True})
        for row in self.search([("button_active", "=", True)]):
            if row.model not in exposed:
                row.button_active = False
        message = self.env._("Exposed models and pipelines are now the same as in Melaya.")
        if skipped:
            message = self.env._(
                "Exposed models updated. These models are not installed in this database and were skipped: %(models)s",
                models=", ".join(skipped),
            )
        return self._notify(message)

    def action_save_to_melaya(self):
        self._push_to_melaya()
        return self._notify(self.env._("Exposed models and pipelines saved to Melaya."))

    @api.model
    def _notify(self, message):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"message": message, "type": "success", "sticky": False, "next": {"type": "ir.actions.client", "tag": "soft_reload"}},
        }
