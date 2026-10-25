"use strict";
// Shared presentation for the three record pages. Only the current page enters the DOM.
class RecordList {
  constructor(id, renderRow, fetchMore) {
    this.host = $(id); this.renderRow = renderRow; this.fetchMore = fetchMore;
    this.records = []; this.cursor = null; this.page = 0; this.size = 20; this.busy = false; this.epoch = 0;
    this.footer = node("div", ""); this.footer.className = "record-pagination";
    this.info = node("span", "正在读取记录…"); this.info.setAttribute("aria-live", "polite");
    const controls = node("div", ""); controls.className = "pager-controls";
    this.select = node("select", ""); this.select.setAttribute("aria-label", "每页记录数");
    for (const count of [20, 50]) { const option = node("option", `每页 ${count} 条`); option.value = count; this.select.append(option); }
    this.previous = node("button", "上一页"); this.next = node("button", "下一页");
    for (const button of [this.previous, this.next]) { button.type = "button"; button.className = "secondary"; }
    this.previous.onclick = () => { this.page--; this.render(); };
    this.next.onclick = () => this.forward().catch(showError);
    this.select.onchange = async () => {
      this.size = Number(this.select.value); this.page = 0; this.render();
      if (this.records.length < this.size && this.cursor) {
        this.busy = true; this.render();
        try { await this.fetchMore(); } catch (error) { showError(error); } finally { this.busy = false; this.render(); }
      }
    };
    controls.append(this.select, this.previous, this.next); this.footer.append(this.info, controls); this.host.after(this.footer);
  }
  set(records, cursor, append = false) {
    if (!append) { this.records = []; this.page = 0; this.epoch++; }
    this.records.push(...records); this.cursor = cursor; this.render();
  }
  async forward() {
    if (this.busy) return;
    this.busy = true; const epoch = this.epoch; this.render();
    try {
      if ((this.page + 2) * this.size > this.records.length && this.cursor) await this.fetchMore();
      if (epoch === this.epoch && (this.page + 1) * this.size < this.records.length) this.page++;
    } finally { this.busy = false; this.render(); }
  }
  render() {
    const start = this.page * this.size, end = Math.min(start + this.size, this.records.length);
    this.host.replaceChildren(...this.records.slice(start, end).map(this.renderRow));
    if (!this.records.length) { const empty = node("p", "当前条件下暂无记录。"); empty.className = "record-empty"; this.host.append(empty); }
    this.info.textContent = this.records.length ? `第 ${this.page + 1} 页 · ${start + 1}–${end} 条${this.cursor ? " · 后面还有记录" : " · 共 " + number(this.records.length) + " 条"}` : "暂无记录";
    this.previous.disabled = this.busy || this.page === 0;
    this.next.disabled = this.busy || (end === this.records.length && !this.cursor);
    this.select.disabled = this.busy; this.next.textContent = this.busy ? "读取中…" : "下一页";
  }
}

function recordCell(label, primary, secondary = "", className = "") {
  const cell = node("div", ""); cell.className = "record-cell " + className; cell.dataset.label = label;
  if (primary instanceof Node) cell.append(primary); else cell.append(node("strong", primary));
  if (secondary) { const small = node("small", secondary); small.title = secondary; cell.append(small); }
  return cell;
}
function statusBadge(state, label = stateNames[state] || "未知状态") { const badge = node("span", label); badge.className = "status-badge " + state; badge.dataset.state = state; return badge; }
function recordTime(stamp) {
  const time = new Date(stamp * 1000);
  return [time.toLocaleDateString("zh-CN", {timeZone:"Asia/Shanghai"}), time.toLocaleTimeString("zh-CN", {timeZone:"Asia/Shanghai",hour12:false})];
}
function detailButton(label, action) { const button = node("button", label); button.type = "button"; button.className = "record-detail secondary"; button.onclick = action; return button; }
function showRecord(title, description, fields, actions = []) {
  let dialog = $("record-dialog");
  if (!dialog) { dialog = node("dialog", ""); dialog.id = "record-dialog"; dialog.setAttribute("aria-labelledby", "record-dialog-title"); document.body.append(dialog); }
  const heading = node("div", ""); heading.className = "record-dialog-heading";
  const h2 = node("h2", title); h2.id = "record-dialog-title";
  const close = detailButton("关闭", () => dialog.close()); heading.append(h2, close);
  const intro = node("p", description); intro.className = "muted small";
  const list = node("dl", ""); list.className = "record-details";
  for (const [key, value] of fields) list.append(node("dt", key), node("dd", value == null || value === "" ? "—" : String(value)));
  dialog.replaceChildren(heading, intro, list);
  if (actions.length) { const footer = node("div", ""); footer.className = "record-dialog-actions"; footer.append(...actions); dialog.append(footer); }
  dialog.showModal();
}
function taskRow(task) {
  let language = task.language;
  try { language = new Intl.DisplayNames(["zh-CN"], {type:"language"}).of(task.language) || task.language; } catch (_) {}
  const row = node("article", ""); row.className = "record-row task-row";
  const progress = node("div", ""); progress.className = "task-progress";
  progress.append(node("strong", task.total == null ? (task.state === "ready" ? "已完成" : "等待处理") : `${number(task.completed)} / ${number(task.total)} 段`));
  const track = node("div", ""); track.className = "progress-track";
  const fill = node("span", ""); fill.style.width = (task.state === "ready" ? 100 : task.total ? Math.min(100, task.completed / task.total * 100) : 0) + "%"; track.append(fill); progress.append(track);
  const [day, clock] = recordTime(task.updated);
  const details = detailButton("详情", () => showRecord(task.title || "翻译任务", "任务信息与处理进度", [
    ["资源", task.key], ["翻译字段", fieldNames[task.field] || "资源"], ["目标语言", language],
    ...(task.result_quality === "original" ? [["处理结果", "无需翻译，保留原文"], ["原因", ({project_name:"项目名称保留原名",empty_content:"内容为空",technical_content:"代码或版本信息",already_target_language:"原文已是目标语言",unchanged:"内容无需改动"})[task.no_translation_reason] || "保留原文"]] : []),
    ["状态", stateNames[task.state] || "处理中"], ["分段进度", task.total == null && task.state === "ready" ? "已完成" : `${number(task.completed)} / ${task.total == null ? "待确定" : number(task.total)}`],
    ["已尝试", number(task.attempt) + " 次"], ["创建时间", date(task.created)], ["更新时间", date(task.updated)],
    ...(task.error ? [["处理说明", errorNames[task.error.code] || "处理暂未完成，请检查任务状态。"]] : []),
    ...(task.state === "deferred" ? [["计划重试", date(task.next_attempt)]] : [])
  ], task.state === "failed" ? [detailButton("重试此任务", event => retryTask(task, event.currentTarget))] : []));
  row.append(recordCell("资源 / 字段", task.title || task.key, (fieldNames[task.field] || "资源") + " · " + language + " · " + task.key, "record-main"), recordCell("状态", statusBadge(task.state, task.state === "ready" && task.result_quality === "original" ? "无需翻译" : undefined)), recordCell("翻译进度", progress), recordCell("更新时间", day, clock, "record-time"), recordCell("操作", details, "", "record-actions"));
  if(task.state === "failed")row.querySelector(".record-actions").append(detailButton("重试", event => retryTask(task, event.currentTarget)));
  return row;
}
function usageRow(call) {
  const row = node("article", ""); row.className = "record-row usage-row";
  const [day, clock] = recordTime(call.created);
  const details = detailButton("明细", () => showRecord(call.model || "历史模型未记录", "模型调用与费用明细 · USD", [
    ["调用时间", date(call.created)], ["调用状态", call.status === "pending" ? "调用中" : stateNames[call.status] || "结果未确认"],
    ["Token 总量", number(call.tokens) + (call.actual ? " · 上游已回报" : " · 暂按预留量统计")],
    ["输入 Token", call.details_known ? number(call.input_tokens) : "上游未回报完整明细"],
    ["其中缓存输入", call.details_known ? number(call.cached_tokens) : "未知"], ["输出 Token", call.details_known ? number(call.output_tokens) : "未知"],
    ["估算费用", money(call.cost)], ["费用说明", call.cost == null ? "缺少调用用量明细或模型价格，无法估算。" : "按调用时保存的模型价格估算，最终以服务商账单为准。"]
  ]));
  row.append(recordCell("模型", call.model || "历史模型未记录", "模型调用 #" + call.id, "record-main"), recordCell("状态", statusBadge(call.status, call.status === "pending" ? "调用中" : undefined)), recordCell("Token 总量", number(call.tokens), call.actual ? "已回报用量" : "预留 / 未确认", "record-numeric"), recordCell("费用 · USD", money(call.cost), "", "record-numeric"), recordCell("调用时间", day, clock, "record-time"), recordCell("操作", details, "", "record-actions"));
  return row;
}
function clientRow(client) {
  const row = node("article", ""); row.className = "record-row client-row";
  const [day, clock] = recordTime(client.created);
  const details = detailButton(client.state === "pending" ? "审批" : "管理", () => {
    const actions = [];
    for (const [state, label] of [["active", client.state === "active" ? "调整限额" : "批准申请"], ["rejected", "驳回申请"], ["revoked", "撤销密钥"]]) {
      if (state === client.state && state !== "active") continue;
      const button = detailButton(label, () => { $("record-dialog").close(); changeClient(client, state).catch(showError); });
      if (state === "active") button.classList.remove("secondary");
      if (state === "revoked") button.classList.add("danger");
      actions.push(button);
    }
    showRecord(client.name, "申请详情与接入权限", [["审批状态", stateNames[client.state]], ["联系方式", client.contact], ["项目主页", client.website || "未填写"], ["接入用途", client.purpose], ["密钥前缀", client.prefix], ["申请时间", date(client.created)], ...(client.state === "active" ? [["每小时翻译字段", `${number(client.fields_used_this_hour)} / ${number(client.fields_per_hour)}`], ["每分钟请求上限", number(client.requests_per_minute)]] : [])], actions);
  });
  row.append(recordCell("应用 / 联系方式", client.name, client.contact, "record-main"), recordCell("状态", statusBadge(client.state)), recordCell("接入额度", client.state === "active" ? `${number(client.fields_used_this_hour)} / ${number(client.fields_per_hour)}` : "—", client.state === "active" ? "字段/小时" : client.state === "pending" ? "批准后生效" : "当前未启用", "record-numeric"), recordCell("申请时间", day, clock, "record-time"), recordCell("操作", details, "", "record-actions"));
  return row;
}
