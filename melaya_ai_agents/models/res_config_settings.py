from odoo import api, fields, models
from odoo.fields import Command

from .melaya_client import (
    DEFAULT_API_URL,
    P_AGENT_GROUPS,
    P_API_URL,
    P_AUTO_JOIN,
    P_PROJECT_NAME,
    P_PROJECT_URL,
    MelayaError,
)

# The Melaya Agent user may never be an administrator.
FORBIDDEN_GROUP_XMLIDS = ("base.group_system", "base.group_erp_manager")
# Offered by default when the apps are installed: Sales, Invoicing, Contacts.
DEFAULT_GROUP_XMLIDS = (
    "sales_team.group_sale_salesman",
    "account.group_account_invoice",
    "base.group_partner_manager",
)


def forbidden_agent_groups(env):
    """Administrator groups plus every group that implies one of them."""
    admin = env["res.groups"]
    for xmlid in FORBIDDEN_GROUP_XMLIDS:
        admin |= env.ref(xmlid, raise_if_not_found=False) or env["res.groups"]
    admin = admin.sudo()
    return admin | admin.mapped("all_implied_by_ids")


def allowed_agent_groups(env, groups):
    """Drop administrator groups, groups implying them and portal/public groups."""
    forbidden = forbidden_agent_groups(env)
    return groups.sudo().filtered(lambda g: g not in forbidden and not g.share)


def default_agent_groups(env):
    groups = env["res.groups"]
    for xmlid in DEFAULT_GROUP_XMLIDS:
        groups |= env.ref(xmlid, raise_if_not_found=False) or env["res.groups"]
    return allowed_agent_groups(env, groups)


def stored_agent_groups(env):
    raw = env["melaya.client"]._param_get(P_AGENT_GROUPS)
    if not raw:
        return default_agent_groups(env)
    ids = [int(x) for x in raw.split(",") if x.strip().isdigit()]
    return allowed_agent_groups(env, env["res.groups"].sudo().browse(ids).exists())


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    melaya_api_url = fields.Char(
        string="Melaya API URL",
        config_parameter=P_API_URL,
        default=DEFAULT_API_URL,
        groups="base.group_system",
        help="Change it only when Melaya support asks you to.",
    )
    melaya_auto_join = fields.Boolean(
        string="Let Odoo users join automatically",
        config_parameter=P_AUTO_JOIN,
        default=True,
        groups="base.group_system",
        help="Melaya / User members who link their Melaya account join the project as Runner if the project owner's plan has a free seat. The project owner can change roles in Melaya.",
    )
    melaya_connected = fields.Boolean(string="Connected to Melaya", compute="_compute_melaya_state", groups="base.group_system")
    melaya_database_copy = fields.Boolean(string="Copy of a connected database", compute="_compute_melaya_state", groups="base.group_system")
    melaya_project_name = fields.Char(string="Melaya project", compute="_compute_melaya_state", groups="base.group_system")
    melaya_project_url = fields.Char(string="Open the project in Melaya", compute="_compute_melaya_state", groups="base.group_system")
    melaya_exposed_count = fields.Integer(string="Exposed models", compute="_compute_melaya_state", groups="base.group_system")
    melaya_agent_group_ids = fields.Many2many(
        "res.groups",
        "melaya_settings_agent_group_rel",
        "config_id",
        "group_id",
        string="Melaya Agent groups",
        domain=lambda self: [("id", "not in", forbidden_agent_groups(self.env).ids), ("share", "=", False)],
        groups="base.group_system",
        help="Odoo groups given to the Melaya Agent user when you connect. Melaya reads and writes Odoo with these rights only. Administrator groups are never allowed.",
    )

    @api.depends_context("uid")
    def _compute_melaya_state(self):
        client = self.env["melaya.client"]
        connected = client._is_connected()
        copy = client._is_database_copy()
        name = client._param_get(P_PROJECT_NAME)
        url = client._param_get(P_PROJECT_URL)
        count = self.env["melaya.exposed.model"].sudo().search_count([("button_active", "=", True)])
        for rec in self:
            rec.melaya_connected = connected
            rec.melaya_database_copy = copy
            rec.melaya_project_name = name if connected else False
            rec.melaya_project_url = url if connected else False
            rec.melaya_exposed_count = count

    @api.model
    def get_values(self):
        res = super().get_values()
        res["melaya_agent_group_ids"] = [Command.set(stored_agent_groups(self.env).ids)]
        return res

    def set_values(self):
        client = self.env["melaya.client"]
        old_auto_join = client._param_get_bool(P_AUTO_JOIN, True)
        super().set_values()
        self._melaya_store_agent_groups()
        if client._is_connected() and bool(self.melaya_auto_join) != old_auto_join:
            self.env["melaya.exposed.model"]._push_to_melaya(auto_join=self.melaya_auto_join)

    def _melaya_store_agent_groups(self):
        groups = allowed_agent_groups(self.env, self.melaya_agent_group_ids)
        self.env["melaya.client"]._param_set(P_AGENT_GROUPS, ",".join(str(i) for i in groups.ids) or "0")

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def action_melaya_connect(self):
        self.env["melaya.client"]._check_admin()
        # A request this admin started and closed before Odoo collected the
        # connection (the dialog was dismissed, the tab closed) is reopened, so
        # "Check connection" can finish it. Otherwise the approved install would
        # sit unused in Melaya until its 30-minute cleanup.
        waiting = self.env["melaya.connect.wizard"].search(
            [("state", "=", "waiting"), ("create_uid", "=", self.env.uid)], order="id desc", limit=1,
        )
        if waiting:
            return {
                "type": "ir.actions.act_window",
                "name": self.env._("Connect to Melaya"),
                "res_model": "melaya.connect.wizard",
                "res_id": waiting.id,
                "view_mode": "form",
                "target": "new",
            }
        self._melaya_store_agent_groups()
        groups = allowed_agent_groups(self.env, self.melaya_agent_group_ids)
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Connect to Melaya"),
            "res_model": "melaya.connect.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_group_ids": [Command.set(groups.ids)]},
        }

    def action_melaya_disconnect(self):
        client = self.env["melaya.client"]
        client._check_admin()
        try:
            client._disconnect(reason="admin")
        except MelayaError as exc:
            # Melaya no longer knows this install (already revoked): clean up locally anyway.
            if exc.status not in (401, 404):
                raise
            client._clear_connection()
        client._revoke_agent_keys()
        client._archive_agent()
        return {"type": "ir.actions.client", "tag": "reload"}

    def action_melaya_rotate_token(self):
        self.env["melaya.client"]._token_rotate()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "message": self.env._("New Melaya token saved. The old one stops working in 10 minutes."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_melaya_diagnostics(self):
        self.env["melaya.client"]._check_admin()
        return self.env["ir.actions.actions"]._for_xml_id("melaya_ai_agents.action_melaya_diagnostics")

    def action_melaya_exposed_models(self):
        return self.env["ir.actions.actions"]._for_xml_id("melaya_ai_agents.action_melaya_exposed_model")
