"""JSON-RPC routes used by the Melaya record panel, the chatter button and
the top bar menu.

The browser only talks to these routes; they call melaya.client server-side,
so the install token never reaches the browser. Every record route re-checks,
as the logged-in user, the Melaya / User group, that the model is exposed and
that the user can read the record.
"""
import re

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

from ..models.melaya_client import MelayaError, safe_url

MAX_BRIEF = 6000  # same limit as Melaya
MAX_RUN_IDS = 20
PIPELINE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")  # pipeline id, as Melaya checks it
ROLES = ("owner", "editor", "runner", "viewer")
REASONS = ("not_linked", "seat_full", "viewer")


def _message(exc):
    return exc.args[0] if exc.args else str(exc)


def _clean_run(run):
    if not isinstance(run, dict) or not run.get("run_id"):
        return None
    return {
        "run_id": str(run.get("run_id")),
        "pipeline": str(run.get("pipeline") or ""),
        "name": str(run.get("name") or run.get("pipeline") or ""),
        "status": str(run.get("status") or ""),
        "started_at": str(run.get("started_at") or ""),
        "url": safe_url(run.get("url")) or "",
    }


class MelayaPanelController(http.Controller):

    def _call(self, func):
        try:
            result = func()
        except MelayaError as exc:
            return {"ok": False, "error": _message(exc), "code": exc.code or "", "seat_full": exc.code == "seat_full"}
        except (AccessError, UserError) as exc:
            return {"ok": False, "error": _message(exc), "code": "denied"}
        result["ok"] = True
        return result

    def _reason_message(self, reason, owner_name):
        _ = request.env._
        if reason == "viewer":
            return _("Your role in the Melaya project is Viewer. Ask the project owner for the Runner role to run pipelines.")
        if reason == "seat_full":
            return _(
                "Your Melaya workspace for %(db)s is full. Ask %(owner)s to add seats.",
                db=request.env.cr.dbname,
                owner=owner_name or _("the project owner"),
            )
        return ""

    def _check(self, model, record_id):
        return request.env["melaya.record.panel"]._check_record(model, record_id)

    @http.route("/melaya_ai_agents/panel/card", type="jsonrpc", auth="user", methods=["POST"])
    def card(self, model, record_id, **kwargs):
        def run():
            record, _exposed = self._check(model, record_id)
            data = request.env["melaya.client"]._card(model, record.id)
            pipelines = [
                {"id": str(p.get("id") or p.get("name")), "name": p.get("name")}
                for p in data.get("pipelines") or []
                if isinstance(p, dict) and isinstance(p.get("name"), str)
            ]
            runs = [r for r in (_clean_run(x) for x in data.get("runs") or []) if r]
            request.env["melaya.exposed.model"]._remember_pipeline_names(pipelines)
            # Keep Odoo's copy of the user's runs current; a run Melaya
            # started after a "starting" answer is picked up here.
            request.env["melaya.run"]._sync_from_panel(runs, record=record)
            role = data.get("role") if data.get("role") in ROLES else None
            reason = data.get("reason") if data.get("reason") in REASONS else None
            seat_full = reason == "seat_full" or bool(data.get("seat_full"))
            if seat_full and not reason:
                reason = "seat_full"
            owner_name = data.get("owner_name") if isinstance(data.get("owner_name"), str) else ""
            owner_name = owner_name.strip()[:120]
            return {
                "linked": bool(data.get("linked")),
                "role": role,
                "reason": reason,
                "owner_name": owner_name,
                "seat_full": seat_full,
                "reason_message": self._reason_message(reason, owner_name),
                "can_run": bool(data.get("linked")) and reason not in ("viewer", "seat_full") and role != "viewer",
                "can_ask": data.get("can_ask") is True,
                "pipelines": pipelines,
                "runs": runs,
                "record_name": record.display_name or "",
                "can_add_templates": request.env["melaya.record.panel"]._empty_state(model)["can_add_templates"],
            }
        return self._call(run)

    @http.route("/melaya_ai_agents/panel/ask", type="jsonrpc", auth="user", methods=["POST"])
    def ask(self, model, record_id, **kwargs):
        def run():
            # check_access('read') as the clicking user BEFORE Melaya mints the
            # single-use link: it is the only proof Melaya gets that this user
            # may see this record (the shared Melaya Agent may read more).
            record, _exposed = self._check(model, record_id)
            data = request.env["melaya.client"]._ask_start(model, record.id)
            url = safe_url(data.get("url"))
            if not url:
                raise UserError(request.env._("Melaya did not return an Ask Melaya link. Try again."))
            return {"url": url}
        return self._call(run)

    @http.route("/melaya_ai_agents/panel/link_start", type="jsonrpc", auth="user", methods=["POST"])
    def link_start(self, model, record_id, **kwargs):
        def run():
            self._check(model, record_id)
            data = request.env["melaya.client"]._link_start()
            url = safe_url(data.get("url"))
            if not url:
                raise UserError(request.env._("Melaya did not return a link page. Try again."))
            return {"url": url}
        return self._call(run)

    @http.route("/melaya_ai_agents/panel/unlink", type="jsonrpc", auth="user", methods=["POST"])
    def unlink(self, model, record_id, **kwargs):
        def run():
            self._check(model, record_id)
            data = request.env["melaya.client"]._unlink()
            return {"unlinked": bool(data.get("unlinked", True))}
        return self._call(run)

    @http.route("/melaya_ai_agents/panel/run", type="jsonrpc", auth="user", methods=["POST"])
    def run_pipeline(self, model, record_id, pipeline, brief="", pipeline_name="", **kwargs):
        def run():
            record, _exposed = self._check(model, record_id)
            if not isinstance(pipeline, str) or not PIPELINE_RE.match(pipeline.strip()):
                raise UserError(request.env._("Pick a pipeline."))
            text = brief if isinstance(brief, str) else ""
            if len(text) > MAX_BRIEF:
                raise UserError(request.env._("The brief is too long (%(max)s characters at most).", max=MAX_BRIEF))
            data = request.env["melaya.client"]._run(model, record.id, pipeline.strip(), text.strip())
            Runs = request.env["melaya.run"]
            if data.get("status") == "starting" and not data.get("run_id"):
                # HTTP 202: Melaya records the run once it starts; the panel refreshes the
                # card, which matches the run to this placeholder.
                Runs._record_start(record, pipeline.strip(), status="starting")
                return {"starting": True, "message": request.env._("Melaya is starting the run. It will appear here in a moment.")}
            started = _clean_run({
                "run_id": data.get("run_id"),
                "pipeline": pipeline.strip(),
                "name": pipeline_name.strip()[:200] if isinstance(pipeline_name, str) and pipeline_name.strip() else pipeline.strip(),
                "status": data.get("status") or "started",
                "url": data.get("url"),
            })
            if not started:
                raise UserError(request.env._("Melaya did not confirm the run. Check the project in Melaya."))
            Runs._record_start(record, pipeline.strip(), run_id=started["run_id"], status=started["status"], url=started["url"])
            return {"run": started}
        return self._call(run)

    @http.route("/melaya_ai_agents/panel/runs_status", type="jsonrpc", auth="user", methods=["POST"])
    def runs_status(self, model, record_id, run_ids, **kwargs):
        def run():
            self._check(model, record_id)
            ids = [str(r) for r in (run_ids or []) if isinstance(r, (str, int))][:MAX_RUN_IDS]
            if not ids:
                return {"runs": []}
            data = request.env["melaya.client"]._runs_status(ids)
            runs = []
            for item in data.get("runs") or []:
                if isinstance(item, dict) and item.get("run_id"):
                    runs.append({
                        "run_id": str(item["run_id"]),
                        "status": str(item.get("status") or ""),
                        "url": safe_url(item.get("url")) or "",
                    })
            request.env["melaya.run"]._sync_from_panel(runs)
            return {"runs": runs}
        return self._call(run)

    @http.route("/melaya_ai_agents/exposed_models", type="jsonrpc", auth="user", methods=["POST"])
    def exposed_models(self, **kwargs):
        """Models that show the Melaya button in the chatter for this user
        (empty unless connected and in Melaya / User). The web client asks
        once per page load."""
        return request.env["melaya.record.panel"]._ui_config()

    @http.route("/melaya_ai_agents/templates/list", type="jsonrpc", auth="user", methods=["POST"])
    def templates_list(self, model="", **kwargs):
        """Add from templates: the Odoo templates of Melaya (Settings administrators only)."""
        return self._call(lambda: request.env["melaya.template.picker"]._picker_data(model if isinstance(model, str) else ""))

    @http.route("/melaya_ai_agents/templates/add", type="jsonrpc", auth="user", methods=["POST"])
    def templates_add(self, template_ids=None, **kwargs):
        """Add from templates: create the ticked templates' pipelines and put them on the exposed models."""
        return self._call(lambda: request.env["melaya.template.picker"]._add_templates(template_ids or []))

    @http.route("/melaya_ai_agents/runs/recent", type="jsonrpc", auth="user", methods=["POST"])
    def recent_runs(self, **kwargs):
        """The user's own recent runs for the top bar menu (Odoo's copy, no call to Melaya)."""
        return self._call(lambda: request.env["melaya.run"]._systray_data())
