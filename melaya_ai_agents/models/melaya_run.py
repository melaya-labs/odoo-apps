"""Runs started from Odoo, kept so Odoo can follow them without the panel.

Melaya keeps the runs; Odoo keeps only what it needs to remind the person who
started a run: the run id, the record it was started from, the pipeline, the
last status Melaya reported and the link to the run. A cron asks Melaya for
the status of unfinished runs (``runs/status``, as the person who started
them) and then, in Odoo only:

* schedules one To-Do activity "Approve in Melaya" for that person while the
  run waits for an approval, and marks it done when it no longer waits;
* posts one internal note on the record when the run is over.

Nothing about the record goes to Melaya: the cron sends run ids only.
"""
import logging
from datetime import timedelta

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import split_every

from ..lib import guards
from ..lib import run_status as rs
from .melaya_client import safe_url

_logger = logging.getLogger(__name__)

USER_GROUP = "melaya_ai_agents.group_melaya_user"
MAX_IDS_PER_CALL = 20  # runs/status takes 20 ids at most
CALLS_PER_USER = 2  # per cron run (each minute); Melaya allows 30 status calls per user and minute
MAX_CALLS = 40  # per cron run, all users together
FOLLOW_FOR = timedelta(hours=24)  # stop following a run after a day
FOLLOW_APPROVAL_FOR = timedelta(days=7)  # unless its approval reminder is still open
PLACEHOLDER_MATCH = timedelta(minutes=2)  # clock slack when a "starting" run shows up in the card
KEEP_FOR = timedelta(days=180)
SYSTRAY_LIMIT = 10
# Stop the whole cron run on these errors (Melaya or the connection is down).
STOP_CODES = (
    "timeout", "network", "builder_unavailable", "unavailable", "not_connected", "database_copy",
    "bad_token", "bad_api_url", "invalid_signature", "wrong_database", "stale_timestamp",
)


class MelayaRun(models.Model):
    _name = "melaya.run"
    _description = "Melaya run started from Odoo"
    _order = "id desc"
    _rec_name = "pipeline_name"

    run_id = fields.Char(string="Run id", readonly=True, index=True, copy=False)
    model = fields.Char(string="Model", required=True, readonly=True, index=True)
    res_id = fields.Many2oneReference(string="Record id", model_field="model", required=True, readonly=True, index=True)
    record_name = fields.Char(string="Record", readonly=True)
    user_id = fields.Many2one("res.users", string="Started by", required=True, readonly=True, index=True, ondelete="cascade")
    pipeline = fields.Char(string="Pipeline id", readonly=True)
    pipeline_name = fields.Char(string="Pipeline", readonly=True)
    status = fields.Char(string="Status in Melaya", readonly=True)
    state = fields.Selection(
        [
            (rs.RUNNING, "In progress"),
            (rs.WAITING, "Approval needed"),
            (rs.DONE, "Finished"),
            (rs.STALE, "No longer followed"),
        ],
        string="State",
        default=rs.RUNNING,
        required=True,
        readonly=True,
        index=True,
    )
    url = fields.Char(string="Run in Melaya", readonly=True)
    activity_id = fields.Many2one("mail.activity", string="Approval reminder", readonly=True, ondelete="set null")
    approval_notified = fields.Boolean(string="Approval reminder sent", readonly=True)
    result_posted = fields.Boolean(string="Result posted", readonly=True)
    started_at = fields.Datetime(string="Started", default=fields.Datetime.now, required=True, readonly=True)
    last_checked_at = fields.Datetime(string="Last checked", readonly=True)
    finished_at = fields.Datetime(string="Finished", readonly=True)

    _run_id_uniq = models.Constraint("unique(run_id)", "This Melaya run is already recorded.")

    # ------------------------------------------------------------------
    # Written by the panel routes (as the clicking user, after their checks)
    # ------------------------------------------------------------------
    @api.model
    def _pipeline_display_name(self, pipeline):
        """Display name from the pipelines last synced from Melaya, else the id."""
        for item in self.env["melaya.exposed.model"]._available_pipelines():
            if item["id"] == pipeline:
                return item["name"][:200]
        return pipeline

    @api.model
    def _record_start(self, record, pipeline, run_id=None, status=None, url=None):
        """Remember a run the current user started on ``record`` (the caller
        checked the record). ``run_id`` is empty when Melaya answered
        "starting": the run is matched later, when it shows up in the card.
        Never fails the caller: the run already started in Melaya."""
        vals = {
            "run_id": run_id or False,
            "model": record._name,
            "res_id": record.id,
            "record_name": (record.display_name or "")[:200],
            "user_id": self.env.uid,
            "pipeline": pipeline,
            "pipeline_name": self._pipeline_display_name(pipeline),
            "status": rs.clean_status(status, "started" if run_id else "starting"),
            "url": safe_url(url) or False,
        }
        vals["state"] = rs.state_for(vals["status"])
        try:
            with self.env.cr.savepoint():
                return self.sudo().create(vals)
        except Exception:  # noqa: BLE001 (local bookkeeping only)
            _logger.warning("Melaya run could not be recorded in Odoo for model %s", record._name)
            return self.browse()

    def _apply_remote(self, item):
        """Update from what Melaya reported (card or runs/status). Finished and
        no longer followed runs are final."""
        self.ensure_one()
        name = item.get("name")
        name = name.strip()[:200] if isinstance(name, str) and name.strip() else None
        if self.state in (rs.DONE, rs.STALE):
            # Final: only the display name Melaya reports may still change.
            if name and name != self.pipeline_name:
                self.write({"pipeline_name": name})
            return
        status = rs.clean_status(item.get("status"))
        vals = {}
        if status in rs.NO_NEWS and self.status and self.status not in rs.NO_NEWS:
            status = self.status  # Melaya did not find a status this time: keep the last one
        if status != self.status:
            vals["status"] = status
            vals["state"] = rs.state_for(status)
        url = safe_url(item.get("url"))
        if url and url != self.url:
            vals["url"] = url
        if name and name != self.pipeline_name:
            vals["pipeline_name"] = name
        if vals:
            self.write(vals)

    @api.model
    def _sync_from_panel(self, items, record=None):
        """Statuses the panel just got from Melaya for the current user's runs
        (no extra call). With ``record`` (the card), a run Melaya started
        after answering "starting" is matched to its placeholder."""
        by_id = {}
        for item in items or []:
            if isinstance(item, dict) and item.get("run_id"):
                by_id[str(item["run_id"])] = item
        if not by_id:
            return
        Runs = self.sudo()
        try:
            with self.env.cr.savepoint():
                for run in Runs.search([("run_id", "in", list(by_id)), ("user_id", "=", self.env.uid)]):
                    run._apply_remote(by_id.pop(run.run_id))
                if record is None or not by_id:
                    return
                taken = set(Runs.search([("run_id", "in", list(by_id))]).mapped("run_id"))
                fresh = [item for run_id, item in by_id.items() if run_id not in taken]
                fresh.sort(key=lambda i: str(i.get("started_at") or ""))
                for item in fresh:
                    placeholder = Runs.search([
                        ("user_id", "=", self.env.uid),
                        ("model", "=", record._name),
                        ("res_id", "=", record.id),
                        ("pipeline", "=", str(item.get("pipeline") or "")),
                        ("run_id", "=", False),
                        ("state", "=", rs.RUNNING),
                    ], order="id asc", limit=1)
                    if not placeholder:
                        continue
                    started = guards.parse_server_time(item.get("started_at"))
                    if started is not None:
                        started = started.replace(tzinfo=None)
                        if started < placeholder.create_date - PLACEHOLDER_MATCH:
                            continue
                    placeholder.write({"run_id": str(item["run_id"]), "status": False})
                    placeholder._apply_remote(item)
        except Exception:  # noqa: BLE001 (local bookkeeping only)
            _logger.warning("Melaya runs could not be updated in Odoo")

    # ------------------------------------------------------------------
    # Systray (as the clicking user: the record rules show their own runs)
    # ------------------------------------------------------------------
    @api.model
    def _systray_data(self):
        if not self.env.user.has_group(USER_GROUP):
            raise AccessError(self.env._("Ask your administrator to add you to the Melaya / User group."))
        domain = [("user_id", "=", self.env.uid)]
        runs = self.search(domain, limit=SYSTRAY_LIMIT, order="id desc")
        return {
            "runs": [
                {
                    "id": run.id,
                    "model": run.model,
                    "res_id": run.res_id,
                    "record_name": run.record_name or "",
                    "pipeline_name": run.pipeline_name or run.pipeline or "",
                    "status": run.status or "",
                    "state": run.state,
                    "url": safe_url(run.url) or "",
                    "started_at": fields.Datetime.to_string(run.started_at) if run.started_at else "",
                }
                for run in runs
            ],
            "approval_count": self.search_count(domain + [("state", "=", rs.WAITING)]),
        }

    # ------------------------------------------------------------------
    # Cron
    # ------------------------------------------------------------------
    @api.model
    def _cron_follow_runs(self):
        self._follow_runs(commit=True)

    @api.model
    def _follow_runs(self, commit=False):
        """Ask Melaya for the status of unfinished runs, then remind and report
        in Odoo. Uses sudo to read the runs and to write activities and notes;
        Melaya is called as each run's requester."""
        Runs = self.sudo()
        now = fields.Datetime.now()
        Runs._stop_following_old(now)
        if self.env["melaya.client"]._is_connected():
            Runs._poll_statuses(now, commit=commit)
        Runs._apply_effects(commit=commit)

    def _stop_following_old(self, now):
        """No more polling after a day (a week while the approval reminder is
        open); no note is posted for these runs."""
        old = self.search([
            ("state", "in", (rs.RUNNING, rs.WAITING)),
            ("started_at", "<", now - FOLLOW_FOR),
        ])
        for run in old:
            reminder = run.activity_id
            if reminder.exists() and reminder.active and run.started_at >= now - FOLLOW_APPROVAL_FOR:
                continue
            if reminder.exists():
                reminder.unlink()
            run.write({"state": rs.STALE, "activity_id": False})

    def _poll_statuses(self, now, commit=False):
        runs = self.search(
            [("state", "in", (rs.RUNNING, rs.WAITING)), ("run_id", "!=", False)],
            order="last_checked_at asc nulls first, id asc",
        )
        client = self.env["melaya.client"]
        calls = 0
        for user, user_runs in runs.grouped("user_id").items():
            if calls >= MAX_CALLS:
                break
            if not user.active:
                continue
            for batch in list(split_every(MAX_IDS_PER_CALL, user_runs.ids, self.browse))[:CALLS_PER_USER]:
                if calls >= MAX_CALLS:
                    break
                calls += 1
                batch.write({"last_checked_at": now})
                try:
                    data = client.with_user(user)._runs_status(batch.mapped("run_id"))
                except UserError as exc:
                    code = getattr(exc, "code", None) or "error"
                    _logger.warning("Melaya run status check failed: %s", code)
                    if code in STOP_CODES or (getattr(exc, "status", None) or 0) >= 500:
                        return
                    break  # this user (not linked, rate limited): try again next minute
                by_id = {
                    str(item["run_id"]): item
                    for item in (data.get("runs") or [])
                    if isinstance(item, dict) and item.get("run_id")
                }
                for run in batch:
                    if run.run_id in by_id:
                        run._apply_remote(by_id[run.run_id])
                if commit:
                    self.env["ir.cron"]._commit_progress(len(batch))

    def _apply_effects(self, commit=False):
        runs = self.search([
            "|", "|",
            "&", ("state", "=", rs.WAITING), ("approval_notified", "=", False),
            "&", ("state", "!=", rs.WAITING), ("activity_id", "!=", False),
            "&", ("state", "=", rs.DONE), ("result_posted", "=", False),
        ], order="id asc")
        for run in runs:
            try:
                with self.env.cr.savepoint():
                    run._apply_effect()
            except Exception:  # noqa: BLE001 (one record must not block the others)
                _logger.warning("Melaya run %s: the reminder or the result note failed", run.id)
                # Tried once: never retried every minute, never posted twice.
                with self.env.cr.savepoint():
                    run.write({
                        "approval_notified": run.approval_notified or run.state == rs.WAITING,
                        "result_posted": run.result_posted or run.state == rs.DONE,
                        "activity_id": run.activity_id.id if run.state == rs.WAITING else False,
                    })
            if commit:
                self.env["ir.cron"]._commit_progress(1)

    def _target_record(self):
        self.ensure_one()
        if not self.model or self.model not in self.env or not self.res_id:
            return None
        record = self.env[self.model].sudo().browse(self.res_id).exists()
        return record or None

    def _apply_effect(self):
        self.ensure_one()
        record = self._target_record()
        reminder = self.activity_id
        if self.state != rs.WAITING and reminder:
            if record and reminder.exists() and reminder.active:
                reminder.action_feedback()
            elif reminder.exists():
                reminder.unlink()
            self.write({"activity_id": False, "approval_notified": False})
        if self.state == rs.WAITING and not self.approval_notified:
            activity = self._schedule_reminder(record) if record else False
            self.write({"approval_notified": True, "activity_id": activity.id if activity else False})
        if self.state == rs.DONE and not self.result_posted:
            # Marked first: a failure below rolls this back with the note.
            self.write({"result_posted": True, "finished_at": fields.Datetime.now()})
            if record:
                self._post_result(record)

    def _env_for_requester(self):
        lang = self.user_id.lang or self.env.lang
        return self.with_context(lang=lang).env

    def _run_label(self, env):
        return self.pipeline_name or self.pipeline or env._("Melaya run")

    def _run_link(self, env):
        url = safe_url(self.url)
        if not url:
            return Markup("")
        return Markup(' <a href="%s" target="_blank" rel="noopener noreferrer">%s</a>') % (
            url, env._("Open the run in Melaya"),
        )

    def _schedule_reminder(self, record):
        """One To-Do for the requester, when the record supports activities
        and the requester can still read it."""
        user = self.user_id
        if not user.active or not hasattr(record, "activity_schedule"):
            return False
        if not record.with_user(user).has_access("read"):
            return False
        env = self._env_for_requester()
        text = env._("%(name)s is waiting for your approval in Melaya.", name=self._run_label(env))
        note = Markup("<p>%s%s</p>") % (text, self._run_link(env))
        return record.sudo().activity_schedule(
            "mail.mail_activity_data_todo",
            summary=env._("Approve in Melaya"),
            note=note,
            user_id=user.id,
        )

    def _post_result(self, record):
        """One internal note (never shown to customers) by the Melaya Agent."""
        if not hasattr(record, "message_post"):
            return False
        env = self._env_for_requester()
        name = self._run_label(env)
        kind = rs.outcome(self.status)
        if kind == "failed":
            text = env._("%(name)s failed.", name=name)
        elif kind == "canceled":
            text = env._("%(name)s was canceled.", name=name)
        else:
            text = env._("%(name)s completed.", name=name)
        body = Markup("<p>%s%s</p>") % (text, self._run_link(env))
        agent = self.env["melaya.client"]._agent_user()
        author = agent.partner_id or self.env.ref("base.partner_root", raise_if_not_found=False)
        return record.sudo().with_context(mail_create_nosubscribe=True).message_post(
            body=body,
            message_type="comment",
            subtype_xmlid="mail.mt_note",
            author_id=author.id if author else None,
        )

    @api.autovacuum
    def _gc_old_runs(self):
        """Forget finished and no longer followed runs after six months."""
        limit = fields.Datetime.now() - KEEP_FOR
        self.sudo().search([("state", "in", (rs.DONE, rs.STALE)), ("started_at", "<", limit)]).unlink()
