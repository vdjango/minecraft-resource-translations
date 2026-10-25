"use strict";
const $=id=>document.getElementById(id);
const labels={pending:"等待管理员审批",active:"已批准，可以接入",rejected:"申请已驳回",revoked:"接入密钥已撤销"};
function explain(code){return ({application_daily_limit:"今天的申请次数已用完（每天最多 3 次），请明天再试。",invalid_request:"申请信息不完整或长度超出限制，请检查后重试。",rate_limited:"请求过于频繁，请稍后重试。",not_found:"找不到对应申请或接入密钥。"})[code]||"请求未能完成，请稍后重试。";}
$("application").onsubmit=async e=>{
  e.preventDefault(); const form=e.currentTarget,button=form.querySelector("button"); button.disabled=true;
  try{
    const r=await fetch("/v1/clients/applications",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:$("name").value,contact:$("contact").value,website:$("website").value,purpose:$("purpose").value})});
    const d=await r.json(); if(!r.ok)throw Error(explain(d.code));
    $("issued-key").textContent=d.access_token; $("result").classList.remove("hidden"); $("status-key").value=d.access_token;
    $("message").textContent="申请已提交，等待管理员审批。请立即保存只显示一次的接入密钥。";
  }catch(error){$("message").textContent=error.message;}
  finally{button.disabled=false;}
};
$("status-form").onsubmit=async e=>{
  e.preventDefault();const input=$("status-key"),key=input.value;input.value="";
  try{
    const r=await fetch("/v1/clients/application",{headers:{Authorization:"Bearer "+key}}),d=await r.json();if(!r.ok)throw Error(explain(d.code));
    const status=d.state,active=status==="active";
    $("application-status").textContent=(labels[status]||"状态暂不可用")+"。"+(d.notice||"")+(active?` 当前额度：每小时 ${d.fields_per_hour.toLocaleString("zh-CN")} 个翻译字段，每分钟 ${d.requests_per_minute.toLocaleString("zh-CN")} 次请求。`:"");
  }catch(error){$("application-status").textContent=error.message;}
};
