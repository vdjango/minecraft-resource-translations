"use strict";
const trafficEndpointNames = {resolve:"查询译文",ensure:"申请翻译",catalogue:"资源列表 / 搜索",artifact:"下载译文正文",manifest:"项目译文状态",source:"查看原文",refresh:"收录 / 检查更新",job:"任务进度",events:"任务事件订阅",history:"项目更新历史",other:"其他请求"};
const percent = value => value == null ? "—" : (value * 100).toFixed(1) + "%";
function trafficLatency(value) { return !value?.samples ? "暂无样本" : value.p95_le_ms == null ? "> 5,000 ms" : "≤ " + number(value.p95_le_ms) + " ms"; }
function trafficRows(host, rows, columns, empty) {
  host.replaceChildren();
  if (!rows.length) { const p=node("p",empty);p.className="record-empty";host.append(p);return; }
  for (const row of rows) {
    const article=node("article","");article.className="traffic-data-row";
    for (const column of columns) { const cell=node("div","");cell.className="record-cell";cell.dataset.label=column.label;cell.append(node("strong",column.value(row)));if(column.note)cell.append(node("small",column.note(row)));article.append(cell); }
    host.append(article);
  }
}
function trafficChart(host, points, label) {
  host.replaceChildren();
  const maximum=Math.max(1,...points.map(point=>point.requests));
  const chart=node("div","");chart.className="traffic-bars";chart.style.setProperty("--columns",points.length);chart.setAttribute("role","img");chart.setAttribute("aria-label",label + "，共 " + number(points.reduce((sum,point)=>sum+point.requests,0)) + " 次请求");
  for(const point of points){const slot=node("div","");slot.className="traffic-bar-slot";slot.title=point.label+" · "+number(point.requests)+" 次请求";const bar=node("span","");bar.className="traffic-bar";bar.style.height=(point.requests/maximum*100)+"%";if(!point.requests)bar.classList.add("empty");slot.append(bar);chart.append(slot);}
  const ticks=node("div","");ticks.className="traffic-chart-ticks";for(const index of [...new Set([0,Math.floor((points.length-1)/2),points.length-1])])ticks.append(node("span",points[index].label));
  host.append(chart,ticks);
}
function renderTrafficSummary(data) {
  const s=data.today;
  metrics($("traffic-summary"),[["今日服务请求",number(s.requests),"仅统计公共翻译 API"],["启动器实例 · 含估算",number(s.launcher_instances),"上报 "+number(s.reported_instances)+" · 估算 "+number(s.estimated_instances)],["今日访问应用",number(s.active_integrations),"按独立应用密钥去重"],["译文字段命中率",percent(s.field_hit_ratio),number(s.ready_fields)+" / "+number(s.fields)+" 个查询字段"]]);
}
function renderTraffic(data) {
  const s=data.today, resolve=data.endpoints.find(item=>item.endpoint==="resolve");
  metrics($("traffic-metrics"),[["今日服务请求",number(s.requests),"北京时间 "+data.day],["启动器实例 · 含估算",number(s.launcher_instances),"上报 "+number(s.reported_instances)+" · 估算 "+number(s.estimated_instances)],["今日访问应用",number(s.active_integrations),"按独立应用密钥去重"],["今日翻译查询",number(resolve?.requests),number(s.fields)+" 个字段"],["译文字段命中率",percent(s.field_hit_ratio),number(s.ready_fields)+" 个字段可直接展示"],["今日模型调用",number(data.model.calls),"成功 "+number(data.model.succeeded)+" · 失败 "+number(data.model.failed)],["译文查询 p95",trafficLatency(resolve?.latency),"服务端分桶上界 · 20ms 内 "+percent(resolve?.latency.within_20ms_ratio)],["今日请求错误",number(s.errors),"其中限流 "+number(s.throttled)+" 次"]]);
  const hourMap=new Map(data.hourly.map(item=>[new Date(item.hour*1000).toLocaleString("en-GB",{timeZone:"Asia/Shanghai",hour:"2-digit",hour12:false}),item.requests]));
  trafficChart($("traffic-hourly"),Array.from({length:24},(_,hour)=>({label:String(hour).padStart(2,"0")+":00",requests:hourMap.get(String(hour).padStart(2,"0"))||0})),"今日每小时请求");
  const dailyMap=new Map(data.daily.map(item=>[item.day,item.requests]));
  const end=Date.parse(data.day+"T00:00:00+08:00");
  const days=Array.from({length:data.days},(_,index)=>{const day=new Date(end-(data.days-index-1)*86400000).toLocaleDateString("en-CA",{timeZone:"Asia/Shanghai"});return {label:day.slice(5),requests:dailyMap.get(day)||0};});
  trafficChart($("traffic-daily"),days,"所选日期请求趋势");
  trafficRows($("traffic-endpoints"),data.endpoints,[{label:"接口",value:item=>trafficEndpointNames[item.endpoint]||"其他请求"},{label:"请求",value:item=>number(item.requests)},{label:"错误",value:item=>number(item.errors)},{label:"p95",value:item=>trafficLatency(item.latency)}],"暂无公共 API 请求，接入方访问后将自动显示。");
  trafficRows($("traffic-clients"),data.top_clients,[{label:"应用",value:item=>item.name},{label:"请求",value:item=>number(item.requests)},{label:"错误",value:item=>number(item.errors)},{label:"命中字段",value:item=>number(item.ready_fields)+" / "+number(item.fields)}],"暂无使用应用密钥访问的启动器。");
  $("traffic-health").textContent=data.aggregation.degraded ? "统计暂未完全落库，系统正在重试；译文查询不受影响。" : "已更新 · "+new Date(data.generated_at).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",hour12:false})+" · 每 "+data.aggregation.interval_seconds+" 秒聚合，保留 "+data.aggregation.retention_days+" 天";
  if(data.aggregation.omitted_visitor_events)$("traffic-health").textContent+=" · 部分访客标识因容量限制未计入";
  $("traffic-coverage").textContent="统计从 "+new Date(data.collection_started_at).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",hour12:false})+" 开始，启用前的访问不补算。";
}
let trafficRequest=0;
async function loadTraffic() {
  const version=++trafficRequest,button=$("refresh-traffic");if(button)button.disabled=true;
  try {const data=await api("v1/admin/traffic?days="+($("traffic-days")?.value||"1"));if(version!==trafficRequest)return;if($("traffic-summary"))renderTrafficSummary(data);if($("traffic-dashboard"))renderTraffic(data);}
  finally{if(button&&version===trafficRequest)button.disabled=false;}
}
window.addEventListener("DOMContentLoaded",()=>{if($("refresh-traffic"))$("refresh-traffic").onclick=()=>loadTraffic().catch(showError);if($("traffic-days"))$("traffic-days").onchange=()=>loadTraffic().catch(showError);});
