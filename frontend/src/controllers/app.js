"use strict";
const $ = id => document.getElementById(id), api = new URL("v1/", new URL("./", location.href));
const labels = {mod:"模组",resourcepack:"资源包",shader:"光影",modpack:"整合包"};
const states = {missing:"等待翻译",queued:"已加入翻译队列",verifying:"正在核验原文",running:"正在翻译",partial:"正在分段翻译",deferred:"稍后重试",failed:"翻译暂不可用",source_mismatch:"原文已更新"};
const tagLabels = {optimization:"性能优化",adventure:"冒险",decoration:"装饰",equipment:"装备",food:"食物",library:"前置库",magic:"魔法",mobs:"生物",storage:"存储",technology:"科技",utility:"实用工具",worldgen:"世界生成",realistic:"写实",fantasy:"奇幻","vanilla-like":"原版风格",animated:"动画",themed:"主题",challenging:"挑战",combat:"战斗","kitchen-sink":"综合玩法",quests:"任务",multiplayer:"多人",lightweight:"轻量",performance:"性能","API and Library":"API 与前置库"};
const relations = {required:"必需",optional:"可选",incompatible:"不兼容",embedded:"已内置",tool:"工具"};
const md = window.markdownit({html:true,linkify:false});
let targetLanguage="zh-Hans",languageNames={"zh-Hans":"简体中文"};
const languageName=()=>languageNames[targetLanguage]||targetLanguage;
const originalLabel=t=>({project_name:"项目名称，保留原名",empty_content:"暂无正文内容",technical_content:"代码或版本信息，保留原文",already_target_language:"内容已是目标语言",unchanged:"内容无需改动"}[t.no_translation_reason]||"无需翻译，保留原文");
let pageIndex=0,pageCursors=[null],listLoading=false;
let session,sessionPending,sessionUntil=0,kind="",cursor=null,detail=null,detailSource=null,generation=0,listGeneration=0,lastTranslation=null;
const sleep = ms => new Promise(resolve=>setTimeout(resolve,ms));
const json = (data,headers={}) => ({method:"POST",headers:{"Content-Type":"application/json",...headers},body:JSON.stringify(data)});
async function request(path,options={}) {
  // Colons in action names are path characters, never URL protocols.
  const response=await (window.translationNetwork?window.translationNetwork.fetch(path,options):fetch(new URL("./"+path,api),options)), data=await response.json();
  if(!response.ok)throw Error(data.code||"请求失败");return data;
}
async function auth() {
  if(!session||Date.now()>sessionUntil){
    if(!sessionPending)sessionPending=request("sessions/anonymous",json({client_id:"buggcraft-web"})).then(d=>{session=d.access_token;sessionUntil=Date.now()+d.expires_in*1000-60000;}).finally(()=>{sessionPending=null;});
    await sessionPending;
  }
  return {"Content-Type":"application/json",Authorization:"Bearer "+session};
}
function node(tag,text,cls){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;}
function date(value){const d=new Date(value);return value&&!Number.isNaN(d.getTime())?d.toLocaleDateString("zh-CN"):"暂未提供";}
function number(v){return new Intl.NumberFormat("zh-CN",{notation:"compact",maximumFractionDigits:1}).format(Number(v)||0);}
function link(url,text,cls){const e=node("a",text,cls);try{const u=new URL(url);if(u.protocol==="https:"){e.href=u.href;e.target="_blank";e.rel="noopener noreferrer";}}catch(_){}return e;}
function icon(target,url,title){target.replaceChildren(node("span",(title||"B").slice(0,1)));try{if(new URL(url).protocol!=="https:")return;}catch(_){return;}const img=document.createElement("img");img.alt="";img.loading="lazy";img.referrerPolicy="no-referrer";img.onload=()=>target.replaceChildren(img);img.src=url;}
function chips(values,cls="tag"){const box=node("div",undefined,"tag-list");for(const v of values||[])box.append(node("span",tagLabels[v]||v,cls));return box;}
function versions(values){const v=[...(values||[])].sort(new Intl.Collator("en",{numeric:true}).compare).reverse();return v.length?v.slice(0,3).join(" / ")+(v.length>3?` 等 ${v.length} 个版本`:""):"版本信息待补全";}
function authors(m){return(m.authors||[]).map(a=>a.name).join("、")||"作者信息待补全";}
function renderBody(target,text,format="markdown") {
  const html=format==="html"?text:md.render(text);
  // Both original and translated content go through the same sanitizer.
  target.innerHTML=DOMPurify.sanitize(html,{FORBID_TAGS:["style","script","iframe","object","embed","svg","math","form","input","button","video","audio"],FORBID_ATTR:["style","srcset"]});
  for(const img of target.querySelectorAll("img")){img.loading="lazy";img.referrerPolicy="no-referrer";}
  for(const a of target.querySelectorAll("a")){a.target="_blank";a.rel="noopener noreferrer";}
}
pageViews.catalog.mount(resource=>showDetail(resource));
async function load(append=false){
  const token=++listGeneration;listLoading=true;$("more").disabled=true;$("previous-page").disabled=true;$("projects").setAttribute("aria-busy","true");$("notice").textContent="正在读取资源…";
  pageViews.catalog.loading(true);
  try{if(!append){pageIndex=0;pageCursors=[null];}const params=new URLSearchParams({limit:$("page-size").value,kind,q:$("query").value,target_language:targetLanguage});if(pageCursors[pageIndex])params.set("cursor",pageCursors[pageIndex]);const data=await request("catalog/search?"+params);if(token!==listGeneration)return;pageViews.catalog.set(data.projects,languageName());cursor=data.next_cursor;$("more").disabled=!cursor;$("previous-page").disabled=pageIndex===0;$("page-info").textContent=`第 ${pageIndex+1} 页 / 共 ${Math.max(1,Math.ceil(data.total_count/Number($("page-size").value)))} 页`;$("count").textContent=`共 ${data.total_count} 个项目`;$("notice").textContent=data.projects.length?"":"暂无匹配项目，可通过项目 ID 收录资源。";translateVisible(data.projects,token);}catch(e){if(token===listGeneration){if(e.message==="cursor_expired"&&append){load();return;}$("notice").textContent="读取失败："+e.message;}}
  finally{if(token===listGeneration){listLoading=false;pageViews.catalog.loading(false);$("projects").setAttribute("aria-busy","false");$("more").disabled=!cursor;$("previous-page").disabled=pageIndex===0;}}
}
async function translateVisible(projects,token){
  const items=projects.filter(p=>p.field_states?p.field_states.summary!=="ready":p.translation_state!=="ready").map((p,i)=>({request_id:String(i),resource:p.resource,fields:[{field:"summary",source_hash:p.summary_source_hash}]}));if(!items.length)return;
  const payload={target_language:targetLanguage,profile:"minecraft-v1",items};
  try{
    let ensuring=false,ensureDone=false,ensureError="";
    for(let i=0;i<40&&token===listGeneration;i++){
      const d=await request("translations:resolve",json(payload));if(token!==listGeneration)return;let pending=false;
      for(const r of d.results)for(const f of r.fields){
        if(f.state==="ready"&&f.matches_request&&f.is_current)pageViews.catalog.update(r.resource,f.translation.text,f.translation.translation_required===false?"简介 · "+originalLabel(f.translation):languageName()+" · 简介已就绪");
        else pageViews.catalog.update(r.resource,undefined,f.update?.notice||states[f.state]||"等待翻译");
        if(!(f.state==="ready"&&f.matches_request&&f.is_current)&&!["failed","blocked"].includes(f.state))pending=true;
      }
      if(!pending&&!d.results.some(r=>r.fields.some(f=>f.state==="failed"))){if(token===listGeneration)$("notice").textContent="";return;}
      if(!ensuring){
        ensuring=true;
        // Cached translations render first. Queue submission cannot hold up resolve.
        void (async()=>{const headers=await auth();for(let start=0;start<items.length&&token===listGeneration;start+=20)await request("translations:ensure",json({...payload,items:items.slice(start,start+20),purpose:"visible_list"},{...headers,"Idempotency-Key":crypto.randomUUID()}));})().catch(e=>{ensureError=e.message;}).finally(()=>{ensureDone=true;});
      }
      if(token===listGeneration)$("notice").textContent=ensureError?"译文暂时无法准备："+ensureError+"，可点击搜索重试。":"正在自动更新简介译文，原文仍可阅读…";
      if(ensureDone&&!pending){if(token===listGeneration)$("notice").textContent=ensureError||"部分译文暂不可用，再次访问时会重试。";return;}await sleep(i<3?800:2500);
    }
  }catch(e){if(token===listGeneration)$("notice").textContent="译文读取失败："+e.message+"。原文仍可阅读，点击搜索重试。";}
}
async function refresh(resource,status){
  status.textContent="正在核验原文与项目信息…";const headers=await auth(),job=await request(`projects/${resource.provider}/${resource.project_id}/refresh`,{method:"POST",headers});
  for(let i=0;i<90;i++){const d=await request("jobs/"+job.job_id,{headers});status.textContent=states[d.state]||d.state;if(d.state==="ready")return;if(d.state==="failed")throw Error(d.error?.code||"原文核验失败");await sleep(1000);}throw Error("仍在后台处理，可稍后再次查看。");
}
function tab(name){for(const b of document.querySelectorAll("[data-tab]")){b.classList.toggle("active",b.dataset.tab===name);b.setAttribute("aria-selected",String(b.dataset.tab===name));}for(const t of["intro","releases","dependencies"])$("panel-"+t).classList.toggle("hidden",t!==name);}
function renderMetadata(source){
  const m=source.metadata||{};icon($("detail-icon"),m.icon_url,source.fields.title.text);$("detail-byline").textContent="作者 · "+authors(m)+"　/　更新于 "+date(m.updated_at);$("detail-tags").replaceChildren(...chips([...(m.tags||[]),...(m.loaders||[])]).children);
  const aside=$("project-sidebar"),dl=node("dl");aside.replaceChildren(node("h3","项目信息"));for(const[k,v]of[["资源类型",labels[source.kind]],["创建时间",date(m.created_at)],["更新时间",date(m.updated_at)],["下载量",number(m.downloads)],["许可证",m.license||"原站未提供"],["项目 ID",source.resource.project_id]])dl.append(node("dt",k),node("dd",v));aside.append(dl,node("h3","作者与团队"));if(m.authors?.length)for(const a of m.authors)aside.append(link(a.url,a.name,"author-link"));else aside.append(node("p","作者信息暂未提供","muted"));aside.append(node("h3","支持的 Minecraft 版本"),chips([...(m.game_versions||[])].reverse(),"version-tag"),node("h3","加载器 / 平台"),chips(m.loaders||[]));if(m.metadata_complete===false)aside.append(node("p","部分原站信息暂不可用，后台会继续更新。","muted"));
  $("release-list").replaceChildren();$("dependency-version").replaceChildren();const releases=m.releases||[];
  releases.forEach((r,i)=>{const tr=node("tr"),name=node("td"),support=node("td"),dep=node("td"),button=node("button",r.dependencies.length+" 项","dependency-button");name.append(link(r.url,r.name),node("small",r.channel,"release-channel"));support.append(node("div",r.game_versions.join(" / ")),node("small",r.loaders.join(" / "),"muted"));button.onclick=()=>{$("dependency-version").value=String(i);renderDependencies();tab("dependencies");};dep.append(button);tr.append(name,support,node("td",date(r.published_at)),dep);$("release-list").append(tr);const option=node("option",r.name+" · "+r.loaders.join(" / "));option.value=String(i);$("dependency-version").append(option);});
  if(!releases.length){const tr=node("tr"),td=node("td","原站暂未提供可用版本，请访问原项目。");td.colSpan=4;tr.append(td);$("release-list").append(tr);}renderDependencies();
}
function renderDependencies(){const r=detailSource?.metadata?.releases?.[Number($("dependency-version").value||0)],target=$("dependency-list");target.replaceChildren();if(!r||!r.dependencies.length){target.append(node("p",r?"此版本未声明额外依赖。":"版本依赖信息暂未提供。","empty-state"));return;}for(const d of r.dependencies){const row=node("div",undefined,"dependency-row");row.append(node("span",relations[d.relation]||d.relation,"tag"),link(d.url,d.name));if(d.version_id)row.append(node("small","版本 ID · "+d.version_id,"muted"));target.append(row);}}
async function showDetail(resource){
  const token=++generation;detail=resource;detailSource=null;lastTranslation=null;tab("intro");$("report").classList.add("hidden");$("detail-title").textContent="正在读取项目…";$("detail-status").textContent="正在读取译文…";$("body-language-label").textContent="正在读取译文";$("translated-body").replaceChildren(node("p","正在读取项目介绍…","muted"));delete $("translated-body").dataset.revision;
  for(const id of["translated-summary","original-body","original-summary","project-sidebar","detail-tags","detail-byline"])$(id).replaceChildren();$("original-article").classList.add("hidden");$("toggle-original").textContent="对照原文";$("toggle-original").setAttribute("aria-pressed","false");if(!$("detail-dialog").open)$("detail-dialog").showModal();
  try{
    let initial;const detailPath=`projects/${resource.provider}/${resource.project_id}/detail?target_language=${encodeURIComponent(targetLanguage)}`;try{initial=await request(detailPath);}catch(e){if(e.message!=="project_not_indexed")throw e;await refresh(resource,$("detail-status"));initial=await request(detailPath);}if(token!==generation)return;const source=initial.source;detailSource=source;
    $("detail-title").textContent=source.fields.title.text;$("detail-provider").textContent=(resource.provider==="modrinth"?"Modrinth":"CurseForge")+" / "+labels[source.kind];$("original-link").href=source.source_url;$("translated-summary").textContent=source.fields.summary.text;$("original-summary").textContent=source.fields.summary.text;renderBody($("original-body"),source.fields.body.text,source.fields.body.format);renderMetadata(source);$("versions").textContent="当前原文版本："+source.snapshot_hash;
    renderBody($("translated-body"),source.fields.body.text,source.fields.body.format);
    request(`projects/${resource.provider}/${resource.project_id}/history`).then(h=>{if(token===generation)$("versions").textContent+="\n\n"+h.events.map(x=>date(x.observed_at)+"  "+({source_observed:"原文更新",metadata_updated:"项目信息更新",translation_published:"译文发布"}[x.type]||x.type)+(x.changed_fields?"  "+x.changed_fields.join(", "):"")).join("\n");}).catch(()=>{});
    const payload={target_language:targetLanguage,profile:"minecraft-v1",items:[{request_id:"detail",resource,fields:["summary","body"].map(field=>({field,source_hash:source.fields[field].source_hash}))}]};let ensured=false,ensureDone=false,ensureError="";
    for(let i=0;i<150&&token===generation&&$("detail-dialog").open;i++){
      const d=i===0?{results:[{fields:initial.fields}]}:await request("translations:resolve",json(payload));if(token!==generation)return;let complete=true,failed=false,notice="",summaryReady=false,bodyReady=false;
      if(d.results[0].fields.some(f=>f.update?.action==="refresh_source")){ $("detail-status").textContent="原文已更新，正在读取新版本并更新译文";await sleep(700);if(token===generation)showDetail(resource);return; }
      for(const f of d.results[0].fields){if(f.state==="ready"&&f.matches_request&&f.is_current){const target=f.field==="summary"?$("translated-summary"):$("translated-body");if(f.translation.text!==undefined){if(f.field==="body")renderBody(target,f.translation.text,f.translation.format||source.fields.body.format);else target.textContent=f.translation.text;}else if(target.dataset.revision!==f.translation.translation_revision){const artifact=await request("artifacts/"+f.translation.artifact.artifact_sha256);if(token!==generation)return;renderBody(target,artifact.text,artifact.format);target.dataset.revision=f.translation.translation_revision;}if(f.field==="summary")summaryReady=true;if(f.field==="body"){$("body-language-label").textContent=f.translation.translation_required===false?originalLabel(f.translation):"AI · "+languageName();bodyReady=true;lastTranslation={resource,field:f.field,source_hash:f.requested_source_hash,translation_revision:f.translation.translation_revision};$("report").classList.toggle("hidden",f.translation.translation_required===false);}}else{complete=false;notice=f.update?.notice||states[f.state]||f.state;if(["failed","blocked"].includes(f.state))failed=true;}if(f.update?.notice)notice=f.update.notice;}
      if(!bodyReady)$("body-language-label").textContent=failed?"原文 · 译文暂不可用":"原文 · 译文准备中";
      pageViews.catalog.detail(resource,d.results[0].fields,languageName());
      $("detail-status").textContent=complete?(d.results[0].fields.every(f=>f.translation?.translation_required===false)?"无需翻译 · 内容已就绪，保留原文":languageName()+" · 简介与正文已就绪 · 与当前原文一致"):summaryReady&&!bodyReady?"简介译文已就绪；正文："+(notice||"正在翻译，暂时显示原文"):notice||"正在翻译";if(complete)return;
      if(!ensured){ensured=true;void (async()=>{const headers=await auth();if(token!==generation)return;headers["Idempotency-Key"]=crypto.randomUUID();await request("translations:ensure",json({...payload,purpose:"detail"},headers));})().catch(e=>{ensureError=e.message;}).finally(()=>{ensureDone=true;});}
      if(ensureDone&&(failed||ensureError)){$("detail-status").textContent=(ensureError||notice||"译文暂不可用")+"；可点击刷新重试，原文仍可阅读。";return;}await sleep(i<5?1000:2500);
    }if(token===generation)$("detail-status").textContent="翻译继续在后台进行，可稍后再次查看。";
  }catch(e){if(token===generation)$("detail-status").textContent="暂时无法翻译："+e.message;}
}
$("search-form").onsubmit=e=>{e.preventDefault();load();};$("more").onclick=()=>{if(!cursor||listLoading)return;pageCursors[++pageIndex]=cursor;load(true);};$("previous-page").onclick=()=>{if(pageIndex>0&&!listLoading){pageIndex--;load(true);}};$("page-size").onchange=()=>load();
for(const b of document.querySelectorAll("[data-kind]"))b.onclick=()=>{kind=b.dataset.kind;for(const x of document.querySelectorAll("[data-kind]"))x.classList.toggle("active",x===b);load();};
for(const b of document.querySelectorAll("[data-tab]"))b.onclick=()=>tab(b.dataset.tab);
for(const b of document.querySelectorAll(".close"))b.onclick=()=>b.closest("dialog").close();$("detail-dialog").addEventListener("close",()=>generation++);$("dependency-version").onchange=renderDependencies;
$("toggle-original").onclick=()=>{const hidden=$("original-article").classList.toggle("hidden");$("toggle-original").textContent=hidden?"对照原文":"收起原文";$("toggle-original").setAttribute("aria-pressed",String(!hidden));};
$("open-import").onclick=()=>$("import-dialog").showModal();$("import-form").onsubmit=async e=>{e.preventDefault();$("import-submit").disabled=true;const resource={provider:$("provider").value,project_id:$("project-id").value.trim(),entity_type:"project",entity_id:$("project-id").value.trim()};try{await refresh(resource,$("import-status"));$("import-dialog").close();load();showDetail(resource);}catch(e){$("import-status").textContent=e.message;}finally{$("import-submit").disabled=false;}};
$("refresh-source").onclick=async()=>{if(!detail)return;const r=detail;$("refresh-source").disabled=true;try{await refresh(r,$("detail-status"));showDetail(r);load();}catch(e){$("detail-status").textContent=e.message;}finally{$("refresh-source").disabled=false;}};
$("report").onclick=async()=>{if(!lastTranslation)return;const message=prompt("请描述翻译问题（最多 4000 字）：");if(!message)return;try{await request("feedback",json({...lastTranslation,category:"incorrect",message:message.slice(0,4000)},await auth()));$("detail-status").textContent="反馈已提交，等待审核。";}catch(e){$("detail-status").textContent=e.message;}};
$("target-language").onchange=()=>{
  targetLanguage=$("target-language").value;
  try{localStorage.setItem("buggcraft.translation.target-language",targetLanguage);}catch(_){}
  generation++;load();if($("detail-dialog").open&&detail)showDetail(detail);
};
async function initialize(){
  let saved;try{saved=localStorage.getItem("buggcraft.translation.target-language");}catch(_){}
  if(saved&&[...$("target-language").options].some(o=>o.value===saved)){targetLanguage=saved;$("target-language").value=saved;}
  const initialLanguage=targetLanguage;
  const seeded=pageViews.catalog.seeded();if(seeded&&targetLanguage==="zh-Hans"){$("count").textContent=`共 ${seeded.total_count} 个项目`;cursor=seeded.next_cursor;$("more").disabled=!cursor;translateVisible(seeded.projects,listGeneration);}else void load();
  try{
    const d=await request("capabilities");
    $("service-state").textContent=d.generation_available?"翻译服务在线":"译文查询在线 · 生成暂不可用";
    const languages=d.languages||[{code:"zh-Hans",name:"简体中文"}];languageNames=Object.fromEntries(languages.map(l=>[l.code,l.name]));
    targetLanguage=languages.some(l=>l.code===saved)?saved:d.default_target_language||"zh-Hans";
    $("target-language").replaceChildren(...languages.map(l=>{const o=node("option",l.name);o.value=l.code;return o;}));$("target-language").value=targetLanguage;
  }catch(_){$("service-state").textContent="服务暂时无法连接";}
  if(targetLanguage!==initialLanguage)load();
}
initialize();
