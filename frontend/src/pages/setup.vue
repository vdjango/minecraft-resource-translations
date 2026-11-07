<script setup lang="ts">
import {computed,onMounted,ref} from 'vue';

const stages=['管理员','站点入口','翻译服务','资源分发','邮件通知','确认完成'];
const currentOrigin=location.origin;
const step=ref(0),ready=ref(false),busy=ref(false),error=ref(''),authenticated=ref(false),initialized=ref(false);
const username=ref(''),password=ref(''),confirmation=ref('');
const publicUrl=ref(location.origin),translationEnabled=ref(false),keyConfigured=ref(false),apiKey=ref('');
const translation=ref({base_url:'https://api.openai.com/v1',model:'',reasoning_effort:'medium',daily_token_limit:1000000,monthly_token_limit:20000000,daily_call_limit:2000,max_completion_tokens:16384,model_timeout:120});
const resources=ref<any>({enabled:true,public_url:'https://mirror.bugg.icu',quota_gib:50,reserve_gib:10,concurrency:4,metadata_ttl:300,policies:{core:'cache',loaders:'cache',mods:'redirect',java:'backup',libraries:'backup',assets:'backup'},auto_refresh:true,refresh_minutes:30,eviction:'oldest_version',protect_latest:2,monthly_egress_gib:0,egress_threshold:90,bandwidth_mbps:0,max_serving:32});
const categories=[['core','游戏核心','清单、客户端与服务端'],['loaders','加载器','Forge、NeoForge、Fabric、Quilt'],['mods','模组文件','默认直连原站，不扫描模组站'],['java','Java 运行时','Mojang 清单与运行时文件'],['libraries','依赖库','游戏依赖与原生库'],['assets','原版资源','音效、材质、语言与资源索引']];
const modes:Record<string,string>={cache:'缓存并提供下载',redirect:'仅重定向（不保存）',backup:'备份并重定向'};
const cacheRoot=ref(''),workerOnline=ref<boolean|null>(null),cacheBytes=ref(0);
const mail=ref({mail_enabled:false,smtp_host:'smtp.163.com',smtp_port:465,smtp_security:'ssl',smtp_username:'',mail_from:'',mail_to:'',mail_timeout:15});
const smtpPassword=ref(''),smtpConfigured=ref(false),models=ref<string[]>([]),modelsBusy=ref(false),modelNotice=ref('');
const saved=ref<string[]>([]);
const heading=computed(()=>['先为你的平台设置管理员','让所有服务拥有同一个入口','配置项目介绍的翻译服务','决定资源怎样到达玩家','接收申请与服务通知','准备好，开启你的资源服务'][step.value]);
const explanations=['由你设置用户名和密码。后续配置使用同一账户，中途退出后登录即可继续。','网站、译文 API 和游戏下载共用平台入口。请填写实际使用的访问地址。','可以现在连接供应商，也可以先运行资源下载与已有译文查询，稍后在后台开启生成。','六类内容分别配置。按空间与带宽能力提供服务，无法本地提供时转向其他来源。','通知为可选项，可以稍后在后台配置。','检查配置后完成安装。之后访问网站将进入正常页面，所有配置都能在后台调整。'];

async function request(path:string,body?:unknown){
  const response=await fetch(path,{method:body?'POST':'GET',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{}),signal:AbortSignal.timeout(12000)});
  const data=await response.json();
  if(!response.ok){
    const labels:Record<string,string>={unauthorized:'管理会话已过期，请重新登录继续。',invalid_credentials:'请检查用户名、密码长度与两次密码是否一致。',invalid_request:'配置格式不正确，请检查地址、数值范围与必填项。',invalid_settings:'配置不完整，请检查必填项。',setup_already_initialized:'管理员已创建，请登录继续。',setup_incomplete:'仍有步骤没有保存，请返回完成配置。',authentication_busy:'另一项登录操作正在执行，请稍后重试。',rate_limited:'尝试过于频繁，请稍后重试。',setup_already_completed:'安装已完成，可以进入后台。'};
    if(response.status===401){authenticated.value=false;step.value=0;}
    throw Error(labels[data.code]||'暂时无法保存，请重试。');
  }
  return data;
}
function applyStatus(data:any){
  initialized.value=data.initialized;authenticated.value=data.authenticated;saved.value=data.sections||[];
  if(data.settings){
    publicUrl.value=data.settings.public_url||location.origin;
    keyConfigured.value=data.settings.api_key_configured;translationEnabled.value=keyConfigured.value;
    for(const key of Object.keys(translation.value) as (keyof typeof translation.value)[])if(data.settings[key]!==undefined)(translation.value as any)[key]=data.settings[key];
    smtpConfigured.value=data.settings.smtp_password_configured;
    for(const key of Object.keys(mail.value) as (keyof typeof mail.value)[])if(data.settings[key]!==undefined)(mail.value as any)[key]=data.settings[key];
  }
  if(data.resources){resources.value={...data.resources,policies:Object.fromEntries(Object.entries(data.resources.policies||{}).map(([key,mode])=>[key,mode==='disabled'?'redirect':mode]))};cacheRoot.value=data.cache_root;}
}
async function load(){
  try{const data=await request('/v1/setup/status');if(!data.required){location.replace('/admin');return;}applyStatus(data);
    if(data.authenticated){const index=['site','translation','resources','mail'].findIndex(section=>!saved.value.includes(section));step.value=index<0?5:index+1;}
  }catch(e){error.value=(e as Error).message;}finally{ready.value=true;}
}
async function next(){
  if(busy.value)return;busy.value=true;error.value='';
  try{
    if(step.value===0){
      if(!initialized.value&&password.value!==confirmation.value)throw Error('两次输入的密码不一致。');
      await request(initialized.value?'/v1/admin/session':'/v1/setup/account',{username:username.value,password:password.value,...(!initialized.value?{password_confirm:confirmation.value}:{})});
      password.value='';confirmation.value='';await load();return;
    }
    if(step.value===5){await request('/v1/setup/finish',{});location.replace('/admin');return;}
    const section=['','site','translation','resources','mail'][step.value];
    let settings:any;
    if(section==='site')settings={public_url:publicUrl.value.replace(/\/$/,'')};
    if(section==='translation'){
      if(translationEnabled.value){if(!keyConfigured.value&&!apiKey.value)throw Error('请填写供应商 API Key，或选择暂不启用。');settings={...translation.value,...(apiKey.value?{api_key:apiKey.value}:{})};}
      else settings={api_key:''};
    }
    if(section==='resources'){const {sources,...values}=resources.value;settings=values;}
    if(section==='mail')settings=mail.value.mail_enabled?{...mail.value,...(smtpPassword.value?{smtp_password:smtpPassword.value}:{})}:{mail_enabled:false};
    const data=await request('/v1/setup/configure',{section,settings});
    saved.value=data.sections||[];if(data.settings){keyConfigured.value=data.settings.api_key_configured;smtpConfigured.value=data.settings.smtp_password_configured;}
    apiKey.value='';smtpPassword.value='';step.value++;
  }catch(e){error.value=(e as Error).message;}finally{busy.value=false;}
}
async function readModels(){
  if(modelsBusy.value)return;modelsBusy.value=true;modelNotice.value='';
  try{const data=await request('/v1/admin/models',{base_url:translation.value.base_url,...(apiKey.value?{api_key:apiKey.value}:{})});models.value=data.models;modelNotice.value=`已取得 ${data.models.length} 个模型。`;if(!translation.value.model&&data.models.length)translation.value.model=data.models[0];}
  catch(e){modelNotice.value=(e as Error).message+' 也可以手动填写模型名称。';}finally{modelsBusy.value=false;}
}
async function inspectResources(){
  try{const data=await request('/v1/admin/mirror');workerOnline.value=data.running;cacheBytes.value=data.cached_bytes||0;}catch{workerOnline.value=false;}
}
onMounted(()=>void load());
</script>

<template>
<div class="installation-shell"><header class="installation-header"><a class="brand" href="/setup"><span class="mark">B</span><span class="brand-type"><strong>BUGGCRAFT</strong><small>RESOURCES &amp; TRANSLATIONS</small></span></a><span class="installation-badge">首次安装 · 配置向导</span></header>
<main class="installation-layout"><aside class="installation-progress" aria-label="初始化进度"><p class="eyebrow">MAKE IT YOURS</p><h2>一个平台。<br>从这里开始。</h2><ol><li v-for="(stage,index) in stages" :key="stage" :class="{current:step===index,done:step>index}" :aria-current="step===index?'step':undefined"><span>{{step>index?'✓':String(index+1).padStart(2,'0')}}</span><div>{{stage}}<small>{{index===0?'设置管理账户':index===1?'网站与服务地址':index===2?'供应商与生成预算':index===3?'分类、空间与分流':index===4?'可选的邮件通知':'检查并开启服务'}}</small></div></li></ol><p class="muted small">配置按步骤保存。<br>完成前所有网页入口均进入此向导。</p></aside>
<section class="installation-panel" :aria-busy="busy"><div class="eyebrow">SETUP / {{String(step+1).padStart(2,'0')}} OF 06</div><h1>{{heading}}</h1><p class="installation-lead">{{explanations[step]}}</p><p v-if="!ready" role="status">正在读取安装状态…</p>
<form v-if="ready" @submit.prevent="next">
<div v-if="step===0" class="wizard-section"><p v-if="initialized" class="wizard-note">管理员账户已设置。请登录，继续未完成的安装。</p><label>管理员用户名<input v-model.trim="username" autocomplete="username" pattern="[A-Za-z0-9_-]{1,32}" maxlength="32" required placeholder="设置你的用户名"><small>1–32 个英文字母、数字、下划线或短横线。</small></label><label>管理员密码<input v-model="password" type="password" :autocomplete="initialized?'current-password':'new-password'" :minlength="initialized?undefined:12" maxlength="256" required :placeholder="initialized?'输入管理员密码':'至少 12 个字符'"></label><label v-if="!initialized">确认密码<input v-model="confirmation" type="password" autocomplete="new-password" minlength="12" maxlength="256" required placeholder="再输入一次密码"></label><p class="muted small">没有默认账户。密码只保存为加密摘要。</p></div>
<div v-if="step===1" class="wizard-section"><label>平台公开地址<input v-model.trim="publicUrl" type="url" required placeholder="https://mirror.bugg.icu"><small>先使用当前可访问的地址；域名和 HTTPS 就绪后可在后台切换。</small></label><p class="wizard-note">当前访问：<code>{{currentOrigin}}</code><br>计划部署域名：<code>mirror.bugg.icu</code></p><div class="wizard-grid"><article><h3>同一个网站</h3><p>资源目录、译文、下载、文档和后台使用统一入口。</p></article><article><h3>各自执行任务</h3><p>翻译与下载分别执行，避免大文件影响译文服务。</p></article></div></div>
<div v-if="step===2" class="wizard-section"><div class="wizard-choice"><label><input v-model="translationEnabled" type="radio" :value="false"> 暂不启用新译文生成<small>下载和已有译文查询照常使用，稍后可以开启。</small></label><label><input v-model="translationEnabled" type="radio" :value="true"> 连接翻译供应商<small>设置兼容 OpenAI 的服务地址、密钥、模型和预算。</small></label></div><template v-if="translationEnabled"><label>API 基础地址<input v-model.trim="translation.base_url" type="url" required placeholder="https://api.example.com/v1"></label><label>API Key<input v-model="apiKey" type="password" autocomplete="new-password" :required="!keyConfigured" :placeholder="keyConfigured?'已保存；留空保留':'填写供应商密钥'"></label><div class="wizard-inline"><label>模型<input v-model.trim="translation.model" required list="setup-models" placeholder="输入或读取模型名称"><datalist id="setup-models"><option v-for="model in models" :key="model" :value="model" /></datalist></label><button type="button" class="secondary" :disabled="modelsBusy" @click="readModels">{{modelsBusy?'正在读取…':'读取模型'}}</button></div><p v-if="modelNotice" role="status" class="muted small">{{modelNotice}}</p><div class="wizard-grid"><label>每日 Token 上限<input v-model.number="translation.daily_token_limit" type="number" min="0" max="10000000000" required></label><label>每月 Token 上限<input v-model.number="translation.monthly_token_limit" type="number" min="0" max="100000000000" required></label><label>每日调用上限<input v-model.number="translation.daily_call_limit" type="number" min="0" max="10000000" required></label><label>推理档位<select v-model="translation.reasoning_effort"><option value="none">关闭</option><option value="minimal">极简</option><option value="low">低</option><option value="medium">中</option><option value="high">高</option><option value="xhigh">极高</option></select></label></div></template></div>
<div v-if="step===3" class="wizard-section"><label class="wizard-toggle"><input v-model="resources.enabled" type="checkbox"> 启用资源下载服务</label><p class="muted small">关闭时资源文件按原有策略回退上游；可在后台随时调整。</p><label>公开下载地址<input v-model.trim="resources.public_url" type="url" required placeholder="https://mirror.bugg.icu"><small>公网下载节点要求 HTTPS；正式部署时与平台域名一致。本地预览不会自动配置 DNS。</small></label><h3>六类资源策略</h3><div class="wizard-policies"><label v-for="category in categories" :key="category[0]"><span><strong>{{category[1]}}</strong><small>{{category[2]}}</small></span><select v-model="resources.policies[category[0]]" :aria-label="category[1]+'策略'"><option v-for="(label,mode) in modes" :key="mode" :value="mode">{{label}}</option></select></label></div><p class="wizard-note">备份并重定向：平时用户直连上游，后台保留副本；确认上游故障后使用已完成备份。来源映射自动生成，优先已核验的国内来源，不占本地缓存。</p><h3>公开来源</h3><label class="wizard-toggle"><input v-model="resources.auto_sources" type="checkbox"> 自动扫描并生成来源映射</label><label class="wizard-toggle"><input v-model="resources.source_prefer_cn" type="checkbox"> 优先国内来源，同地区按探测耗时选择</label><p class="muted small">内置阿里云、华为云、南京大学与 Maven Central。安装后可添加来源，查看各源文件覆盖和下载命中；本地副本默认保留。</p><h3>缓存与刷新</h3><p class="muted small">缓存目录：<code>{{cacheRoot}}</code>。目录由部署环境指定。</p><div class="wizard-grid"><label>缓存上限 / GiB<input v-model.number="resources.quota_gib" type="number" min="1" max="100000" required></label><label>磁盘保留 / GiB<input v-model.number="resources.reserve_gib" type="number" min="10" max="100000" required></label><label>空间不足时<select v-model="resources.eviction"><option value="oldest_version">优先新版本</option><option value="lru">淘汰最久未使用</option><option value="stop">停止新增，不删除</option></select></label><label>保护最新版本数<input v-model.number="resources.protect_latest" type="number" min="0" max="50" required></label><label>刷新间隔 / 分钟<input v-model.number="resources.refresh_minutes" type="number" min="5" max="1440" required></label><label class="wizard-toggle"><input v-model="resources.auto_refresh" type="checkbox"> 定时增量刷新</label></div><details class="wizard-details"><summary>出口预算与协作节点</summary><div class="wizard-grid"><label>每月出口 / GiB<input v-model.number="resources.monthly_egress_gib" type="number" min="0" max="1000000" required><small>0 表示尚未设置限制。</small></label><label>转移阈值 / %<input v-model.number="resources.egress_threshold" type="number" min="10" max="100" required></label><label>规划带宽 / Mbps<input v-model.number="resources.bandwidth_mbps" type="number" min="0" max="100000" required></label><label>同时发送文件数<input v-model.number="resources.max_serving" type="number" min="1" max="512" required></label></div><p class="muted small">接近预算时转健康节点、备用源或文件的原始发布地址。节点地址、权重、来源映射和 Cloudflare 规则可在完成后的平台设置中管理。</p></details></div>
<div v-if="step===4" class="wizard-section"><div class="wizard-choice"><label><input v-model="mail.mail_enabled" type="radio" :value="false"> 暂不启用邮件<small>稍后在管理后台填写。</small></label><label><input v-model="mail.mail_enabled" type="radio" :value="true"> 配置邮件通知<small>接收接入申请与管理通知。</small></label></div><template v-if="mail.mail_enabled"><div class="wizard-grid"><label>SMTP 服务器<input v-model.trim="mail.smtp_host" required></label><label>端口<input v-model.number="mail.smtp_port" type="number" min="1" max="65535" required></label><label>加密方式<select v-model="mail.smtp_security"><option value="ssl">SSL / TLS</option><option value="starttls">STARTTLS</option></select></label><label>SMTP 用户名<input v-model.trim="mail.smtp_username" required autocomplete="off"></label></div><label>SMTP 密码 / 授权码<input v-model="smtpPassword" type="password" :required="!smtpConfigured" autocomplete="new-password" :placeholder="smtpConfigured?'已保存；留空保留':'填写授权码'"></label><div class="wizard-grid"><label>发件地址<input v-model.trim="mail.mail_from" type="email" required></label><label>通知收件地址<input v-model.trim="mail.mail_to" type="email" required></label></div></template></div>
<div v-if="step===5" class="wizard-section"><dl class="wizard-summary"><div><dt>管理账户</dt><dd>已设置</dd></div><div><dt>平台地址</dt><dd>{{publicUrl}}</dd></div><div><dt>译文生成</dt><dd>{{keyConfigured?'已配置供应商':'暂不启用'}}</dd></div><div><dt>资源下载</dt><dd>{{resources.enabled?'启用':'关闭本地服务'}} · {{resources.quota_gib}} GiB 缓存</dd></div><div><dt>更新与淘汰</dt><dd>{{resources.auto_refresh?'每 '+resources.refresh_minutes+' 分钟':'手动刷新'}} · {{resources.eviction==='oldest_version'?'优先新版本':resources.eviction==='lru'?'最久未使用淘汰':'停止新增'}}</dd></div><div><dt>邮件通知</dt><dd>{{mail.mail_enabled?'已配置':'暂不启用'}}</dd></div></dl><button class="secondary" type="button" @click="inspectResources">检查资源进程状态</button><p v-if="workerOnline!==null" class="wizard-note">{{workerOnline?'下载工作进程在线 · 已缓存 '+(cacheBytes/1024**3).toFixed(2)+' GiB':'下载工作进程未启动；设置已保存，按部署文档启动后即可提供下载。'}}</p><p class="muted small">域名解析、HTTPS 和下载工作进程按部署环境配置。Cloudflare 操作指引、来源映射及节点接入在后台「平台设置」继续管理。</p></div>
<p v-if="error" role="alert" class="wizard-error">{{error}}</p><div class="wizard-footer"><button v-if="step>1" type="button" class="secondary" :disabled="busy" @click="step--;error=''">上一步</button><span class="muted small">{{step===0?'首次设置无需默认密码或私有链接。':'本步骤保存后可以随时回来继续。'}}</span><button type="submit" :disabled="busy">{{busy?'正在保存…':step===5?'完成安装，进入后台 →':step===0&&initialized?'登录并继续 →':'保存并继续 →'}}</button></div></form></section></main><footer class="installation-footer">Buggcraft · 资源、译文与服务节点，统一管理。</footer></div>
</template>
