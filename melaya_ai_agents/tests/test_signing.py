import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.melaya_ai_agents.lib import signing

# Fixed vector, shared with integrations/odoo/tests/test_signing_vector.py.
SECRET = "s3cret"
PATH = "/api/v1/odoo/app/card"
TIMESTAMP = "1700000000"
NONCE = "abcdefghijklmnop"
BODY = b'{"a":1}'
BODY_SHA256 = "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"
EXPECTED_HEX = "ed0b83decd745958c602d615910fbe00460735cd55f0df2ca488e568886dc94f"
INSTALL_ID = "0b6f7c1e-9a52-4a7e-8d0e-2f1b3c4d5e6f"
SESSION = "odoo.addons.melaya_ai_agents.models.melaya_client._http_session"


@tagged("post_install", "-at_install")
class TestMelayaSigning(TransactionCase):

    def test_string_to_sign(self):
        self.assertEqual(
            signing.string_to_sign(PATH, TIMESTAMP, NONCE, BODY),
            "POST\n%s\n%s\n%s\n%s" % (PATH, TIMESTAMP, NONCE, BODY_SHA256),
        )

    def test_signature_vector(self):
        self.assertEqual(signing.sign(SECRET, PATH, TIMESTAMP, NONCE, BODY), "v1=" + EXPECTED_HEX)

    def test_signed_headers(self):
        token = "mel_odoo_%s.%s" % (INSTALL_ID, SECRET)
        headers = signing.signed_headers(token, PATH, BODY, timestamp=TIMESTAMP, nonce=NONCE)
        self.assertEqual(headers["X-Melaya-Install"], INSTALL_ID)
        self.assertEqual(headers["X-Melaya-Timestamp"], TIMESTAMP)
        self.assertEqual(headers["X-Melaya-Nonce"], NONCE)
        self.assertEqual(headers["X-Melaya-Signature"], "v1=" + EXPECTED_HEX)

    def test_bad_token_refused(self):
        for token in ("", "mel_odoo_", "mel_odoo_abc", "other_abc.def"):
            with self.assertRaises(signing.InvalidInstallToken):
                signing.parse_install_token(token)

    def _fake_session(self, captured, status=200, payload=None):
        payload = {"ok": True} if payload is None else payload

        class FakeResponse:
            status_code = status
            content = b"{}"

            def json(self):
                return payload

        class FakeSession:
            def post(self, url, data=None, headers=None, timeout=None, allow_redirects=None):
                captured.update(url=url, data=data, headers=headers, timeout=timeout)
                return FakeResponse()

        return FakeSession()

    def _connect(self):
        client = self.env["melaya.client"]
        client._param_set("melaya_ai_agents.install_token", "mel_odoo_%s.%s" % (INSTALL_ID, SECRET))
        client._param_set("melaya_ai_agents.token_database_uuid", client._database_uuid())
        client._param_set("melaya_ai_agents.api_url", "https://api.example.test")
        return client

    def test_client_signs_the_sent_body(self):
        """melaya.client signs exactly the bytes it sends, on the request path."""
        client = self._connect()
        captured = {}
        with patch(SESSION, lambda: self._fake_session(captured)):
            result = client._status()
        self.assertEqual(result, {"ok": True})
        self.assertEqual(captured["url"], "https://api.example.test/api/v1/odoo/app/status")
        headers = captured["headers"]
        expected = signing.sign(
            SECRET, "/api/v1/odoo/app/status", headers["X-Melaya-Timestamp"], headers["X-Melaya-Nonce"], captured["data"]
        )
        self.assertEqual(headers["X-Melaya-Signature"], expected)
        body = json.loads(captured["data"])
        self.assertEqual(body["database_uuid"], client._database_uuid())
        self.assertIn("odoo_user", body)
        self.assertNotIn(SECRET.encode(), captured["data"])
        self.assertEqual(captured["timeout"], (3.05, 10))

    def test_login_only_after_link(self):
        client = self._connect()
        captured = {}
        with patch(SESSION, lambda: self._fake_session(captured)):
            client._status()
        self.assertEqual(json.loads(captured["data"])["odoo_user"]["login"], "")
        with patch(SESSION, lambda: self._fake_session(captured, payload={"url": "https://app.example.test/l"})):
            client._link_start()
        self.assertEqual(json.loads(captured["data"])["odoo_user"]["login"], self.env.user.login)

    def test_database_copy_never_sends_the_token(self):
        client = self._connect()
        client._param_set("melaya_ai_agents.token_database_uuid", "another-database")
        self.assertTrue(client._is_database_copy())
        self.assertFalse(client._is_connected())
        captured = {}
        with patch(SESSION, lambda: self._fake_session(captured)):
            with self.assertRaises(UserError) as caught:
                client._status()
        self.assertEqual(getattr(caught.exception, "code", None), "database_copy")
        self.assertEqual(captured, {})

    def test_stale_timestamp_message(self):
        client = self._connect()
        server_time = (datetime.now(timezone.utc) - timedelta(minutes=7)).isoformat()
        captured = {}
        fake = self._fake_session(captured, status=401, payload={"error": "stale_timestamp", "server_time": server_time})
        with patch(SESSION, lambda: fake):
            with self.assertRaises(UserError) as caught:
                client._status()
        self.assertIn("7", caught.exception.args[0])
