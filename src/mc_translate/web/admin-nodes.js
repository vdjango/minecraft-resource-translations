"use strict";
async function loadNodes() {
  const data=await api("v1/admin/nodes"),host=$("federation-nodes");host.replaceChildren();
  $("node-role").textContent=data.role==="mirror"?"当前是社区镜像 · 主服务 "+data.hub_url+" · "+({pending:"等待审批",active:"已启用",unavailable:"暂时无法连接"}[data.local_state]||"等待连接"):"当前是主服务 · 审批通过且在线的节点才会进入自动分发。";
  const online=data.nodes.filter(item=>item.online).length;
  metrics($("node-metrics"),[["在线节点",number(online),"心跳 90 秒与探测 120 秒内"],["等待审批",number(data.nodes.filter(item=>item.state==="pending").length),"批准前验证节点地址所有权"],["节点今日请求",number(data.nodes.reduce((sum,item)=>sum+(item.stats.day===new Date().toLocaleDateString("en-CA",{timeZone:"Asia/Shanghai"})?item.stats.requests||0:0),0)),"节点汇总上报，与主站回源分开"],["节点缓存命中",number(data.nodes.reduce((sum,item)=>sum+(item.stats.day===new Date().toLocaleDateString("en-CA",{timeZone:"Asia/Shanghai"})?item.stats.cache_hits||0:0),0)),"不包含 CDN 与启动器本地缓存"]]);
  for(const item of data.nodes){
    const row=node("article","");row.className="node-record";
    const text=node("div","");text.className="node-record-main";text.append(node("strong",item.name),node("small",item.public_url),node("small",item.region+" · 联系方式："+item.contact));
    const status=node("div","");status.className="node-record-state";const badge=node("span",item.online?"在线":item.state==="pending"?"等待审批":item.state==="revoked"?"已撤销":"离线");badge.className="status-badge "+(item.online?"active":item.state==="revoked"?"failed":"pending");status.append(badge,node("small",item.last_seen?"心跳 "+date(item.last_seen):"尚未收到心跳"));
    const actions=node("div","");actions.className="action-row";
    const review=node("button",item.state==="active"?"撤销节点":"验证并启用");review.className="secondary";review.onclick=async()=>{review.disabled=true;try{await api("v1/admin/nodes",{id:item.id,state:item.state==="active"?"revoked":"active"});await loadNodes();message(item.state==="active"?"节点已退出分发列表。":"节点已启用，等待下一次心跳后开始分流。");}catch(error){message(error.code==="node_verification_failed"?"节点验证失败，请确认域名和 HTTPS 可访问，且已连接当前主服务。":error.message,true);}finally{review.disabled=false;}};actions.append(review);row.append(text,status,actions);host.append(row);
  }
  if(!data.nodes.length){const empty=node("p",data.role==="mirror"?"此页面只管理本地节点；其他节点由主服务审批。":"暂无社区节点申请，部署者按加入指南启动后会自动出现在这里。");empty.className="record-empty";host.append(empty);}
}
window.addEventListener("DOMContentLoaded",()=>{if($("refresh-nodes"))$("refresh-nodes").onclick=()=>loadNodes().catch(showError);});
