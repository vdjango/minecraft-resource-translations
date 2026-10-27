"use strict";
const projectHost=document.getElementById("project-page"),projectData=JSON.parse(projectHost.dataset.detail);
const projectLanguage=document.getElementById("project-language"),projectStatus=document.getElementById("project-status"),projectRetry=document.getElementById("project-retry");
let projectGeneration=0,projectSession;
const projectMd=window.markdownit({html:true});
async function projectRequest(path,body,headers={}){
  const response=await fetch("/v1/"+path,{...(body?{method:"POST",body:JSON.stringify(body),headers:{"Content-Type":"application/json",...headers}}:{}),cache:"no-store"});
  const value=await response.json();if(!response.ok)throw Error(value.code||"读取失败");return value;
}
async function readProject(allowEnsure=true){
  const generation=++projectGeneration,language=projectLanguage.value,resource=projectData.source.resource;
  projectRetry.disabled=true;
  try{
    for(let step=0;step<90&&generation===projectGeneration;step++){
      if(step===0)projectStatus.textContent="正在读取最新译文…";
      const data=await projectRequest(`projects/${resource.provider}/${resource.project_id}/detail?target_language=${encodeURIComponent(language)}`);
      if(generation!==projectGeneration)return;
      for(const f of data.fields){const source=data.source.fields[f.field],translated=f.state==="ready"&&f.is_current&&f.translation,text=translated?translated.text:source.text;
        if(f.field==="summary")document.getElementById("project-summary").textContent=text;
        else{const body=document.getElementById("project-body");body.lang=translated?language:"und";body.innerHTML=DOMPurify.sanitize((translated?.format||source.format)==="html"?text:projectMd.render(text),{FORBID_TAGS:["style","script","iframe","object","embed","svg","math","form","input","button"],FORBID_ATTR:["style","srcset"]});for(const img of body.querySelectorAll("img")){img.loading="lazy";img.referrerPolicy="no-referrer";}}
      }
      const ready=data.fields.every(f=>f.state==="ready"&&f.is_current);
      projectStatus.textContent=ready?projectLanguage.selectedOptions[0].textContent+" · 简介与正文已就绪":data.fields.find(f=>f.state!=="ready")?.update?.notice||"译文准备中，暂时显示原文";
      if(ready)return;
      if(step===0&&allowEnsure){
        if(!projectSession)projectSession=await projectRequest("sessions/anonymous",{client_id:"buggcraft-web"});
        if(generation!==projectGeneration)return;
        await projectRequest("translations:ensure",{target_language:language,profile:"minecraft-v1",purpose:"detail",items:[{request_id:"project",resource,fields:["summary","body"].map(field=>({field,source_hash:data.source.fields[field].source_hash}))}]},{Authorization:"Bearer "+projectSession.access_token,"Idempotency-Key":crypto.randomUUID()});
      }
      await new Promise(resolve=>setTimeout(resolve,step<4?1000:2500));
    }
    if(generation===projectGeneration)projectStatus.textContent="后台仍在处理译文，可稍后重新读取。";
  }catch(e){if(generation===projectGeneration)projectStatus.textContent="译文暂时无法读取："+e.message+"，可重新读取。";}
  finally{if(generation===projectGeneration)projectRetry.disabled=false;}
}
projectLanguage.onchange=()=>{const url=new URL(location.href);url.searchParams.set("target_language",projectLanguage.value);location.href=url.href;};
projectRetry.onclick=()=>readProject();
// The first HTML already contains ready translations; only pending content polls.
if(projectData.fields.some(f=>f.state!=="ready"))void readProject();
