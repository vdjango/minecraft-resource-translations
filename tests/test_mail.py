import json
import smtplib
import unittest
from unittest.mock import MagicMock, patch

from mc_translate.config import Config
from mc_translate.mail import MailService, send_smtp
from mc_translate.management import Settings
from tests.test_platform import PlatformFixture


class MailCase(PlatformFixture):
    async def test_application_durable_without_smtp_in_request_and_retries(self):
        await self.app["mail"].close()
        response = await self.client.post(
            "/v1/clients/applications",
            json=dict(
                name="Example",
                contact="developer@example.org",
                website="https://example.org",
                purpose="Integration",
            ),
        )
        receipt = await response.json()
        row = await self.store.one("SELECT * FROM mail_outbox")
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["attempt"], 0)
        self.assertNotIn(receipt["access_token"], row["payload"])
        self.assertEqual(
            json.loads(row["payload"])["application_id"], receipt["application_id"]
        )
        sender = MagicMock(
            side_effect=smtplib.SMTPAuthenticationError(535, b"secret-provider-detail")
        )
        service = MailService(self.config, self.store, sender)
        self.config.mail_enabled = True
        self.assertTrue(await service.deliver_one())
        row = await self.store.one("SELECT * FROM mail_outbox")
        self.assertEqual(row["error"], "smtp_535")
        self.assertEqual(row["attempt"], 1)
        self.assertFalse(await service.deliver_one())  # bounded retry, no busy loop
        for _ in range(4):
            await self.store.db.execute("UPDATE mail_outbox SET next_attempt=0")
            await service.deliver_one()
        self.assertEqual(
            (await self.store.one("SELECT * FROM mail_outbox"))["state"], "failed"
        )
        self.assertEqual(sender.call_count, 5)

    async def test_admin_config_redaction_validation_and_queued_test(self):
        await self.app["mail"].close()
        self.config.admin_token = "admin-mail"
        headers = {"Authorization": "Bearer admin-mail"}
        self.assertEqual(
            (await self.client.post("/v1/admin/mail", json={"action": "test"})).status,
            401,
        )
        changes = dict(
            mail_enabled=True,
            smtp_username="sender@163.com",
            smtp_password="test-secret",
            mail_from="sender@163.com",
            mail_to="receiver@qq.com",
        )
        response = await self.client.post(
            "/v1/admin/settings", headers=headers, json=changes
        )
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertTrue(data["smtp_password_configured"])
        self.assertNotIn("test-secret", json.dumps(data))
        self.assertEqual(
            Settings(Config(database=self.config.database)).config.smtp_password,
            "test-secret",
        )
        for changes in (
            {"mail_to": "x@qq.com\nBcc: other@qq.com"},
            {"mail_enabled": "true"},
            {"smtp_security": "none"},
            {"smtp_port": 0},
        ):
            self.assertEqual(
                (
                    await self.client.post(
                        "/v1/admin/settings", headers=headers, json=changes
                    )
                ).status,
                400,
            )
        response = await self.client.post(
            "/v1/admin/mail", headers=headers, json={"action": "test"}
        )
        self.assertEqual(response.status, 202)
        sender = MagicMock()
        service = MailService(self.config, self.store, sender)
        self.assertTrue(await service.deliver_one())
        row = await self.store.one("SELECT * FROM mail_outbox")
        self.assertEqual(row["state"], "sent")
        self.assertIsNotNone(row["sent_at"])
        data = await (await self.client.get("/v1/admin/mail", headers=headers)).json()
        self.assertNotIn("payload", data["messages"][0])
        self.assertNotIn("test-secret", json.dumps(data))


class SMTPCase(unittest.TestCase):
    def test_tls_auth_envelope_and_stable_message_id(self):
        config = Config(
            smtp_username="sender@163.com",
            smtp_password="private",
            mail_from="sender@163.com",
            mail_to="receiver@qq.com",
        )
        connection = MagicMock()
        connection.send_message.return_value = {}
        with patch(
            "mc_translate.mail.smtplib.SMTP_SSL", return_value=connection
        ) as factory:
            send_smtp(
                config,
                dict(id="mail_fixture", created=0, payload=json.dumps({"test": True})),
            )
        self.assertEqual(factory.call_args.args[:2], ("smtp.163.com", 465))
        self.assertIn("context", factory.call_args.kwargs)
        connection.login.assert_called_once_with("sender@163.com", "private")
        message = connection.send_message.call_args.args[0]
        self.assertEqual(message["To"], "receiver@qq.com")
        self.assertEqual(message["Message-ID"], "<mail_fixture@bugg.icu>")
        self.assertNotIn("private", message.as_string())
        connection.close.assert_called_once()
