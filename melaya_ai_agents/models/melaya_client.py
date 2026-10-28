"""HTTP client for the Melaya Odoo app endpoints.

Every method is private (leading underscore) so it can never be called from
the browser through /web/dataset/call_kw. The install token is read with sudo
here and nowhere else; it is never returned, logged or sent to the browser.
Logs carry the endpoint and the HTTP status only (no token, key or body).
"""
import logging
import threading
from datetime import datetime, timezone
from http.cookiejar import DefaultCookiePolicy
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

from odoo import api, models, release
from odoo.api import SUPERUSER_ID
from odoo.exceptions import AccessError, UserError

from ..lib import guards, signing

_logger = logging.getLogger(__name__)

PARAM_PREFIX = "melaya_ai_agents."
P_API_URL = PARAM_PREFIX + "api_url"
P_TOKEN = PARAM_PREFIX + "install_token"
P_TOKEN_DB = PARAM_PREFIX + "token_database_uuid"
P_PROJECT_NAME = PARAM_PREFIX + "project_name"
P_PROJECT_URL = PARAM_PREFIX + "project_url"
P_AUTO_JOIN = PARAM_PREFIX + "auto_join"
P_AGENT_GROUPS = PARAM_PREFIX + "agent_group_ids"
P_AGENT_USER = PARAM_PREFIX + "agent_user_id"
P_PIPELINES = PARAM_PREFIX + "available_pipelines"
P_LAST_ERROR = PARAM_PREFIX + "last_error"
# Cleared on disconnect (and by data/neutralize.sql on a neutralized copy).
CONNECTION_PARAMS = (P_TOKEN, P_TOKEN_DB, P_PROJECT_NAME, P_PROJECT_URL, P_PIPELINES, P_LAST_ERROR)

DEFAULT_API_URL = "https://api.melaya.org"
APP_PREFIX = "/api/v1/odoo/app/"
# (connect, read) seconds.
READ_TIMEOUT = (3.05, 10)
RUN_TIMEOUT = (3.05, 30)
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
USER_AGENT = "melaya-odoo/%s (Odoo %s)" % (guards.addon_version(), release.version)

# One HTTP session (keep-alive pool) per worker thread; no cookies kept, so
# nothing can carry over from one Odoo user's call to another's.
_local = threading.local()
# The last error is written at most once per minute per database while it stays the same.
_error_throttle = guards.ErrorThrottle(60.0)


def _http_session():
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
        adapter = HTTPAdapter(pool_connections=2, pool_maxsize=4, max_retries=0)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _local.session = session
    return session


class MelayaError(UserError):
    """A Melaya call failed. ``message`` is safe to show to the user."""

    def __init__(self, message, code=None, status=None, details=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}


def safe_url(url):
    """Return ``url`` when it is an absolute http(s) URL, else ``False``."""
    if not isinstance(url, str):
        return False
    parsed = urlparse(url.strip())
    if parsed.scheme == "https" and parsed.netloc:
        return url.strip()
    if parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS:
        return url.strip()
    return False


class MelayaClient(models.AbstractModel):
    _name = "melaya.client"
    _description = "Melaya API client"

    # ------------------------------------------------------------------
    # Parameters (ir.config_parameter, read and written with sudo)
    # ------------------------------------------------------------------
    @api.model
    def _param_get(self, key, default=""):
        icp = self.env["ir.config_parameter"].sudo()
        getter = getattr(icp, "get_str", None)  # Odoo 20
        value = getter(key, "") if getter else icp.get_param(key, "")
        return value or default

    @api.model
    def _param_set(self, key, value):
        icp = self.env["ir.config_parameter"].sudo()
        value = None if value in (None, False, "") else str(value)
        setter = getattr(icp, "set_str", None)  # Odoo 20
        if setter:
            setter(key, value)
        else:
            icp.set_param(key, value or False)

    @api.model
    def _param_get_bool(self, key, default=False):
        icp = self.env["ir.config_parameter"].sudo()
        getter = getattr(icp, "get_bool", None)  # Odoo 20
        if getter:
            return getter(key, default)
        value = icp.get_param(key, None)
        return default if value in (None, False, "") else value in ("True", "true", "1")

    @api.model
    def _param_set_bool(self, key, value):
        icp = self.env["ir.config_parameter"].sudo()
        setter = getattr(icp, "set_bool", None)  # Odoo 20
        if setter:
            setter(key, bool(value))
        else:
            icp.set_param(key, "True" if value else "False")

    @api.model
    def _api_url(self):
        url = (self._param_get(P_API_URL) or DEFAULT_API_URL).strip().rstrip("/")
        parsed = urlparse(url)
        if not safe_url(url) or parsed.query or parsed.fragment:
            raise MelayaError(
                self.env._("The Melaya API URL must be an https:// address, for example https://api.melaya.org."),
                code="bad_api_url",
            )
        return url

    @api.model
    def _get_token(self):
        return self._param_get(P_TOKEN)

    @api.model
    def _database_uuid(self):
        return self._param_get("database.uuid")

    @api.model
    def _is_database_copy(self):
        """A token is stored but was issued for another database (a duplicate or a restored copy)."""
        if not self._get_token():
            return False
        return guards.is_database_copy(self._param_get(P_TOKEN_DB), self._database_uuid())

    @api.model
    def _is_connected(self):
        """Connected = a token is stored and it belongs to this database."""
        return bool(self._get_token()) and not self._is_database_copy()

    @api.model
    def _copy_message(self):
        return self.env._(
            "This database is a copy of the one connected to Melaya. Connect it again from Settings > Melaya."
        )

    @api.model
    def _agent_user(self):
        try:
            user_id = int(self._param_get(P_AGENT_USER) or 0)
        except ValueError:
            user_id = 0
        user = self.env["res.users"].sudo().with_context(active_test=False).browse(user_id).exists()
        return user

    # ------------------------------------------------------------------
    # Identity block
    # ------------------------------------------------------------------
    @api.model
    def _odoo_user(self, as_addon=False, identify=False):
        """``odoo_user`` of a signed call. Login and name are sent only to start
        a link (``identify``) and once the user's Melaya account is linked;
        before that, the Odoo user id only."""
        if as_addon:
            # id 0 means the addon itself (cron, uninstall), per the contract.
            return {
                "id": 0,
                "login": "melaya_ai_agents",
                "name": "Melaya AI Agents for Odoo",
                "is_admin": False,
                "is_manager": False,
                "is_user": False,
            }
        user = self.env.user
        block = {
            "id": user.id,
            # Empty until the user links (keys kept so the body shape never changes).
            "login": "",
            "name": "",
            "is_admin": user.has_group("base.group_system"),
            "is_manager": user.has_group("melaya_ai_agents.group_melaya_manager"),
            "is_user": user.has_group("melaya_ai_agents.group_melaya_user"),
        }
        if identify or self.env["melaya.user.link"]._is_linked(user):
            block["login"] = user.login or ""
            block["name"] = user.name or ""
        return block

    @api.model
    def _set_linked(self, linked):
        if self.env.user.id:
            self.env["melaya.user.link"]._set_linked(self.env.user, bool(linked))

    @api.model
    def _check_admin(self):
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(self.env._("Only Settings administrators can change the Melaya connection."))

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    @api.model
    def _post(self, path, payload, timeout=READ_TIMEOUT, token=None):
        url = self._api_url() + path
        body = signing.body_bytes(payload)
        if token:
            try:
                headers = signing.signed_headers(token, urlparse(url).path, body)
            except signing.InvalidInstallToken:
                raise MelayaError(
                    self.env._("The stored Melaya connection is damaged. Disconnect and connect again in Settings > Melaya."),
                    code="bad_token",
                ) from None
        else:
            headers = {"Content-Type": "application/json"}
        headers["Accept"] = "application/json"
        headers["User-Agent"] = USER_AGENT
        try:
            response = _http_session().post(url, data=body, headers=headers, timeout=timeout, allow_redirects=False)
        except requests.Timeout:
            self._record_error(path, None, "timeout")
            raise MelayaError(self.env._("Melaya did not answer in time. Try again in a moment."), code="timeout") from None
        except requests.RequestException as exc:
            self._record_error(path, None, type(exc).__name__)
            raise MelayaError(
                self.env._("Melaya could not be reached. Check that this Odoo server can open %(url)s.", url=self._api_url()),
                code="network",
            ) from None
        _logger.info("Melaya call %s answered HTTP %s", path, response.status_code)
        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {"result": data}
        if response.status_code >= 300:
            code = data.get("error") or data.get("code")
            if not isinstance(code, str):
                code = None
            self._record_error(path, response.status_code, code)
            raise self._error_for(response.status_code, code, data)
        return data

    @api.model
    def _clock_message(self, server_time):
        offset = guards.clock_offset_seconds(server_time)
        if offset is None:
            return None
        return self.env._(
            "Your Odoo server clock is off by about %(minutes)s minutes. Fix the server time and try again.",
            minutes=guards.clock_offset_minutes(offset),
        )

    @api.model
    def _error_for(self, status, code, data):
        """Translate a Melaya error answer into a message for the user.
        Known error codes first (whatever the HTTP status), then the server's
        own short message, then a generic message per status."""
        _ = self.env._
        server_message = data.get("message")
        if not (isinstance(server_message, str) and 0 < len(server_message) <= 300):
            server_message = None
        names = data.get("pipelines") if isinstance(data.get("pipelines"), list) else []
        names = ", ".join(str(n.get("name") or n.get("id")) if isinstance(n, dict) else str(n) for n in names) or "?"
        unavailable = _("Melaya is unavailable right now. Try again in a minute.")
        refused = _(
            "Melaya refused the request. The connection may have been revoked, or this server's clock is wrong. "
            "An administrator can check Settings > Melaya > Diagnostics."
        )
        message = None
        if code == "seat_full":
            owner = data.get("owner_name") or data.get("owner") or _("the project owner")
            if isinstance(owner, dict):
                owner = owner.get("name") or _("the project owner")
            message = _(
                "Your Melaya workspace for %(db)s is full. Ask %(owner)s to add seats.",
                db=self.env.cr.dbname,
                owner=owner,
            )
        elif code == "wrong_database":
            message = self._copy_message()
        elif code == "stale_timestamp":
            message = self._clock_message(data.get("server_time")) or refused
        elif code == "invalid_signature":
            message = refused
        elif code == "replayed":
            message = _("Melaya refused a repeated request. Try again.")
        elif code == "rate_limited":
            message = server_message or _("Too many requests to Melaya. Wait a minute and try again.")
        elif code == "not_linked":
            message = _("Link your Melaya account first.")
        elif code == "viewer" or (code == "forbidden" and data.get("reason") == "viewer"):
            message = _("Your role in the Melaya project is Viewer. Ask the project owner for the Runner role to run pipelines.")
        elif code == "model_not_exposed":
            message = _("This kind of record is not exposed in Melaya. Ask your administrator to click Save to Melaya in Exposed models.")
        elif code == "pipeline_not_found":
            message = _("That pipeline is not available on this record.")
        elif code == "project_connectors_required":
            message = _(
                "This pipeline uses personal connected accounts. Ask the project owner to switch it to the project's connected accounts."
            )
        elif code == "personal_pipelines":
            message = _(
                "These pipelines use personal connected accounts: %(names)s. Switch them to the project's connected accounts in Melaya, then save again.",
                names=names,
            )
        elif code == "builder_unavailable":
            message = unavailable
        elif code == "unknown_pipelines":
            message = _(
                "These pipelines do not exist in the Melaya project: %(names)s. Click Sync from Melaya to see the available ones.",
                names=names,
            )
        elif code == "template_not_found":
            message = _("This template is no longer available. Close this window and open Add from templates again.")
        elif code == "empty_list":
            message = _("Melaya refused an empty member list. Nothing was changed.")
        elif code == "start_timeout":
            message = _("Melaya is taking longer than usual to start the run. Check Melaya in a minute.")
        elif code == "base_url_not_allowed":
            message = _("Melaya can only connect to a public https Odoo address.")
        elif code == "invalid_payload" and server_message:
            message = server_message
        elif code in ("invalid_payload", "invalid_record", "invalid_pipeline", "invalid_odoo_user", "invalid_json"):
            message = _("Melaya refused the data sent by Odoo (%(code)s). Update this module, or contact Melaya support.", code=code)
        elif server_message:
            message = server_message
        elif status == 401:
            message = refused
        elif status == 403:
            message = _("Your role in the Melaya project does not allow this action.")
        elif status == 404:
            message = _("Melaya could not find what this request refers to.")
        elif status == 429:
            message = _("Too many requests to Melaya. Wait a minute and try again.")
        elif status == 503:
            message = unavailable
        elif status >= 500:
            message = _("Melaya had a problem with this request. Try again in a moment.")
        else:
            message = _("Melaya refused the request (HTTP %(status)s).", status=status)
        return MelayaError(message, code=code, status=status, details=data)

    @api.model
    def _record_error(self, endpoint, status, code):
        """Keep the last failure for Diagnostics. Written in its own transaction
        (the caller's is rolled back by the error), and only when the error
        changed or the stored one is older than a minute. Never stores bodies
        or secrets."""
        if not _error_throttle.should_record(self.env.cr.dbname, (endpoint, status, code)):
            return
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        text = "%s %s HTTP %s %s" % (stamp, endpoint, status or "-", code or "")
        from odoo.modules import module as odoo_module
        if odoo_module.current_test:
            # Under the test runner a separate cursor would COMMIT a simulated
            # failure into the real database (it then showed in Diagnostics).
            self._param_set(P_LAST_ERROR, text.strip())
            return
        try:
            with self.env.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                env["melaya.client"]._param_set(P_LAST_ERROR, text.strip())
        except Exception:  # noqa: BLE001 (diagnostics only, never block the caller)
            _logger.debug("Could not record the last Melaya error", exc_info=True)

    @api.model
    def _signed(self, endpoint, payload=None, timeout=READ_TIMEOUT, as_addon=False, identify=False):
        token = self._get_token()
        if not token:
            raise MelayaError(
                self.env._("This database is not connected to Melaya yet. An administrator can connect it in Settings > Melaya."),
                code="not_connected",
            )
        current_db = self._database_uuid()
        token_db = self._param_get(P_TOKEN_DB)
        if guards.is_database_copy(token_db, current_db):
            # The token belongs to another database: never send it.
            raise MelayaError(self._copy_message(), code="database_copy")
        if not token_db and current_db:
            self._param_set(P_TOKEN_DB, current_db)  # connection made before this check existed
        body = dict(payload or {})
        body["database_uuid"] = current_db
        body["odoo_user"] = self._odoo_user(as_addon=as_addon, identify=identify)
        try:
            return self._post(APP_PREFIX + endpoint, body, timeout=timeout, token=token)
        except MelayaError as exc:
            if exc.code == "not_linked" and not as_addon:
                self._set_linked(False)
            raise

    # ------------------------------------------------------------------
    # Connect flow (unsigned)
    # ------------------------------------------------------------------
    @api.model
    def _connect_start(self, payload):
        self._check_admin()
        return self._post(APP_PREFIX + "connect/start", payload, timeout=RUN_TIMEOUT)

    @api.model
    def _connect_poll(self, connect_id, poll_secret):
        self._check_admin()
        return self._post(
            APP_PREFIX + "connect/poll",
            {"connect_id": connect_id, "poll_secret": poll_secret},
            timeout=READ_TIMEOUT,
        )

    @api.model
    def _connect_cancel(self, connect_id, poll_secret):
        """Withdraw an unfinished connect request. The poll secret is the
        proof; also called from the transient-record cleanup (no admin user)."""
        return self._post(
            APP_PREFIX + "connect/cancel",
            {"connect_id": connect_id, "poll_secret": poll_secret},
            timeout=READ_TIMEOUT,
        )

    @api.model
    def _store_connection(self, install_token, project=None, project_url=None):
        signing.parse_install_token(install_token)  # refuse a malformed token early
        self._param_set(P_TOKEN, install_token)
        self._param_set(P_TOKEN_DB, self._database_uuid())
        name = project.get("name") if isinstance(project, dict) else project
        url = project_url or (project.get("url") if isinstance(project, dict) else None)
        self._param_set(P_PROJECT_NAME, name if isinstance(name, str) else "")
        self._param_set(P_PROJECT_URL, safe_url(url) or "")
        self._param_set_bool(P_AUTO_JOIN, True)  # Melaya creates the install with auto_join = true

    @api.model
    def _clear_connection(self):
        for key in CONNECTION_PARAMS:
            self._param_set(key, None)
        self.env["melaya.user.link"].sudo().search([]).unlink()

    @api.model
    def _delete_all_params(self):
        """Uninstall: delete every melaya_ai_agents.* system parameter."""
        params = self.env["ir.config_parameter"].sudo().search([("key", "=like", PARAM_PREFIX + "%")])
        params.filtered(lambda p: p.key.startswith(PARAM_PREFIX)).unlink()

    @api.model
    def _revoke_agent_keys(self):
        """Delete every API key of the Melaya Agent user (best effort)."""
        user = self._agent_user()
        if not user:
            return 0
        keys = self.env["res.users.apikeys"].sudo().search([("user_id", "=", user.id)])
        count = len(keys)
        if keys:
            keys._remove()
        return count

    @api.model
    def _archive_agent(self):
        """Archive the Melaya Agent user (after its keys are deleted). Connecting again reactivates it."""
        user = self._agent_user()
        if user and user.active and user != self.env.user:
            user.sudo().active = False

    # ------------------------------------------------------------------
    # Signed endpoints
    # ------------------------------------------------------------------
    @api.model
    def _status(self):
        return self._signed("status")

    @api.model
    def _link_start(self):
        return self._signed("link/start", identify=True)

    @api.model
    def _unlink(self, target_user_id=None):
        payload = {}
        if target_user_id:
            self._check_admin()
            payload["target_user_id"] = int(target_user_id)
        data = self._signed("unlink", payload)
        if not target_user_id:
            self._set_linked(False)
        return data

    @api.model
    def _card(self, model, record_id):
        data = self._signed("card", {"model": model, "record_id": int(record_id)})
        self._set_linked(bool(data.get("linked")))
        return data

    @api.model
    def _ask_start(self, model, record_id):
        """A single-use Ask Melaya link (10 minutes) for this record and the
        current user; the caller has checked the user's read access."""
        return self._signed("ask/start", {"model": model, "record_id": int(record_id)})

    @api.model
    def _run(self, model, record_id, pipeline, brief):
        return self._signed(
            "run",
            {"model": model, "record_id": int(record_id), "pipeline": pipeline, "brief": brief or ""},
            timeout=RUN_TIMEOUT,
        )

    @api.model
    def _runs_status(self, run_ids):
        return self._signed("runs/status", {"run_ids": [str(r) for r in run_ids][:20]})

    @api.model
    def _settings_get(self):
        self._check_admin()
        return self._signed("settings/get")

    @api.model
    def _settings_set(self, auto_join, exposed):
        self._check_admin()
        return self._signed("settings/set", {"auto_join": bool(auto_join), "exposed": exposed}, timeout=RUN_TIMEOUT)

    @api.model
    def _templates_list(self):
        """Odoo templates this install can add (admin only)."""
        self._check_admin()
        return self._signed("templates/list")

    @api.model
    def _templates_install(self, template_id):
        """Create one template's pipeline in the project, or return the one already there (admin only)."""
        self._check_admin()
        return self._signed("templates/install", {"template_id": str(template_id)}, timeout=RUN_TIMEOUT)

    @api.model
    def _members_sync(self, active_user_ids):
        ids = sorted(set(active_user_ids))
        payload = {"active_user_ids": ids}
        if not ids:
            # Only reached when the Melaya / User group exists and has no active user.
            payload["allow_empty"] = True
        return self._signed("members/sync", payload, as_addon=True)

    @api.model
    def _token_rotate(self):
        self._check_admin()
        data = self._signed("token/rotate", timeout=RUN_TIMEOUT)
        token = data.get("install_token")
        if not isinstance(token, str):
            raise MelayaError(self.env._("Melaya did not return a new token. Nothing was changed."), code="bad_answer")
        signing.parse_install_token(token)
        # Store at once: the previous token keeps working for 10 minutes on Melaya's side.
        self._param_set(P_TOKEN, token)
        return True

    @api.model
    def _disconnect(self, reason="admin", as_addon=False):
        if not as_addon:
            self._check_admin()
        data = self._signed("disconnect", {"reason": reason}, timeout=RUN_TIMEOUT, as_addon=as_addon)
        self._clear_connection()
        return data

    @api.model
    def _diagnostics(self):
        self._check_admin()
        return self._signed("diagnostics")

    # ------------------------------------------------------------------
    # Cron
    # ------------------------------------------------------------------
    @api.model
    def _melaya_user_ids(self):
        """Active members of Melaya / User (the Agent excluded), or None when
        the group cannot be resolved (then nothing is synced)."""
        group = self.env.ref("melaya_ai_agents.group_melaya_user", raise_if_not_found=False)
        if not group:
            return None
        agent = self._agent_user()
        users = group.sudo().all_user_ids.filtered(lambda u: u.active and u != agent)
        return users.ids

    @api.model
    def _cron_members_sync(self):
        if not self._is_connected():
            return
        user_ids = self._melaya_user_ids()
        if user_ids is None:
            _logger.warning("Melaya members sync skipped: the Melaya / User group was not found")
            return
        try:
            self._members_sync(user_ids)
        except UserError as exc:
            _logger.warning("Melaya members sync failed: %s", getattr(exc, "code", None) or "error")
