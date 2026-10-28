"""Add from templates: Melaya's Odoo templates turned into pipelines from Odoo.

The dialog (static/src/templates/) reads the templates with ``_picker_data``
and adds the ticked ones with ``_add_templates``, through the routes of
controllers/main.py. For each template Melaya creates the pipeline in the
project (``templates/install``, admin only, idempotent); Odoo then puts it on
the exposed models it works on (creating the row when the model is not exposed
yet) and saves the exposed models to Melaya, like Save to Melaya.
"""
import re

from odoo import api, models
from odoo.exceptions import UserError, ValidationError

from .melaya_client import MelayaError
from .melaya_exposed_model import BLOCKED_PREFIXES, MODEL_RE

TEMPLATE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
PIPELINE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
MAX_PER_ADD = 20
# Errors after which the next templates cannot be added either.
STOP_CODES = (
    "templates_unavailable", "not_connected", "database_copy", "bad_token", "bad_api_url", "timeout", "network",
    "invalid_signature", "wrong_database", "stale_timestamp", "builder_unavailable", "rate_limited",
)
# Connector ids of payload.odoo.connectors, as people know them. Others are
# shown with their id in title case. "odoo" is the project's own connection.
CONNECTOR_LABELS = {
    "gmail": "Gmail", "google": "Google", "google_sheets": "Google Sheets", "google_drive": "Google Drive",
    "google_calendar": "Google Calendar", "google_docs": "Google Docs", "google_meet": "Google Meet",
    "google_ads": "Google Ads", "google_analytics": "Google Analytics", "outlook": "Outlook",
    "microsoft": "Microsoft 365", "teams": "Microsoft Teams", "microsoft_teams": "Microsoft Teams",
    "slack": "Slack", "whatsapp": "WhatsApp", "hubspot": "HubSpot", "linkedin": "LinkedIn",
    "linkedin_api": "LinkedIn", "meta_ads": "Meta Ads", "github": "GitHub", "jira": "Jira", "linear": "Linear",
    "zoom": "Zoom", "zoho": "Zoho", "notion": "Notion", "stripe": "Stripe", "shopify": "Shopify",
    "telegram": "Telegram", "discord": "Discord", "smtp": "Email (SMTP)", "companies_house": "Companies House",
}


def connector_label(service):
    return CONNECTOR_LABELS.get(service) or service.replace("_", " ").title()


def _strings(value, limit=20):
    return [v.strip() for v in value if isinstance(v, str) and v.strip()][:limit] if isinstance(value, list) else []


class MelayaTemplatePicker(models.AbstractModel):
    _name = "melaya.template.picker"
    _description = "Melaya templates for Odoo"

    # ------------------------------------------------------------------
    # Opening the dialog
    # ------------------------------------------------------------------
    @api.model
    def action_open(self, model_name="", reload=True):
        """Client action of the Add from templates dialog, filtered to ``model_name`` when given."""
        self.env["melaya.client"]._check_admin()
        model_name = model_name if isinstance(model_name, str) and MODEL_RE.match(model_name or "") else ""
        return {
            "type": "ir.actions.client",
            "tag": "melaya_ai_agents.template_picker",
            "params": {"model": model_name, "reload": bool(reload)},
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @api.model
    def _model_ok(self, model_name):
        return (
            isinstance(model_name, str) and bool(MODEL_RE.match(model_name))
            and not model_name.startswith(BLOCKED_PREFIXES) and model_name in self.env
        )

    @api.model
    def _model_label(self, model_name):
        ir_model = self.env["ir.model"].sudo()._get(model_name) if model_name in self.env else None
        return (ir_model.name if ir_model else "") or model_name

    @api.model
    def _app_info(self, apps):
        """Template apps -> (labels people know, installed?). Melaya lists Odoo apps by
        their technical module name ("account", "hr_expense"): those are shown with
        the module's own name and checked for installation. Any other value is a
        display name already: shown as is, never blocks the template.
        installed? is None when no app is a known module (nothing to check)."""
        modules = {
            m.name: m for m in self.env["ir.module.module"].sudo().search([("name", "in", apps)])
        } if apps else {}
        labels = [modules[a].shortdesc or a if a in modules else a for a in apps]
        known = [modules[a] for a in apps if a in modules]
        installed = any(m.state == "installed" for m in known) if known else None
        return list(dict.fromkeys(labels)), installed

    @api.model
    def _exposed_pipelines(self):
        """Active exposed models -> pipeline ids they list (names resolved to ids)."""
        Exposed = self.env["melaya.exposed.model"]
        return {
            row.model: {Exposed._resolve_pipeline(p) for p in row._pipeline_list()}
            for row in Exposed.search([("button_active", "=", True)])
        }

    @api.model
    def _call_melaya(self, func, *args):
        """Call Melaya; a server that does not have the templates endpoints yet answers a clear message."""
        try:
            return func(*args)
        except MelayaError as exc:
            if exc.status == 404 and exc.code in (None, "", "not_found"):
                raise MelayaError(
                    self.env._("Templates are not available from Melaya yet. Try again later, or add pipelines by name in Exposed models."),
                    code="templates_unavailable",
                    status=404,
                ) from None
            raise

    # ------------------------------------------------------------------
    # The list shown in the dialog
    # ------------------------------------------------------------------
    @api.model
    def _picker_data(self, model_name=""):
        client = self.env["melaya.client"]
        client._check_admin()
        data = self._call_melaya(client._templates_list)
        exposed = self._exposed_pipelines()
        other = self.env._("Other")
        templates = []
        for item in data.get("templates") or []:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not TEMPLATE_ID_RE.match(item["id"]):
                continue
            models_ = [m for m in _strings(item.get("models")) if MODEL_RE.match(m)]
            usable = [m for m in models_ if self._model_ok(m)]
            pipeline = item.get("pipeline") if isinstance(item.get("pipeline"), str) else ""
            in_project = bool(item.get("installed")) and bool(pipeline)
            apps, apps_installed = self._app_info(_strings(item.get("apps")))
            categories = [c for c in _strings(item.get("categories")) if c.lower() != "odoo"]
            connectors = [connector_label(c) for c in _strings(item.get("connectors")) if c != "odoo"]
            # A template with no record model runs on a schedule or by hand in
            # Melaya: it needs its Odoo apps, not an exposed model.
            scheduled = not models_
            if scheduled:
                available = apps_installed is not False
                on_records = in_project
            else:
                available = bool(usable)
                on_records = in_project and bool(usable) and all(pipeline in exposed.get(m, ()) for m in usable)
            if not available:
                state = "unavailable"
                note = self.env._(
                    "Install the Odoo app it works on first (%(apps)s).",
                    apps=", ".join(apps) or ", ".join(models_) or "?",
                )
            elif on_records:
                state, note = "added", ""
            elif scheduled:
                state = "new"
                note = self.env._("Runs on a schedule or by hand in Melaya. It adds no button on records.")
            elif in_project:
                state = "in_project"
                note = self.env._("Already in the Melaya project. Tick it to show it on Odoo records.")
            else:
                state, note = "new", ""
            templates.append({
                "id": item["id"],
                "name": str(item.get("name") or item["id"])[:120],
                "summary": str(item.get("summary") or "")[:600],
                "how_to_use": str(item.get("how_to_use") or "")[:1500],
                "group": apps[0] if apps else (categories[0] if categories else other),
                "apps": ", ".join(apps),
                "enterprise": item.get("edition") == "enterprise",
                "connectors": ", ".join(connectors),
                "models": models_,
                "places": ", ".join(self._model_label(m) for m in usable),
                "state": state,
                "note": note,
            })
        templates.sort(key=lambda t: (t["group"] == other, t["group"].lower(), t["name"].lower()))
        project = data.get("project") if isinstance(data.get("project"), dict) else {}
        model_name = model_name if self._model_ok(model_name) else ""
        return {
            "templates": templates,
            "model": model_name,
            "model_label": self._model_label(model_name) if model_name else "",
            "project_name": str(project.get("name") or ""),
        }

    # ------------------------------------------------------------------
    # Adding the ticked templates
    # ------------------------------------------------------------------
    @api.model
    def _expose(self, pipeline, model_names):
        """Put ``pipeline`` on the exposed rows of ``model_names`` (creating the
        rows, Melaya action shown). Returns (labels of the models it is on, models skipped)."""
        Exposed = self.env["melaya.exposed.model"]
        IrModel = self.env["ir.model"].sudo()
        places, skipped = [], []
        for model_name in model_names:
            if not self._model_ok(model_name):
                skipped.append(model_name)
                continue
            ir_model = IrModel._get(model_name)
            row = Exposed.search([("model_id", "=", ir_model.id)], limit=1)
            try:
                with self.env.cr.savepoint():
                    if row:
                        names = row._pipeline_list()
                        if pipeline not in {Exposed._resolve_pipeline(p) for p in names}:
                            names.append(pipeline)
                        vals = {"pipeline_names": "\n".join(names)}
                        if not row.button_active:
                            vals["button_active"] = True
                        row.write(vals)
                    else:
                        Exposed.create({"model_id": ir_model.id, "pipeline_names": pipeline, "button_active": True})
            except ValidationError:
                skipped.append(model_name)
                continue
            places.append(self._model_label(model_name))
        return places, skipped

    @api.model
    def _next_steps(self, result):
        steps = []
        missing = result.get("model_key_missing")
        if isinstance(missing, dict) and missing.get("provider"):
            steps.append(self.env._(
                "Add a %(provider)s key in the project's Connectors > AI Models, or pick another model in Melaya.",
                provider=str(missing.get("provider_name") or missing.get("provider"))[:60],
            ))
        connectors = [connector_label(c) for c in _strings(result.get("connectors")) if c != "odoo"]
        if connectors:
            steps.append(self.env._(
                "It uses %(services)s: connect it in the project's Connectors in Melaya if it is not connected yet.",
                services=", ".join(connectors),
            ))
        return steps

    @api.model
    def _add_templates(self, template_ids):
        client = self.env["melaya.client"]
        client._check_admin()
        ids = []
        for tid in template_ids if isinstance(template_ids, (list, tuple)) else []:
            if isinstance(tid, str) and TEMPLATE_ID_RE.match(tid) and tid not in ids:
                ids.append(tid)
        if not ids:
            raise UserError(self.env._("Tick at least one template."))
        if len(ids) > MAX_PER_ADD:
            raise UserError(self.env._("Add at most %(count)s templates at a time.", count=MAX_PER_ADD))
        Exposed = self.env["melaya.exposed.model"]
        added, failed = [], []
        for tid in ids:
            try:
                result = self._call_melaya(client._templates_install, tid)
            except MelayaError as exc:
                failed.append({"id": tid, "message": exc.args[0] if exc.args else str(exc)})
                if exc.code in STOP_CODES:
                    break
                continue
            pipeline = result.get("pipeline")
            if not (isinstance(pipeline, str) and PIPELINE_RE.match(pipeline)):
                failed.append({"id": tid, "message": self.env._("Melaya did not confirm the pipeline. Check the project in Melaya.")})
                continue
            name = str(result.get("name") or pipeline).strip()[:200] or pipeline
            Exposed._remember_pipeline_names([{"id": pipeline, "name": name}])
            models_ = [m for m in _strings(result.get("models")) if MODEL_RE.match(m)]
            places, skipped = self._expose(pipeline, models_)
            if places:
                where = self.env._("Users find it in the Melaya button on %(places)s records.", places=", ".join(places))
            elif not models_:
                where = self.env._("It runs on a schedule or by hand in Melaya. Schedule it from the pipeline in Melaya.")
            else:
                where = self.env._("It is in the Melaya project, but none of the Odoo apps it works on is installed here.")
            added.append({
                "id": tid,
                "pipeline": pipeline,
                "name": name,
                "created": bool(result.get("created")),
                "where": where,
                "skipped": skipped,
                "steps": self._next_steps(result),
                "exposed": bool(places),
            })
        saved, save_error = False, ""
        if any(a["exposed"] for a in added):
            try:
                Exposed._push_to_melaya()
                saved = True
            except MelayaError as exc:
                save_error = self.env._(
                    "The pipelines are in Melaya, but saving the exposed models failed: %(error)s Fix it, then click Save to Melaya in Melaya > Exposed models.",
                    error=exc.args[0] if exc.args else str(exc),
                )
        return {"added": added, "failed": failed, "saved": saved, "save_error": save_error}
