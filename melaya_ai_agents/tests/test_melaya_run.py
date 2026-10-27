from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError
from odoo.fields import Command
from odoo.tests import TransactionCase, tagged

from odoo.addons.melaya_ai_agents.models.melaya_client import MelayaClient, MelayaError

INSTALL_ID = "0b6f7c1e-9a52-4a7e-8d0e-2f1b3c4d5e6f"
RUN_URL = "https://app.melaya.example/projects/p/runs/%s"


@tagged("post_install", "-at_install")
class TestMelayaRun(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.melaya_group = cls.env.ref("melaya_ai_agents.group_melaya_user")
        Users = cls.env["res.users"].with_context(no_reset_password=True)
        cls.user_a = Users.create({
            "name": "Melaya Run A",
            "login": "melaya.run.a@example.com",
            "email": "melaya.run.a@example.com",
            "group_ids": [Command.set([cls.melaya_group.id])],
        })
        cls.user_b = Users.create({
            "name": "Melaya Run B",
            "login": "melaya.run.b@example.com",
            "email": "melaya.run.b@example.com",
            "group_ids": [Command.set([cls.melaya_group.id])],
        })
        cls.outsider = Users.create({
            "name": "Melaya Outsider",
            "login": "melaya.run.outsider@example.com",
            "email": "melaya.run.outsider@example.com",
            "group_ids": [Command.set([cls.env.ref("base.group_user").id])],
        })
        cls.agent = Users.create({
            "name": "Melaya Agent (test)",
            "login": "melaya.run.agent@example.com",
            "group_ids": [Command.set([cls.env.ref("base.group_user").id])],
        })
        cls.test_user_ids = {cls.user_a.id, cls.user_b.id, cls.outsider.id}
        cls.partner = cls.env["res.partner"].create({"name": "Melaya run test customer"})
        model = cls.env["ir.model"]._get("res.partner")
        Exposed = cls.env["melaya.exposed.model"].sudo()
        cls.exposed = Exposed.search([("model_id", "=", model.id)], limit=1) or Exposed.create({"model_id": model.id})
        cls.exposed.write({"button_active": True, "group_ids": [Command.clear()]})

    def setUp(self):
        super().setUp()
        client = self.env["melaya.client"]
        client._param_set("melaya_ai_agents.install_token", "mel_odoo_%s.s3cret" % INSTALL_ID)
        client._param_set("melaya_ai_agents.token_database_uuid", client._database_uuid())
        client._param_set("melaya_ai_agents.api_url", "https://api.example.test")
        client._param_set("melaya_ai_agents.agent_user_id", self.agent.id)
        self.statuses = {}
        self.calls = []

    # ------------------------------------------------------------------
    def _run(self, user, run_id, status="started", **vals):
        values = {
            "run_id": run_id,
            "model": "res.partner",
            "res_id": self.partner.id,
            "record_name": self.partner.display_name,
            "user_id": user.id,
            "pipeline": "quote-follow-up",
            "pipeline_name": "Quote follow-up",
            "status": status,
            "url": RUN_URL % run_id,
        }
        values.update(vals)
        return self.env["melaya.run"].sudo().create(values)

    def _fake_status(self):
        test = self

        def fake(client, run_ids):
            # A database in use has its own runs (other users), which the cron
            # follows too; only the test users' calls are recorded and answered.
            if client.env.uid not in test.test_user_ids:
                return {"runs": []}
            test.calls.append((client.env.uid, list(run_ids)))
            return {"runs": [
                {"run_id": r, "status": test.statuses[r], "url": RUN_URL % r}
                for r in run_ids if r in test.statuses
            ]}
        return patch.object(MelayaClient, "_runs_status", fake)

    def _follow(self):
        with self._fake_status():
            self.env["melaya.run"]._follow_runs()

    def _notes(self, text):
        note = self.env.ref("mail.mt_note")
        return self.partner.message_ids.filtered(lambda m: m.subtype_id == note and text in (m.body or ""))

    # ------------------------------------------------------------------
    def test_record_rules(self):
        own = self._run(self.user_a, "run_rule_a1")
        other = self._run(self.user_b, "run_rule_b1")
        Runs = self.env["melaya.run"]
        self.assertEqual(Runs.with_user(self.user_a).search([("id", "in", (own | other).ids)]), own)
        self.assertEqual(Runs.with_user(self.user_b).search([("id", "in", (own | other).ids)]), other)
        admin = self.env.ref("base.user_admin")
        self.assertEqual(Runs.with_user(admin).search([("id", "in", (own | other).ids)]), own | other)
        with self.assertRaises(AccessError):
            other.with_user(self.user_a).read(["status"])
        with self.assertRaises(AccessError):
            own.with_user(self.user_a).write({"status": "success"})
        with self.assertRaises(AccessError):
            Runs.with_user(self.outsider).search([])

    def test_approval_activity_then_one_note(self):
        run = self._run(self.user_a, "run_appr_01")
        self.statuses["run_appr_01"] = "approval_needed"
        self._follow()
        self.assertEqual(self.calls, [(self.user_a.id, ["run_appr_01"])], "called as the requester")
        self.assertEqual(run.state, "approval")
        activity = run.activity_id
        self.assertTrue(activity)
        self.assertEqual(activity.res_model, "res.partner")
        self.assertEqual(activity.res_id, self.partner.id)
        self.assertEqual(activity.user_id, self.user_a)
        self.assertEqual(activity.summary, "Approve in Melaya")
        self.assertEqual(activity.activity_type_id, self.env.ref("mail.mail_activity_data_todo"))
        self.assertIn(RUN_URL % "run_appr_01", str(activity.note))

        # Still waiting: no second activity.
        self._follow()
        self.assertEqual(
            self.env["mail.activity"].search_count([
                ("res_model", "=", "res.partner"), ("res_id", "=", self.partner.id), ("summary", "=", "Approve in Melaya"),
            ]),
            1,
        )

        self.statuses["run_appr_01"] = "success"
        self._follow()
        self.assertEqual(run.state, "done")
        self.assertFalse(run.activity_id)
        self.assertFalse(activity.active, "the reminder is marked done")
        self.assertTrue(run.result_posted)
        notes = self._notes("Quote follow-up completed.")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes.author_id, self.agent.partner_id)
        self.assertIn(RUN_URL % "run_appr_01", notes.body)

        # Finished runs are not asked for again and never posted twice.
        self.calls.clear()
        self._follow()
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self._notes("Quote follow-up completed.")), 1)

    def test_failed_and_canceled_notes(self):
        self._run(self.user_a, "run_fail_01")
        self._run(self.user_b, "run_cncl_01", pipeline_name="Invoice reminder")
        self.statuses.update({"run_fail_01": "failed", "run_cncl_01": "canceled"})
        self._follow()
        self.assertEqual(len(self._notes("Quote follow-up failed.")), 1)
        self.assertEqual(len(self._notes("Invoice reminder was canceled.")), 1)
        self.assertEqual({uid for uid, _ids in self.calls}, {self.user_a.id, self.user_b.id})

    def test_record_gone_posts_nothing(self):
        partner = self.env["res.partner"].create({"name": "Melaya gone"})
        run = self._run(self.user_a, "run_gone_01", res_id=partner.id)
        partner.unlink()
        self.statuses["run_gone_01"] = "success"
        self._follow()
        self.assertEqual(run.state, "done")
        self.assertTrue(run.result_posted)

    def test_old_runs_go_stale_without_call_or_note(self):
        run = self._run(self.user_a, "run_old_001", started_at=fields.Datetime.now() - timedelta(hours=25))
        self.statuses["run_old_001"] = "success"
        self._follow()
        self.assertEqual(run.state, "stale")
        self.assertEqual(self.calls, [])
        self.assertFalse(self._notes("Quote follow-up completed."))

    def test_open_reminder_keeps_following(self):
        run = self._run(self.user_a, "run_wait_01", status="approval_needed", state="approval")
        self.statuses["run_wait_01"] = "approval_needed"
        self._follow()
        self.assertTrue(run.activity_id)
        run.started_at = fields.Datetime.now() - timedelta(days=2)
        self._follow()
        self.assertEqual(run.state, "approval", "an open reminder is followed for a week")
        run.started_at = fields.Datetime.now() - timedelta(days=8)
        activity = run.activity_id
        self._follow()
        self.assertEqual(run.state, "stale")
        self.assertFalse(activity.exists())

    def test_twenty_ids_per_call(self):
        for i in range(25):
            self._run(self.user_a, "run_many_%02d" % i)
        self._follow()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([len(ids) for _uid, ids in self.calls], [20, 5])

    def test_not_connected_does_not_call(self):
        self._run(self.user_a, "run_disc_01")
        self.env["melaya.client"]._param_set("melaya_ai_agents.install_token", None)
        self._follow()
        self.assertEqual(self.calls, [])

    def test_melaya_down_stops_the_cron_run(self):
        self._run(self.user_a, "run_down_a1")
        self._run(self.user_b, "run_down_b1")
        calls = []

        def down(client, run_ids):
            calls.append(client.env.uid)
            raise MelayaError("down", code="timeout")

        with patch.object(MelayaClient, "_runs_status", down):
            self.env["melaya.run"]._follow_runs()
        self.assertEqual(len(calls), 1)

    def test_ui_config(self):
        Panel = self.env["melaya.record.panel"]
        config = Panel.with_user(self.user_a)._ui_config()
        self.assertTrue(config["enabled"])
        self.assertIn("res.partner", config["models"])
        self.assertEqual(Panel.with_user(self.outsider)._ui_config(), {"enabled": False, "models": []})

        self.exposed.group_ids = [Command.set([self.env.ref("base.group_system").id])]
        self.assertNotIn("res.partner", Panel.with_user(self.user_a)._ui_config()["models"])
        self.exposed.group_ids = [Command.clear()]

        self.env["melaya.client"]._param_set("melaya_ai_agents.install_token", None)
        self.assertEqual(Panel.with_user(self.user_a)._ui_config(), {"enabled": False, "models": []})

    def test_starting_run_matched_from_the_card(self):
        Runs = self.env["melaya.run"].with_user(self.user_a)
        placeholder = Runs._record_start(self.partner, "quote-follow-up", status="starting")
        self.assertTrue(placeholder)
        self.assertFalse(placeholder.run_id)
        Runs._sync_from_panel([{
            "run_id": "run_late_01",
            "pipeline": "quote-follow-up",
            "name": "Quote follow-up",
            "status": "running",
            "started_at": fields.Datetime.now().isoformat() + "Z",
            "url": RUN_URL % "run_late_01",
        }], record=self.partner)
        self.assertEqual(placeholder.run_id, "run_late_01")
        self.assertEqual(placeholder.status, "running")
        self.assertEqual(placeholder.url, RUN_URL % "run_late_01")

        # Another user's card never adopts it.
        other = self.env["melaya.run"].with_user(self.user_b)._record_start(self.partner, "quote-follow-up", status="starting")
        self.env["melaya.run"].with_user(self.user_b)._sync_from_panel(
            [{"run_id": "run_late_01", "pipeline": "quote-follow-up", "status": "running"}], record=self.partner,
        )
        self.assertFalse(other.run_id)

    def test_cron_matches_a_starting_run_without_the_panel(self):
        Runs = self.env["melaya.run"].with_user(self.user_a)
        old = Runs._record_start(self.partner, "quote-follow-up", status="starting")
        fresh = self.env["melaya.run"].with_user(self.user_b)._record_start(self.partner, "quote-follow-up", status="starting")
        # Only placeholders past the grace period are checked (the panel usually matches first).
        self.env.cr.execute("UPDATE melaya_run SET create_date = create_date - interval '2 minutes' WHERE id = %s", [old.id])
        old.invalidate_recordset(["create_date"])
        test = self
        cards = []

        def card(client, model, record_id):
            if client.env.uid not in test.test_user_ids:
                return {"runs": []}
            cards.append((client.env.uid, model, record_id))
            return {"runs": [{
                "run_id": "run_cron_01", "pipeline": "quote-follow-up", "name": "Quote follow-up",
                "status": "approval_needed", "started_at": fields.Datetime.now().isoformat() + "Z",
                "url": RUN_URL % "run_cron_01",
            }]}

        self.statuses["run_cron_01"] = "approval_needed"
        with patch.object(MelayaClient, "_card", card), self._fake_status():
            self.env["melaya.run"]._follow_runs()
        self.assertEqual(cards, [(self.user_a.id, "res.partner", self.partner.id)], "one card call, as the requester")
        self.assertEqual(old.run_id, "run_cron_01")
        self.assertEqual(old.state, "approval")
        self.assertTrue(old.activity_id, "the approval reminder follows once the run is known")
        self.assertFalse(fresh.run_id)

    def test_panel_status_update_keeps_known_status(self):
        run = self._run(self.user_a, "run_keep_01", status="approval_needed", state="approval")
        self.env["melaya.run"].with_user(self.user_a)._sync_from_panel([{"run_id": "run_keep_01", "status": "started"}])
        self.assertEqual(run.status, "approval_needed")
        self.env["melaya.run"].with_user(self.user_b)._sync_from_panel([{"run_id": "run_keep_01", "status": "success"}])
        self.assertEqual(run.status, "approval_needed", "only the requester's panel updates the run")

    def test_finished_run_still_takes_the_display_name(self):
        run = self._run(self.user_a, "run_name_01", status="success", state="done", pipeline_name="quote-follow-up")
        self.env["melaya.run"].with_user(self.user_a)._sync_from_panel(
            [{"run_id": "run_name_01", "status": "failed", "name": "Quote follow-up"}])
        self.assertEqual(run.pipeline_name, "Quote follow-up")
        self.assertEqual(run.status, "success", "a finished run's status is final")

    def test_systray_data(self):
        self._run(self.user_a, "run_tray_a1", status="approval_needed", state="approval")
        self._run(self.user_a, "run_tray_a2")
        self._run(self.user_b, "run_tray_b1", status="approval_needed", state="approval")
        data = self.env["melaya.run"].with_user(self.user_a)._systray_data()
        self.assertEqual({r["res_id"] for r in data["runs"]}, {self.partner.id})
        self.assertEqual(len(data["runs"]), 2)
        self.assertEqual(data["approval_count"], 1)
        with self.assertRaises(AccessError):
            self.env["melaya.run"].with_user(self.outsider)._systray_data()
