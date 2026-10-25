"use strict";

const mailForm = document.createElement("form");
mailForm.id = "mail-settings";
mailForm.className = "mail-grid";
const mailHeading = document.createElement("h2");
mailHeading.textContent = "申请通知邮件";
const mailFields = [
  ["mail_enabled", "邮件通知", "select"],
  ["smtp_host", "SMTP 服务器", "text"],
  ["smtp_port", "SMTP 端口", "number"],
  ["smtp_security", "加密方式", "select"],
  ["smtp_username", "SMTP 用户名", "text"],
  ["smtp_password", "替换 SMTP 授权码（留空保留）", "password"],
  ["mail_from", "发件邮箱", "email"],
  ["mail_to", "通知收件邮箱", "email"],
  ["mail_timeout", "SMTP 超时（秒）", "number"],
];
for (const [name, title, type] of mailFields) {
  const label = node("label", title);
  const field = document.createElement(type === "select" ? "select" : "input");
  field.id = "mail-" + name;
  if (type === "select") {
    const values = name === "mail_enabled" ? [["false", "关闭"], ["true", "开启"]] : [["ssl", "SSL / TLS"], ["starttls", "STARTTLS"]];
    for (const [value, text] of values) {
      const option = node("option", text); option.value = value; field.append(option);
    }
  } else { field.type = type; }
  if (type === "password") field.autocomplete = "new-password";
  label.append(field); mailForm.append(label);
}
const mailSecret = node("p", ""); mailSecret.id = "mail-secret-state";
const saveMail = node("button", "保存邮件配置"); saveMail.type = "submit";
const testMail = node("button", "发送测试邮件"); testMail.type = "button";
const refreshMail = node("button", "刷新发送状态"); refreshMail.type = "button";
const mailStatus = node("pre", ""); mailStatus.id = "mail-status";
const mailActions = node("div", ""); mailActions.className = "action-row";
testMail.className = refreshMail.className = "secondary";
mailActions.append(saveMail, testMail, refreshMail);
mailForm.append(mailSecret, mailActions, mailStatus);
document.getElementById("mail-form-host").append(mailForm);

async function refreshMailStatus() {
  const data = await api("v1/admin/mail");
  const labels = {pending:"等待发送 / 自动重试", sending:"发送中", sent:"SMTP 已接受", failed:"发送失败（已重试五次）"};
  mailStatus.textContent = data.messages.length ? data.messages.map(m => `${new Date(m.created * 1000).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai"})} · ${labels[m.state] || "状态未知"} · 第 ${m.attempt} 次${m.error ? " · " + ({connection:"连接失败",authentication:"认证失败",recipient:"收件地址被拒绝",timeout:"发送超时"}[m.error] || "需要检查邮件服务配置") : ""}`).join("\n") : "暂无邮件";
}
async function loadMailSettings(settings) {
  for (const [name] of mailFields) if (name !== "smtp_password") $("mail-" + name).value = String(settings[name]);
  mailSecret.textContent = settings.smtp_password_configured ? "SMTP 授权码已配置，读取时不会回显。" : "尚未配置 SMTP 授权码。";
  await refreshMailStatus();
}
mailForm.onsubmit = async event => {
  event.preventDefault();
  const data = {};
  for (const [name, , type] of mailFields) {
    const value = $("mail-" + name).value;
    if (name === "smtp_password" && !value) continue;
    data[name] = name === "mail_enabled" ? value === "true" : type === "number" ? Number(value) : value;
  }
  try {
    const result = await api("v1/admin/settings", data);
    $("mail-smtp_password").value = "";
    await loadMailSettings(result);
    message("邮件配置已保存，新申请将在后台自动通知。");
  } catch (error) { showError(error); }
};
testMail.onclick = async () => {
  testMail.disabled = true;
  try { await api("v1/admin/mail", {action:"test"}); message("测试邮件已加入队列，请刷新发送状态。"); await refreshMailStatus(); }
  catch (error) { showError(error); }
  finally { testMail.disabled = false; }
};
refreshMail.onclick = () => refreshMailStatus().catch(showError);
