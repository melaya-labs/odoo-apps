from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.fields import Command
from odoo.tests import TransactionCase, tagged

from odoo.addons.melaya_ai_agents.models.melaya_client import APP_PREFIX, MelayaClient, MelayaError

INSTALL_ID = "0b6f7c1e-9a52-4a7e-8d0e-2f1b3c4d5e6f"
NEW_MODEL = "res.partner.category"  # in base, not exposed by these tests
EXPOSED_MODEL = "res.partner"
MISSING_MODEL = "x_melaya_test.absent"


def _template(tid, name, models, **extra):
    item = {
        "id": tid, "name": name, "summary": "%s summary" % name, "how_to_use": "Open a record, click Melaya.",
        "models": models, "modes": ["record"], "edition": "both", "apps": ["Contacts"],
        "connectors": [], "categories": ["Odoo"], "installed": False, "pipeline": None,
    }
    item.update(extra)
    return item


@tagged("post_install", "-at_install")
class TestMelayaTemplates(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = cls.env.ref("base.user_admin")
        Users = cls.env["res.users"].with_context(no_reset_password=True)
        cls.member = Users.create({
            "name": "Melaya Template Member",
            "login": "melaya.tpl.member@example.com",
            "group_ids": [Command.set([cls.env.ref("melaya_ai_agents.group_melaya_user").id])],
        })
        Exposed = cls.env["melaya.exposed.model"].sudo()
        Exposed.search([("model", "=", NEW_MODEL)]).unlink()
        partner_model = cls.env["ir.model"]._get(EXPOSED_MODEL)
        cls.partner_row = Exposed.search([("model_id", "=", partner_model.id)], limit=1) or Exposed.create({"model_id": partner_model.id})
        cls.partner_row.write({"button_active": True, "pipeline_names": "odoo_added_one", "group_ids": [Command.clear()]})

    def setUp(self):
        super().setUp()
        client = self.env["melaya.client"]
        client._param_set("melaya_ai_agents.install_token", "mel_odoo_%s.s3cret" % INSTALL_ID)
        client._param_set("melaya_ai_agents.token_database_uuid", client._database_uuid())
        client._param_set("melaya_ai_agents.api_url", "https://api.example.test")
        self.calls = []
        self.templates = [
            _template("usr_aaaaaaaaaaaaaaaaaaaa", "Tag tidy-up", [NEW_MODEL], connectors=["gmail", "odoo"]),
            _template("usr_bbbbbbbbbbbbbbbbbbbb", "Contact brief", [EXPOSED_MODEL], installed=True, pipeline="odoo_added_one"),
            _template("usr_cccccccccccccccccccc", "Field service", [MISSING_MODEL], edition="enterprise", apps=["Field Service"]),
            _template("usr_dddddddddddddddddddd", "Tag report", [NEW_MODEL], installed=True, pipeline="odoo_tag_report", apps=[]),
        ]
        self.installs = {
            "usr_aaaaaaaaaaaaaaaaaaaa": {
                "pipeline": "odoo_tag_tidy_up", "name": "Tag tidy-up", "created": True,
                "models": [NEW_MODEL, MISSING_MODEL], "connectors": ["gmail", "odoo"],
                "model_key_missing": {"provider": "qwen", "provider_name": "Qwen (Alibaba)", "model": "qwen3.7-plus"},
            },
        }
        self.missing_endpoint = False
        self.save_refused = False

    # ------------------------------------------------------------------
    def _fake_post(self):
        test = self

        def fake(client, path, payload, timeout=None, token=None):
            endpoint = path[len(APP_PREFIX):]
            test.calls.append((endpoint, payload))
            if endpoint.startswith("templates/") and test.missing_endpoint:
                # What api.melaya.org answers before the server deploy: 404, an HTML page.
                raise client._error_for(404, None, {})
            if endpoint == "templates/list":
                return {"project": {"name": "odoo20"}, "templates": test.templates}
            if endpoint == "templates/install":
                result = test.installs.get(payload["template_id"])
                if not result:
                    raise client._error_for(404, "template_not_found", {"error": "template_not_found"})
                return dict(result, created=result["created"] and not any(
                    e == "templates/install" and p["template_id"] == payload["template_id"] for e, p in test.calls[:-1]))
            if endpoint == "settings/set":
                if test.save_refused:
                    raise client._error_for(400, "unknown_pipelines", {"error": "unknown_pipelines", "pipelines": ["odoo_tag_tidy_up"]})
                return {"ok": True}
            raise AssertionError("unexpected call %s" % endpoint)
        return patch.object(MelayaClient, "_post", fake)

    def _picker(self, user=None):
        return self.env["melaya.template.picker"].with_user(user or self.admin)

    def _row(self, model_name):
        return self.env["melaya.exposed.model"].sudo().search([("model", "=", model_name)], limit=1)

    # ------------------------------------------------------------------
    def test_picker_lists_templates_with_their_state(self):
        with self._fake_post():
            data = self._picker()._picker_data(NEW_MODEL)
        self.assertEqual(self.calls[0][0], "templates/list")
        self.assertEqual(self.calls[0][1]["odoo_user"]["is_admin"], True)
        self.assertEqual(data["model"], NEW_MODEL)
        self.assertTrue(data["model_label"])
        self.assertEqual(data["project_name"], "odoo20")
        by_id = {t["id"]: t for t in data["templates"]}
        self.assertEqual(by_id["usr_aaaaaaaaaaaaaaaaaaaa"]["state"], "new")
        self.assertEqual(by_id["usr_aaaaaaaaaaaaaaaaaaaa"]["connectors"], "Gmail", "the project's own Odoo connection is not listed")
        self.assertEqual(by_id["usr_bbbbbbbbbbbbbbbbbbbb"]["state"], "added")
        self.assertEqual(by_id["usr_cccccccccccccccccccc"]["state"], "unavailable")
        self.assertTrue(by_id["usr_cccccccccccccccccccc"]["enterprise"])
        self.assertIn("Field Service", by_id["usr_cccccccccccccccccccc"]["note"])
        self.assertEqual(by_id["usr_dddddddddddddddddddd"]["state"], "in_project")
        self.assertTrue(by_id["usr_dddddddddddddddddddd"]["note"])
        # Grouped by Odoo app, templates without an app last.
        self.assertEqual([t["group"] for t in data["templates"]][-1], by_id["usr_dddddddddddddddddddd"]["group"])
        self.assertEqual(by_id["usr_aaaaaaaaaaaaaaaaaaaa"]["group"], "Contacts")

    def test_scheduled_template_and_technical_app_names(self):
        # Melaya lists apps by technical module name; a template with no record
        # model runs on a schedule and must not read as "App not installed".
        self.templates = [
            _template("usr_eeeeeeeeeeeeeeeeeeee", "Cash forecast", [], apps=["base"], connectors=["github", "microsoft_teams"]),
            _template("usr_ffffffffffffffffffff", "Exec brief", [], apps=[], installed=True, pipeline="odoo_exec_brief"),
        ]
        with self._fake_post():
            data = self._picker()._picker_data()
        by_id = {t["id"]: t for t in data["templates"]}
        cash = by_id["usr_eeeeeeeeeeeeeeeeeeee"]
        self.assertEqual(cash["state"], "new")
        self.assertIn("schedule", cash["note"])
        base_name = self.env["ir.module.module"].sudo().search([("name", "=", "base")]).shortdesc
        self.assertEqual(cash["apps"], base_name)
        self.assertEqual(cash["group"], base_name)
        self.assertEqual(cash["connectors"], "GitHub, Microsoft Teams")
        self.assertEqual(by_id["usr_ffffffffffffffffffff"]["state"], "added")

    def test_add_scheduled_template_says_where_it_runs(self):
        self.templates = [_template("usr_eeeeeeeeeeeeeeeeeeee", "Cash forecast", [], apps=["base"])]
        self.installs = {"usr_eeeeeeeeeeeeeeeeeeee": {"pipeline": "odoo_cash_forecast", "name": "Cash forecast",
                                                      "created": True, "models": [], "connectors": []}}
        with self._fake_post():
            result = self._picker()._add_templates(["usr_eeeeeeeeeeeeeeeeeeee"])
        self.assertEqual(len(result["added"]), 1)
        self.assertIn("schedule", result["added"][0]["where"])
        self.assertFalse(result["added"][0]["exposed"])
        self.assertNotIn("settings/set", [e for e, _p in self.calls])

    def test_add_creates_the_exposed_row_and_saves_to_melaya(self):
        with self._fake_post():
            result = self._picker()._add_templates(["usr_aaaaaaaaaaaaaaaaaaaa"])
        row = self._row(NEW_MODEL)
        self.assertTrue(row)
        self.assertTrue(row.button_active)
        self.assertTrue(row.action_id, "the Melaya action is bound")
        self.assertEqual(row._pipeline_list(), ["odoo_tag_tidy_up"])
        endpoints = [e for e, _p in self.calls]
        self.assertEqual(endpoints, ["templates/install", "settings/set"])
        self.assertEqual(self.calls[0][1]["template_id"], "usr_aaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(self.calls[1][1]["exposed"][NEW_MODEL], ["odoo_tag_tidy_up"])
        self.assertTrue(result["saved"])
        self.assertFalse(result["failed"])
        added = result["added"][0]
        self.assertTrue(added["created"])
        self.assertIn(self._picker()._model_label(NEW_MODEL), added["where"])
        self.assertEqual(added["skipped"], [MISSING_MODEL])
        self.assertEqual(len(added["steps"]), 2)
        self.assertIn("Qwen (Alibaba)", added["steps"][0])
        self.assertIn("Connectors > AI Models", added["steps"][0])
        self.assertIn("Gmail", added["steps"][1])
        names = {p["id"]: p["name"] for p in self.env["melaya.exposed.model"]._available_pipelines()}
        self.assertEqual(names.get("odoo_tag_tidy_up"), "Tag tidy-up", "runs show the display name")

    def test_adding_twice_changes_nothing(self):
        with self._fake_post():
            self._picker()._add_templates(["usr_aaaaaaaaaaaaaaaaaaaa"])
            again = self._picker()._add_templates(["usr_aaaaaaaaaaaaaaaaaaaa"])
        self.assertFalse(again["added"][0]["created"])
        self.assertEqual(self._row(NEW_MODEL)._pipeline_list(), ["odoo_tag_tidy_up"])

    def test_add_appends_to_an_existing_row_and_shows_it(self):
        row = self.env["melaya.exposed.model"].sudo().create({
            "model_id": self.env["ir.model"]._get(NEW_MODEL).id, "pipeline_names": "kept_pipeline", "button_active": False,
        })
        with self._fake_post():
            self._picker()._add_templates(["usr_aaaaaaaaaaaaaaaaaaaa"])
        self.assertEqual(row._pipeline_list(), ["kept_pipeline", "odoo_tag_tidy_up"])
        self.assertTrue(row.button_active)

    def test_refused_save_is_reported_and_kept_in_odoo(self):
        self.save_refused = True
        with self._fake_post():
            result = self._picker()._add_templates(["usr_aaaaaaaaaaaaaaaaaaaa"])
        self.assertFalse(result["saved"])
        self.assertIn("Save to Melaya", result["save_error"])
        self.assertEqual(self._row(NEW_MODEL)._pipeline_list(), ["odoo_tag_tidy_up"])

    def test_unknown_template_is_listed_as_not_added(self):
        with self._fake_post():
            result = self._picker()._add_templates(["usr_eeeeeeeeeeeeeeeeeeee", "usr_aaaaaaaaaaaaaaaaaaaa"])
        self.assertEqual([f["id"] for f in result["failed"]], ["usr_eeeeeeeeeeeeeeeeeeee"])
        self.assertEqual([a["id"] for a in result["added"]], ["usr_aaaaaaaaaaaaaaaaaaaa"])

    def test_melaya_without_the_endpoints_answers_not_available_yet(self):
        self.missing_endpoint = True
        with self._fake_post():
            with self.assertRaises(MelayaError) as ctx:
                self._picker()._picker_data()
            self.assertEqual(ctx.exception.code, "templates_unavailable")
            self.assertIn("not available from Melaya yet", ctx.exception.args[0])
            result = self._picker()._add_templates(["usr_aaaaaaaaaaaaaaaaaaaa", "usr_dddddddddddddddddddd"])
        self.assertEqual(len(result["failed"]), 1, "stops at the first answer")
        self.assertFalse(result["added"])
        self.assertFalse(self._row(NEW_MODEL))
        self.assertNotIn("settings/set", [e for e, _p in self.calls])

    def test_administrators_only(self):
        with self._fake_post():
            with self.assertRaises(AccessError):
                self._picker(self.member)._picker_data()
            with self.assertRaises(AccessError):
                self._picker(self.member)._add_templates(["usr_aaaaaaaaaaaaaaaaaaaa"])
            with self.assertRaises(AccessError):
                self._picker(self.member).action_open(NEW_MODEL)
        self.assertEqual(self.calls, [])
        action = self._picker().action_open(NEW_MODEL)
        self.assertEqual(action["tag"], "melaya_ai_agents.template_picker")
        self.assertEqual(action["params"]["model"], NEW_MODEL)
        from_row = self.partner_row.with_user(self.admin).action_open_templates()
        self.assertEqual(from_row["params"]["model"], EXPOSED_MODEL)
        self.assertEqual(self._picker().action_open("not a model")["params"]["model"], "")

    def test_record_panel_empty_state(self):
        Panel = self.env["melaya.record.panel"]
        self.assertEqual(Panel.with_user(self.admin)._empty_state(EXPOSED_MODEL), {"can_add_templates": True, "model": EXPOSED_MODEL})
        self.assertEqual(Panel.with_user(self.member)._empty_state(EXPOSED_MODEL), {"can_add_templates": False, "model": ""})
        self.env["melaya.client"]._param_set("melaya_ai_agents.install_token", None)
        self.assertFalse(Panel.with_user(self.admin)._empty_state(EXPOSED_MODEL)["can_add_templates"])
