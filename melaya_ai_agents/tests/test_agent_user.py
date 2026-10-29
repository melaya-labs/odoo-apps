# Odoo 19.0 overlay of melaya_ai_agents/tests/test_agent_user.py (20.0).
# Difference: Odoo 19 binary fields read back base64 (20: raw bytes), and
# image_process may re-encode the PNG, so the logo test compares the decoded
# image size with the source PNG instead of the bytes.
# Keep in sync: python integrations/odoo/compat/check_overlay.py
import base64
import io

from PIL import Image

from odoo.tests import TransactionCase, tagged

from odoo.addons.melaya_ai_agents.wizards.melaya_connect_wizard import (
    AGENT_NAME,
    agent_avatar,
    has_placeholder_avatar,
)


@tagged("post_install", "-at_install")
class TestMelayaAgentUser(TransactionCase):

    def _wizard(self, login):
        return self.env["melaya.connect.wizard"].create({"agent_login": login})

    def test_new_agent_gets_the_melaya_logo(self):
        groups = self.env.ref("base.group_partner_manager")
        user, created = self._wizard("melaya.agent@test-new")._ensure_agent_user(groups)
        self.assertTrue(created)
        self.assertEqual(user.name, AGENT_NAME)
        self.assertFalse(has_placeholder_avatar(user))
        stored = Image.open(io.BytesIO(base64.b64decode(user.image_1920)))
        source = Image.open(io.BytesIO(base64.b64decode(agent_avatar())))
        self.assertEqual(stored.size, source.size)

    def test_reused_agent_placeholder_is_replaced_custom_picture_kept(self):
        Users = self.env["res.users"].with_context(no_reset_password=True)
        plain = Users.create({"name": AGENT_NAME, "login": "melaya.agent@test-plain"})
        self.assertTrue(has_placeholder_avatar(plain))
        groups = self.env.ref("base.group_partner_manager")
        user, created = self._wizard("melaya.agent@test-plain")._ensure_agent_user(groups)
        self.assertFalse(created)
        self.assertFalse(has_placeholder_avatar(user))

        custom = Users.create({
            "name": AGENT_NAME,
            "login": "melaya.agent@test-custom",
            "image_1920": self.env.company.logo,
        })
        before = bytes(custom.image_1920)
        self._wizard("melaya.agent@test-custom")._ensure_agent_user(groups)
        self.assertEqual(bytes(custom.image_1920), before)


@tagged("post_install", "-at_install")
class TestMelayaPipelineNames(TransactionCase):

    def test_card_pipeline_names_are_merged_into_the_cache(self):
        Exposed = self.env["melaya.exposed.model"]
        self.env["melaya.client"]._param_set(
            "melaya_ai_agents.available_pipelines",
            '[{"id": "old_one", "name": "Old one"}, {"id": "quote_follow_up", "name": "quote_follow_up"}]',
        )
        Exposed._remember_pipeline_names([
            {"id": "quote_follow_up", "name": "Quote follow-up"},
            {"id": "new_one", "name": "New one"},
            {"id": "", "name": "ignored"},
            {"id": "bad", "name": None},
        ])
        names = {p["id"]: p["name"] for p in Exposed._available_pipelines()}
        self.assertEqual(names, {"old_one": "Old one", "quote_follow_up": "Quote follow-up", "new_one": "New one"})
        self.assertEqual(self.env["melaya.run"]._pipeline_display_name("quote_follow_up"), "Quote follow-up")
