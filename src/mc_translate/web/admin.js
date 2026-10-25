"use strict";
const $ = id => document.getElementById(id);
let token = "", currentModel = "", loadedBase = "", prices = {};
function node(tag, text) { const e = document.createElement(tag); e.textContent = text; return e; }
const errors = {unauthorized:"管理会话已过期，请重新登录。", invalid_settings:"配置格式有误，请检查表单；三项价格需一起填写或全部留空。", model_list_unavailable:"服务商模型列表读取失败，请稍后重试。", model_key_required:"更换服务商后，请填写对应 API Key 再读取模型。", model_not_configured:"请先填写服务商 API Key。", mail_disabled:"请先保存并开启邮件通知。", rate_limited:"操作过于频繁，请稍后再试。", cursor_expired:"分页已过期，请刷新列表。"};
async function api(path, body, method) {
  const response = await fetch("/" + path.replace(/^\//,""), {method:method || (body ? "POST" : "GET"), credentials:"same-origin", headers:{...(token ? {Authorization:"Bearer " + token} : {}), "Content-Type":"application/json"}, ...(body ? {body:JSON.stringify(body)} : {})});
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401 && !token) authenticated(false);
    const error=Error(errors[data.code] || ({task_not_found:"任务不存在，请刷新列表。",task_not_retryable:"该任务当前无需重试，请刷新查看。",queue_full:"队列已满，请稍后重试。"})[data.code] || "操作失败，请稍后重试。");
    error.status=response.status;error.code=data.code;throw error;
  }
  return data;
}
function message(text, error=false) { $("message").textContent = text; $("message").classList.toggle("error", error); }
function showError(error) { message(error.message, true); }
const stateNames = {pending:"等待审批", active:"已启用", rejected:"已驳回", revoked:"已撤销", queued:"等待翻译", running:"翻译中", ready:"成功", failed:"失败", deferred:"延后重试", blocked:"已阻止", partial:"部分完成", verifying:"核验中", succeeded:"成功", unknown:"结果未确认"};
const fieldNames = {summary:"简介", body:"正文", title:"标题", refresh:"原文检查"};
const errorNames = {budget_exhausted:"额度已用尽，保留进度等待恢复", model_rate_limited:"模型服务限流", model_unavailable:"模型服务暂时不可用", model_invalid_output:"模型输出不符合要求", model_timeout:"模型请求超时", source_unavailable:"资源来源暂时不可用", source_not_found:"源资源不存在", source_changed:"原文发生更新"};
const number = value => Number(value || 0).toLocaleString("zh-CN");
const money = value => value == null ? "费用未知" : "$" + Number(value).toFixed(6);
const date = stamp => new Date(stamp * 1000).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",hour12:false});
function metrics(host, items) {
  host.replaceChildren();
  for (const [label,value,note] of items) { const card=node("div",""); card.className="metric"; card.append(node("span",label),node("strong",value),node("small",note)); host.append(card); }
}
function pauseText(pause) { return pause ? (pause.reason === "monthly_budget" ? "本月额度已用尽" : "今日额度已用尽") + "，生成任务已暂停。将在 " + date(pause.resume_at) + " 自动重试；已完成的译文仍可读取。" : "队列正常运行。预算按北京时间零点重置；未回报用量的请求按预留 Token 保守计入额度。"; }
function renderOverview(data) {
  if ($("metrics")) {
    metrics($("metrics"), [["今日 Token",number(data.usage.tokens),"每日上限 " + number(data.budgets.daily_tokens)], ["今日模型调用",number(data.usage.calls),"成功 " + number(data.usage.succeeded) + " · 失败 " + number(data.usage.failed)], ["今日费用",data.usage.cost_unknown ? "部分未知" : money(data.usage.cost),number(data.usage.cost_unknown) + " 次费用未知"], ["本月 Token",number(data.usage.monthly.tokens),"本月已知费用 " + money(data.usage.monthly.cost)]]);
    $("task-states").replaceChildren();
    for (const task of data.tasks) { const chip=node("span",stateNames[task.state] || "处理中"); chip.className="state-chip"; chip.append(node("strong",number(task.count))); $("task-states").append(chip); }
    if (!data.tasks.length) $("task-states").append(node("span","暂无翻译任务"));
    $("budget-notice").textContent=pauseText(data.pause);
  }
  if (!$("feedback")) return;
  $("feedback").replaceChildren();
  const feedbackStates={pending_review:"待处理",acknowledged:"已确认",rejected:"已驳回",resolved:"已解决"};
  for (const item of data.feedback) {
    const card=node("article", ""); card.className="admin-feedback";
    card.append(node("h3",item.data.resource.provider + " / " + item.data.resource.project_id),node("p",item.data.message),node("p",feedbackStates[item.state] || "待处理"));
    const actions=node("div",""); actions.className="action-row";
    for (const [state,label] of [["acknowledged","确认收到"],["rejected","驳回"],["resolved","标记已解决"]]) { const button=node("button",label); button.className="secondary"; button.onclick=() => api("v1/admin/overview",{feedback_id:item.id,state}).then(renderOverview).catch(showError); actions.append(button); }
    card.append(actions); $("feedback").append(card);
  }
  if (!data.feedback.length) $("feedback").append(node("p","暂无译文反馈。"));
}
function loadPrices() { const p=prices[$("model").value]; for (const [id,key] of [["price-input","input"],["price-cached","cached_input"],["price-output","output"]]) $(id).value=p ? p[key] : ""; }
function modelOptions(models, selected) {
  $("model").replaceChildren();
  const values=[...new Set([...(selected ? [selected] : []), ...models])];
  for (const id of values) { const option=node("option",id + (id === currentModel ? " · 当前配置" : "")); option.value=id; $("model").append(option); }
  $("model").value=selected || values[0] || ""; loadPrices();
}
async function refreshModels(preview=false) {
  $("refresh-models").disabled=true; $("model-state").textContent="正在读取服务商模型…";
  try {
    const body=preview ? {base_url:$("base-url").value.trim(), ...($("api-key").value ? {api_key:$("api-key").value} : {})} : undefined;
    const data=await api("v1/admin/models",body), selected=$("model").value;
    const sameProvider=!preview || $("base-url").value.trim() === loadedBase;
    modelOptions(data.models, sameProvider && selected ? selected : data.models[0]);
    $("model-state").textContent=`已读取 ${data.models.length} 个模型。`;
  } catch (error) { $("model-state").textContent=error.message + " 当前保存的模型仍可使用。"; }
  finally { $("refresh-models").disabled=false; }
}
let clientList, taskList, usageList;
if ($("clients")) clientList = new RecordList("clients", clientRow, async () => renderClients(await api("v1/admin/clients?after=" + encodeURIComponent(clientList.cursor)), true));
if ($("tasks")) taskList = new RecordList("tasks", taskRow, () => loadTasks(true));
if ($("usage-calls")) usageList = new RecordList("usage-calls", usageRow, () => loadUsage(true));
function renderClients(data, append=false) {
  clientList.set(data.clients, data.next_cursor, append);
  $("client-count").textContent = "已读取 " + number(clientList.records.length) + " 条";
}
async function changeClient(client,state) {
  if (state === "active") {
    $("review-id").value=client.id; $("review-name").textContent=client.name;
    $("review-fields").value=client.fields_per_hour; $("review-rpm").value=client.requests_per_minute; $("review-dialog").showModal(); return;
  }
  if (!confirm((state === "revoked" ? "撤销" : "驳回") + "「" + client.name + "」的接入密钥？")) return;
  renderClients(await api("v1/admin/clients",{id:client.id,state})); message("申请状态已更新。");
}
let taskRequest = 0, usageRequest = 0;
async function loadTasks(append=false) {
  for(const button of document.querySelectorAll("[data-task-state]"))button.setAttribute("aria-pressed",String(button.dataset.taskState===$("task-filter").value));
  const version = append ? taskRequest : ++taskRequest;
  const data = await api("v1/admin/tasks?state=" + encodeURIComponent($("task-filter").value) + (append && taskList.cursor ? "&cursor=" + encodeURIComponent(taskList.cursor) : ""));
  if (version !== taskRequest) return;
  $("task-budget").textContent = data.pause ? pauseText(data.pause) : "队列正常运行 · 额度按北京时间零点重置";
  $("task-budget").classList.toggle("paused", Boolean(data.pause));
  taskList.set(data.tasks, data.next_cursor, append);
}
async function loadUsage(append=false) {
  const version = append ? usageRequest : ++usageRequest;
  const data = await api("v1/admin/usage?period=" + $("usage-period").value + "&status=" + encodeURIComponent($("usage-filter").value) + (append && usageList.cursor ? "&cursor=" + encodeURIComponent(usageList.cursor) : ""));
  if (version !== usageRequest) return;
  const s = data.summary;
  metrics($("usage-summary"), [["Token 总量",number(s.tokens),"包含未确认调用的预留量"], ["成功 / 失败",number(s.succeeded) + " / " + number(s.failed),number(s.unconfirmed) + " 次进行中或未确认"], ["已知费用 · USD",money(s.cost),number(s.cost_unknown) + " 次费用未知"], ["模型调用",number(s.calls),data.key + " · 北京时间"]]);
  usageList.set(data.calls, data.next_cursor, append);
}
async function load() {
  if ($("dashboard") || $("feedback")) renderOverview(await api("v1/admin/overview"));
  if ($("traffic-dashboard") || $("traffic-summary")) await loadTraffic();
  if ($("federation-nodes")) await loadNodes();
  if ($("clients")) renderClients(await api("v1/admin/clients"));
  if ($("tasks")) await loadTasks();
  if ($("usage-calls")) await loadUsage();
  if ($("settings") || $("mail-form-host")) {
    const settings=await api("v1/admin/settings");
    if ($("settings")) {
      loadedBase=settings.base_url; currentModel=settings.model; prices=settings.model_prices || {};
      $("base-url").value=loadedBase; $("effort").value=settings.reasoning_effort;
      modelOptions([],currentModel);
      for (const [id,name] of [["daily-tokens","daily_token_limit"],["monthly-tokens","monthly_token_limit"],["daily-calls","daily_call_limit"],["completion-tokens","max_completion_tokens"],["timeout","model_timeout"]]) $(id).value=settings[name];
      $("key-state").textContent=settings.api_key_configured ? "API Key 已配置，填写新值即可替换。" : "尚未配置 API Key。";
      void refreshModels();
    } else await loadMailSettings(settings);
  }
  message($("tasks") || $("usage-calls") || $("clients") ? "" : "管理数据已更新。");
}
function authenticated(value) { $("session-loading").classList.add("hidden"); $("panel").classList.toggle("hidden",!value); $("login-card").classList.toggle("hidden",value); $("logout").classList.toggle("hidden",!value); }
$("login").onsubmit=async event => {
  event.preventDefault(); token=$("token").value; $("token").value="";
  try {await api("v1/admin/session",{}); token=""; authenticated(true); await load();}
  catch(error) {const loginFailed=Boolean(token); token=""; if(loginFailed || error.status===401)authenticated(false); showError(error);}
};
$("logout").onclick=async () => {try {await api("v1/admin/session",undefined,"DELETE"); token=""; location.reload();} catch(error) {showError(error);} };
if ($("refresh-dashboard")) $("refresh-dashboard").onclick=async () => {try{renderOverview(await api("v1/admin/overview"));if($("traffic-summary"))await loadTraffic();}catch(error){showError(error);}};
if ($("refresh-tasks")) { $("refresh-tasks").onclick=() => loadTasks().catch(showError); $("task-filter").onchange=() => loadTasks().catch(showError); }
for(const button of document.querySelectorAll("[data-task-state]"))button.onclick=()=>{$("task-filter").value=button.dataset.taskState;loadTasks().catch(showError);};
if ($("refresh-usage")) { $("refresh-usage").onclick=() => loadUsage().catch(showError); $("usage-period").onchange=() => loadUsage().catch(showError); $("usage-filter").onchange=() => loadUsage().catch(showError); }
if ($("refresh-clients")) $("refresh-clients").onclick=() => api("v1/admin/clients").then(renderClients).catch(showError);
if ($("review-cancel")) $("review-cancel").onclick=() => $("review-dialog").close();
if ($("review-form")) $("review-form").onsubmit=async event => {
  event.preventDefault();
  try { renderClients(await api("v1/admin/clients",{id:$("review-id").value,state:"active",fields_per_hour:Number($("review-fields").value),requests_per_minute:Number($("review-rpm").value)})); $("review-dialog").close(); message("密钥已启用，限额已保存。"); } catch(error) {showError(error);}
};
if ($("settings")) {
  $("refresh-models").onclick=() => refreshModels(true); $("model").onchange=loadPrices;
  $("settings").onsubmit=async event => {
    event.preventDefault(); const button=event.submitter; button.disabled=true;
    const data={base_url:$("base-url").value.trim(), model:$("model").value, reasoning_effort:$("effort").value, daily_token_limit:Number($("daily-tokens").value), monthly_token_limit:Number($("monthly-tokens").value), daily_call_limit:Number($("daily-calls").value), max_completion_tokens:Number($("completion-tokens").value), model_timeout:Number($("timeout").value)};
    if ($("api-key").value) data.api_key=$("api-key").value;
    try {
      const values=[$("price-input").value,$("price-cached").value,$("price-output").value];
      if (values.some(v => v !== "") && values.some(v => v === "")) throw Error("请填写全部三项价格，或全部留空。");
      const updatedPrices={...prices};
      if (values.every(v => v === "")) delete updatedPrices[data.model];
      else updatedPrices[data.model]={input:Number(values[0]),cached_input:Number(values[1]),output:Number(values[2])};
      data.model_prices=updatedPrices;
      const result=await api("v1/admin/settings",data); $("api-key").value=""; currentModel=result.model; loadedBase=result.base_url; prices=result.model_prices;
      $("key-state").textContent=result.api_key_configured ? "已保存；密钥不会回显。" : "尚未配置 API Key。"; message("模型配置已更新。");
    } catch(error) {showError(error);} finally {button.disabled=false;}
  };
}
async function bootstrap() {
  $("login-card").classList.add("hidden");$("panel").classList.add("hidden");$("session-loading").classList.remove("hidden");$("retry-session").classList.add("hidden");
  $("session-state").textContent="正在恢复管理会话…";
  try { await api("v1/admin/session");authenticated(true); }
  catch(error) {
    if(error.status===401){authenticated(false);return;}
    $("session-state").textContent="暂时无法连接管理服务，请重试。";$("retry-session").classList.remove("hidden");return;
  }
  try { await load(); } catch(error) { showError(error); }
}
async function retryTask(task, button) {
  if(button.disabled)return;button.disabled=true;button.textContent="正在提交…";
  try {
    const data=await api("v1/admin/tasks/"+encodeURIComponent(task.id)+"/retry",{});
    task.state=data.state;task.attempt=0;task.completed=0;task.total=null;task.error=null;
    const dialog=$("record-dialog");if(dialog?.open)dialog.close();
    await loadTasks();message(data.state==="deferred"?"任务已安排重试，等待额度恢复或冷却结束。":"重试已提交，已完成的翻译片段会继续复用。");
  }catch(error){showError(error);}finally{button.disabled=false;button.textContent="重试";}
}
// Authenticate separately from data loading: a slow/failed data request is not a logout.
window.addEventListener("DOMContentLoaded", () => { $("retry-session").onclick=bootstrap;void bootstrap(); });
