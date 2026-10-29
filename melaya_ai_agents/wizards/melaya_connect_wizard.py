# Odoo 19.0 overlay of melaya_ai_agents/wizards/melaya_connect_wizard.py (20.0).
# Difference: no odoo.tools.BinaryBytes in 19 (new in 20). The avatar is
# written base64-encoded and image_1920 reads back base64, so
# agent_avatar() and has_placeholder_avatar() encode and decode it.
# Keep in sync: python integrations/odoo/compat/check_overlay.py
import base64
import logging
import re

from odoo import fields, models, release
from odoo.exceptions import UserError
from odoo.fields import Command
from odoo.tools import file_open

from ..models.melaya_client import P_AGENT_USER, safe_url
from ..models.res_config_settings import allowed_agent_groups, forbidden_agent_groups

_logger = logging.getLogger(__name__)

AGENT_NAME = "Melaya Agent"
AGENT_AVATAR = "melaya_ai_agents/static/img/melaya_agent_avatar.png"
API_KEY_NAME = "Melaya"


def agent_avatar():
    """The Melaya logo, for the Melaya Agent's picture in the chatter (base64, as Odoo 19 binary fields take it)."""
    with file_open(AGENT_AVATAR, "rb") as f:
        return base64.b64encode(f.read())


def has_placeholder_avatar(user):
    """True when the user has no picture or only Odoo's generated initial (an SVG)."""
    image = user.image_1920
    if not image:
        return True
    try:
        head = base64.b64decode(image)
    except (ValueError, TypeError):
        head = bytes(image)
    return head.lstrip()[:5] in (b"<?xml", b"<svg ")
CONFIRM_CODE_RE = re.compile(r"^[A-Z0-9-]{3,20}$")


class MelayaConnectWizard(models.TransientModel):
    _name = "melaya.connect.wizard"
    _description = "Connect Odoo to Melaya"

    state = fields.Selection(
        [
            ("draft", "Review"),
            ("waiting", "Waiting for approval"),
            ("done", "Connected"),
            ("failed", "Not connected"),
        ],
        string="Step",
        default="draft",
        required=True,
    )
    database_name = fields.Char(string="Database", default=lambda self: self.env.cr.dbname, readonly=True)
    base_url = fields.Char(
        string="Odoo URL",
        default=lambda self: self.env["melaya.client"]._param_get("web.base.url"),
        readonly=True,
    )
    agent_login = fields.Char(
        string="Melaya Agent login",
        default=lambda self: "melaya.agent@%s" % self.env.cr.dbname,
        readonly=True,
    )
    group_ids = fields.Many2many(
        "res.groups",
        "melaya_connect_wizard_group_rel",
        "wizard_id",
        "group_id",
        string="Melaya Agent groups",
        domain=lambda self: [("id", "not in", forbidden_agent_groups(self.env).ids), ("share", "=", False)],
        help="Melaya reads and writes Odoo as the Melaya Agent user, with these groups only.",
    )
    consent = fields.Boolean(string="I agree to send this data to Melaya")
    approve_url = fields.Char(string="Approval page", readonly=True)
    confirm_code = fields.Char(string="Confirmation code", readonly=True)
    message = fields.Text(string="Message", readonly=True)
    # Kept on the wizard between clicks, never shown. Transient records are
    # readable by their creator only.
    connect_id = fields.Char(string="Connection request", groups="base.group_system")
    poll_secret = fields.Char(string="Poll secret", groups="base.group_system")
    agent_user_id = fields.Many2one("res.users", string="Melaya Agent user", readonly=True)
    agent_user_created = fields.Boolean(string="Created by this wizard", groups="base.group_system")

    # ------------------------------------------------------------------
    # Agent user and API key
    # ------------------------------------------------------------------
    def _ensure_agent_user(self, groups):
        login = self.agent_login or "melaya.agent@%s" % self.env.cr.dbname
        Users = self.env["res.users"].sudo().with_context(active_test=False, no_reset_password=True)
        group_ids = (groups | self.env.ref("base.group_user")).ids
        user = Users.search([("login", "=", login)], limit=1)
        created = False
        if user:
            vals = {"active": True, "group_ids": [Command.set(group_ids)]}
            if has_placeholder_avatar(user):
                vals["image_1920"] = agent_avatar()
            user.write(vals)
        else:
            user = Users.create({
                "name": AGENT_NAME,
                "login": login,
                "group_ids": [Command.set(group_ids)],
                "image_1920": agent_avatar(),
            })
            created = True
        if user.has_group("base.group_system") or user.has_group("base.group_erp_manager"):
            raise UserError(self.env._("The Melaya Agent user would get administrator rights. Remove those groups and try again."))
        self.env["melaya.client"]._param_set(P_AGENT_USER, user.id)
        return user, created

    def _generate_api_key(self, user):
        Keys = self.env["res.users.apikeys"]
        Keys.sudo().search([("user_id", "=", user.id)])._remove()
        as_agent = Keys.with_user(user).sudo()  # the key belongs to env.user; sudo allows no expiry
        try:
            return as_agent._generate("rpc", API_KEY_NAME, None)
        except TypeError:
            # Older signature without the expiration date argument.
            return as_agent._generate("rpc", API_KEY_NAME)

    def _cleanup_agent(self):
        """After an unfinished connect (cancelled, rejected, expired, discarded):
        delete the Agent's key and archive or delete the user if this wizard
        created it. Skipped when the database got connected meanwhile, or when
        another unfinished wizard uses the same Agent user (its key is live)."""
        client = self.env["melaya.client"]
        for wizard in self.sudo():
            user = wizard.agent_user_id
            if not user:
                continue
            if client._is_connected() and client._agent_user() == user:
                continue
            others = self.sudo().search_count([
                ("id", "not in", self.ids),
                ("state", "=", "waiting"),
                ("agent_user_id", "=", user.id),
            ])
            if others:
                continue
            self.env["res.users.apikeys"].sudo().search([("user_id", "=", user.id)])._remove()
            if wizard.agent_user_created:
                try:
                    with self.env.cr.savepoint():
                        user.unlink()
                except Exception:  # noqa: BLE001 (referenced user: archive instead)
                    user.active = False
            else:
                user.active = False
            if client._agent_user() == user:
                client._param_set(P_AGENT_USER, None)

    def _cancel_remote(self):
        """Withdraw unfinished connect requests in Melaya (best effort)."""
        client = self.env["melaya.client"]
        for wizard in self.sudo():
            if wizard.state != "waiting" or not wizard.connect_id or not wizard.poll_secret:
                continue
            try:
                client._connect_cancel(wizard.connect_id, wizard.poll_secret)
            except Exception:  # noqa: BLE001 (Melaya expires the request after 15 minutes anyway)
                _logger.info("Melaya connect cancel failed for database %s", self.env.cr.dbname)

    def _abandon(self):
        """Cancel in Melaya, then clean up locally. Used by Cancel and when the
        wizard record is discarded (closed with the X, or vacuumed)."""
        waiting = self.sudo().filtered(lambda w: w.state == "waiting")
        if not waiting:
            return
        waiting._cancel_remote()
        waiting._cleanup_agent()
        waiting.write({"state": "failed", "connect_id": False, "poll_secret": False})

    def unlink(self):
        # Transient records are vacuumed (as superuser) about an hour after
        # their last change; an unfinished connect still holds an API key.
        try:
            with self.env.cr.savepoint():
                self._abandon()
        except Exception:  # noqa: BLE001 (never block the vacuum)
            _logger.warning("Melaya connect cleanup failed for database %s", self.env.cr.dbname)
        return super().unlink()

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def _reopen(self):
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Connect to Melaya"),
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_start(self):
        self.ensure_one()
        client = self.env["melaya.client"]
        client._check_admin()
        if client._is_connected():
            raise UserError(self.env._("This database is already connected to Melaya. Disconnect it first."))
        if not self.consent:
            raise UserError(self.env._("Tick I agree to send this data to Melaya to continue."))
        groups = allowed_agent_groups(self.env, self.group_ids)
        if not groups:
            raise UserError(self.env._("Pick at least one group for the Melaya Agent user."))
        # Another unfinished connect would hold a key this one replaces: withdraw it first.
        others = self.sudo().search([("id", "!=", self.id), ("state", "=", "waiting")])
        if others:
            others._cancel_remote()
            others.write({"state": "failed", "connect_id": False, "poll_secret": False})
        user, created = self._ensure_agent_user(groups)
        api_key = self._generate_api_key(user)
        payload = {
            "database_uuid": client._database_uuid(),
            "db_name": self.env.cr.dbname,
            "base_url": client._param_get("web.base.url"),
            "odoo_version": release.version,
            "agent_login": user.login,
            "agent_api_key": api_key,
            "agent_groups": groups.mapped("full_name"),
            "admin_login": self.env.user.login,
            "admin_name": self.env.user.name,
        }
        # If this call fails, the exception rolls back the user and the key.
        data = client._connect_start(payload)
        connect_id = data.get("connect_id")
        poll_secret = data.get("poll_secret")
        approve_url = safe_url(data.get("approve_url"))
        if not (isinstance(connect_id, str) and isinstance(poll_secret, str) and approve_url):
            raise UserError(self.env._("Melaya returned an answer this version of the module cannot read. Nothing was changed."))
        code = data.get("confirm_code")
        code = code.strip().upper() if isinstance(code, str) and CONFIRM_CODE_RE.match(code.strip().upper()) else False
        self.sudo().write({
            "state": "waiting",
            "connect_id": connect_id,
            "poll_secret": poll_secret,
            "approve_url": approve_url,
            "confirm_code": code,
            "agent_user_id": user.id,
            "agent_user_created": created,
            "group_ids": [Command.set(groups.ids)],
            "message": False,
        })
        _logger.info("Melaya connect started for database %s", self.env.cr.dbname)
        return self._reopen()

    def action_open_approval(self):
        self.ensure_one()
        if not self.approve_url:
            return self._reopen()
        return {"type": "ir.actions.act_url", "url": self.approve_url, "target": "new"}

    def action_check(self):
        self.ensure_one()
        client = self.env["melaya.client"]
        wizard = self.sudo()
        if wizard.state != "waiting" or not wizard.connect_id:
            return self._reopen()
        # A lost answer can be retried: Melaya returns the token again for
        # 120 s to the same poll secret.
        data = client._connect_poll(wizard.connect_id, wizard.poll_secret)
        status = data.get("status")
        if status == "approved":
            token = data.get("install_token")
            if not isinstance(token, str) or not token:
                # Approved, but the token window is over: this Odoo never got it.
                wizard.write({
                    "state": "failed",
                    "connect_id": False,
                    "poll_secret": False,
                    "message": self.env._(
                        "Melaya approved the connection but this Odoo did not receive it in time. "
                        "Disconnect the database in Melaya (project > Connectors > Odoo) and connect again."
                    ),
                })
                _logger.info("Melaya connect approved without token for database %s", self.env.cr.dbname)
                return self._reopen()
            # Store the token first.
            client._store_connection(token, data.get("project"), data.get("project_url"))
            project = data.get("project")
            name = project.get("name") if isinstance(project, dict) else project
            wizard.write({
                "state": "done",
                "connect_id": False,
                "poll_secret": False,
                "message": self.env._("Connected to the Melaya project %(project)s.", project=name or self.env.cr.dbname),
            })
            _logger.info("Melaya connect approved for database %s", self.env.cr.dbname)
        elif status in ("rejected", "expired", "cancelled"):
            wizard._cleanup_agent()
            wizard.write({
                "state": "failed",
                "connect_id": False,
                "poll_secret": False,
                "message": (
                    self.env._("The connection was cancelled in Melaya. The Melaya Agent user and its API key were removed.")
                    if status != "expired"
                    else self.env._("The approval page expired. The Melaya Agent user and its API key were removed. Start again.")
                ),
            })
            _logger.info("Melaya connect %s for database %s", status, self.env.cr.dbname)
        else:
            wizard.message = self.env._("Still waiting. Approve the connection in Melaya, then click Check connection again.")
        return self._reopen()

    def action_cancel(self):
        self.ensure_one()
        self.env["melaya.client"]._check_admin()
        self._abandon()
        return {"type": "ir.actions.act_window_close"}

    def action_done(self):
        return {"type": "ir.actions.client", "tag": "reload"}
