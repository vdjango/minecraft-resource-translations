"""Durable, single-consumer notification outbox. SMTP never runs in HTTP handlers."""

import asyncio
import json
import smtplib
import ssl
import time
import uuid
from dataclasses import replace
from email.message import EmailMessage
from email.utils import formatdate


async def enqueue(db, event, payload):
    identity, stamp = "mail_" + uuid.uuid4().hex, time.time()
    await db.execute(
        "INSERT OR IGNORE INTO mail_outbox(id,event,payload,created,updated) VALUES(?,?,?,?,?)",
        (identity, event, json.dumps(payload, ensure_ascii=False), stamp, stamp),
    )
    return identity


def send_smtp(config, row):
    payload = json.loads(row["payload"])
    message = EmailMessage()
    message["From"], message["To"] = config.mail_from, config.mail_to
    message["Subject"] = "Buggcraft Translate · " + (
        "邮件服务测试" if payload.get("test") else "新的开发者接入申请"
    )
    message["Date"] = formatdate(row["created"], usegmt=True)
    message["Message-ID"] = "<" + row["id"] + "@bugg.icu>"
    lines = [
        "邮件服务测试成功。"
        if payload.get("test")
        else "有新的开发者申请，请到管理后台审核。"
    ]
    for name, label in (
        ("application_id", "申请编号"),
        ("name", "应用名称"),
        ("contact", "联系方式"),
        ("website", "网站"),
        ("purpose", "用途"),
    ):
        if name in payload:
            lines.append(label + "：" + payload[name])
    lines.append("管理后台：" + config.public_url.rstrip("/") + "/admin")
    message.set_content("\n\n".join(lines))
    context = ssl.create_default_context()
    if config.smtp_security == "ssl":
        connection = smtplib.SMTP_SSL(
            config.smtp_host,
            config.smtp_port,
            timeout=config.mail_timeout,
            context=context,
        )
    else:
        connection = smtplib.SMTP(
            config.smtp_host, config.smtp_port, timeout=config.mail_timeout
        )
    try:
        connection.ehlo()
        if config.smtp_security == "starttls":
            connection.starttls(context=context)
            connection.ehlo()
        connection.login(config.smtp_username, config.smtp_password)
        refused = connection.send_message(message)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
        # Acceptance of DATA is authoritative; a failed QUIT must not resend mail.
    finally:
        connection.close()


class MailService:
    def __init__(self, config, store, sender=send_smtp):
        self.config, self.store, self.sender = config, store, sender
        self.wake = asyncio.Event()
        self.task = None

    async def start(self):
        self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def deliver_one(self):
        if not self.config.mail_enabled:
            return False
        async with self.store.transaction() as db:
            async with db.execute(
                "SELECT * FROM mail_outbox WHERE state='pending' AND next_attempt<=? ORDER BY created LIMIT 1",
                (time.time(),),
            ) as cursor:
                value = await cursor.fetchone()
            if not value:
                return False
            row = dict(value)
            await db.execute(
                "UPDATE mail_outbox SET state='sending',attempt=attempt+1,updated=? WHERE id=?",
                (time.time(), row["id"]),
            )
        try:
            await asyncio.to_thread(self.sender, replace(self.config), row)
        except Exception as error:
            # Never persist provider error strings: they may include addresses or credentials.
            attempt = row["attempt"] + 1
            code = (
                "smtp_" + str(error.smtp_code)
                if isinstance(error, smtplib.SMTPResponseException)
                else type(error).__name__
            )
            async with self.store.transaction() as db:
                await db.execute(
                    "UPDATE mail_outbox SET state=?,next_attempt=?,updated=?,error=? WHERE id=?",
                    (
                        "failed" if attempt >= 5 else "pending",
                        time.time() + min(3600, 30 * 4 ** (attempt - 1)),
                        time.time(),
                        code,
                        row["id"],
                    ),
                )
        else:
            async with self.store.transaction() as db:
                await db.execute(
                    "UPDATE mail_outbox SET state='sent',sent_at=?,updated=?,error=NULL WHERE id=?",
                    (time.time(), time.time(), row["id"]),
                )
        return True

    async def run(self):
        while True:
            self.wake.clear()
            try:
                if await self.deliver_one():
                    continue
            except Exception:
                # A transient database failure must not permanently stop notification delivery.
                pass
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=10)
            except TimeoutError:
                pass
