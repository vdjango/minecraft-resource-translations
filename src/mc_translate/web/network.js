"use strict";
function renderNetwork(){const host=document.getElementById("network-nodes");host.replaceChildren();const network=window.translationNetwork;for(const item of network.candidates){const card=document.createElement("article");card.className="network-node"+(item.id===network.selectedId?" selected":"");for(const [tag,text]of[["h3",item.name],["p",item.role==="mirror"?"社区镜像 · "+item.region:"主服务 · 始终作为回退"],["p",item.base_url],["strong",item.rtt.toFixed(0)+" ms"],["p",item.id===network.selectedId?"当前使用":"可用节点"]]){const element=document.createElement(tag);element.textContent=text;card.append(element);}host.append(card);}document.getElementById("network-notice").textContent=network.candidates.length?"已完成测速，当前选择 "+network.selected:"暂时未取得测速结果，资源访问将使用主服务。";}
window.addEventListener("translation-node-changed",renderNetwork);
window.addEventListener("DOMContentLoaded",()=>{
  const button=document.getElementById("test-nodes"),notice=document.getElementById("network-notice");
  const test=async()=>{if(button.disabled)return;button.disabled=true;button.textContent="正在测速…";notice.textContent="正在检测可用节点，请稍候…";
    try{await window.translationNetwork.ready;await window.translationNetwork.select();renderNetwork();}
    catch(error){notice.textContent="测速暂未完成，可重新测试；资源仍可通过主服务访问。";}
    finally{button.disabled=false;button.textContent="重新测速";}
  };
  button.onclick=test;void test();
});
