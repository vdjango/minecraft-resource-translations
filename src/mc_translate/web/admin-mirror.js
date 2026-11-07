"use strict";
function mirrorBytes(value){return (Number(value||0)/1024**3).toFixed(2)+' GiB';}
function mirrorSize(value){const bytes=Number(value||0);return bytes>=1024**3?mirrorBytes(bytes):bytes>=1024**2?(bytes/1024**2).toFixed(1)+' MiB':(bytes/1024).toFixed(0)+' KiB';}
function mirrorFailure(row){
  const card=sourceElement('article');card.className='mirror-failure';
  const raw=String(row.url||row.path||'未知文件');let host='';
  try{host=new URL(raw).hostname;}catch{}
  const status=Number(row.status)||(/Bad Gateway/i.test(row.error||'')?502:/404|Not Found/i.test(row.error||'')?404:0);
  const label=status?'HTTP '+status:row.error?'请求异常':'失败';
  const reason=row.error||'上游未返回可用文件';
  const advice=status===404?'检查文件路径与版本是否仍在上游；可在「来源覆盖」寻找替代来源。':status>=500?'上游暂时不可用；稍后使用「只重试失败文件」，或在来源管理中启用替代源。':status===403?'上游拒绝访问；检查源的访问策略，换用其他来源。':'检查来源连接、文件校验及磁盘余量，再单独重试。';
  card.append(sourceElement('strong',label+' · '+(host||'已配置来源')),sourceElement('code',raw),sourceElement('p',reason),sourceElement('small',advice));
  return card;
}
const mirrorCategories=['core','loaders','mods','java','libraries','assets'];
const mirrorCategoryNames={core:'游戏核心',loaders:'加载器',mods:'模组',java:'Java 运行时',libraries:'依赖库',assets:'原版资源'};
function mirrorDestination(settings,category){
  const route=settings.qiniu_routes?.[category]||{};
  if(route.mode==='cloud'||route.mode==='copy'){
    const profile=(settings.qiniu_profiles||[]).find(item=>item.id===route.profile);
    return (profile?.name||profile?.bucket||route.profile||'未配置云空间')+(route.mode==='copy'?' + 本机':'（云端）');
  }
  return '本机磁盘';
}
function transferPlatform(url){
  const name=new URL(url).pathname.split('/').pop().toLowerCase();
  return name.endsWith('.so')?'Linux':name.endsWith('.dylib')||name.endsWith('.jnilib')?'macOS':name.endsWith('.dll')||name.endsWith('.exe')?'Windows':'通用 / 未标注';
}
function roomCacheSettings(){
  return roomCacheStore.payload();
}
function renderRoomCache(data,fill){
  const s=data.settings,state=data.room_client_cache||{};
  roomCacheStore.hydrate(s,fill);
  // The room cache controls are now owned by Vue; old DOM hooks no longer exist.
  if(!$('room-cache-state'))return;
  $('room-cache-state').textContent=!data.running?'资源进程离线，设置仍可保存。':!state.configured?'尚未配置节点授权密钥；不会开放匿名上传。':s.room_cache_enabled?'节点授权已配置，按房间权限提供兜底。':'客户端文件兜底已关闭。';
  metrics($('room-cache-metrics'),[['临时文件',number(state.files),'包含本地和云端索引，按 SHA-256 去重'],['本地保留',mirrorSize(state.local_bytes),'仅本地副本，上限 '+Number((s.room_cache_quota_mib/1024).toFixed(2))+' GiB'],['云端中转暂存',mirrorSize(state.staging_bytes),'独立上限 '+Number((s.room_cache_staging_mib/1024).toFixed(2))+' GiB；云端核验后释放'],['正在补传 / 读取',number(state.uploads)+' / '+number(state.downloads),'云空间容量在存储与容量中按空间计算']]);
  const row=values=>{const tr=document.createElement('tr');for(const value of values){const td=document.createElement('td');td.textContent=String(value);tr.append(td);}return tr;};
  $('room-cache-sources').replaceChildren(...(state.sources||[]).map(source=>row([source.host,source.samples?source.speed.toFixed(0)+' KiB/s':'观察中',source.bad+' / '+source.samples,qualityTime(source.checked)])));
}
function mirrorRules(domain){
  const siteHosts=['bugg.icu','download.bugg.icu','node.bugg.icu','translate.bugg.icu'];
  const mirrorHosts=['mirror.bugg.icu','mirror2.bugg.icu'];
  const activeHost=new URL(domain).hostname;
  if(!siteHosts.includes(activeHost) && !mirrorHosts.includes(activeHost))mirrorHosts.push(activeHost);
  const allHosts=siteHosts.concat(mirrorHosts);
  const hostSet=items=>items.map(item=>JSON.stringify(item)).join(' ');
  const prefix=path=>'starts_with(http.request.uri.path, '+JSON.stringify(path)+')';
  const mirrorPaths=['/assets/','/mc/game/','/java/v1/','/v1/packages/','/v1/objects/',
    '/v1/products/java-runtime/','/maven/','/fabric-meta/','/quilt-meta/',
    '/fabric-maven/','/quilt-maven/','/neoforge-maven/',
    '/mirrors/authlib-injector/','/mods/','/_mirror_node/'];
  const privatePaths=['/api/','/v1/','/admin','/login','/account','/network',
    '/developers','/setup','/room-cache/','/mirror-api/','/_internal/'];
  $('mirror-cf-static').textContent=[
    '(',
    '  http.host in {'+hostSet(allHosts)+'}',
    '  and http.request.uri.path.extension in {"js" "css" "png" "jpg" "jpeg" "webp" "avif" "svg" "ico" "woff" "woff2"}',
    '  and not '+prefix('/api/'),
    '  and not '+prefix('/v1/'),
    ')',
  ].join('\n');
  $('mirror-cf-bypass').textContent=[
    '(',
    '  http.host in {'+hostSet(allHosts)+'}',
    '  and ('+privatePaths.map(prefix).join(' or\n    ')+')',
    ')',
  ].join('\n');
  $('mirror-cf-expression').textContent=[
    '(',
    '  (http.host eq "download.bugg.icu" and '+prefix('/admin/assets/')+')',
    '  or (http.host in {'+hostSet(mirrorHosts)+'} and (',
    '    '+mirrorPaths.map(prefix).join(' or\n    '),
    '  ))',
    ')',
  ].join('\n');
}
let mirrorLoaded=false;
function renderMirror(data,fill=false){
  const s=data.settings;
  const fillSettings=fill||!mirrorLoaded;
  prefetchStore.hydrate(s.prefetch_platform,fillSettings);
  qualityStore.hydrate(data,fillSettings);
  $('mirror-state').textContent=data.running?'资源下载在线 · 最近状态已更新':'资源下载未启动 · 可保存设置，翻译配置与任务继续独立运行。';
  const cloudProfiles=data.cloud_storage?.profiles||[];
  const cloudBytes=cloudProfiles.reduce((sum,profile)=>sum+Number(profile.bytes||0),0);
  const cloudFiles=cloudProfiles.reduce((sum,profile)=>sum+Number(profile.files||0),0);
  const storedCategories=mirrorCategories.filter(key=>['cache','backup'].includes(s.policies[key]));
  const cloudRoutes=storedCategories.filter(key=>['cloud','copy'].includes(s.qiniu_routes?.[key]?.mode));
  const localRoutes=storedCategories.filter(key=>['local','copy'].includes(s.qiniu_routes?.[key]?.mode));
  const cloudOnly=cloudRoutes.length>0&&localRoutes.length===0;
  metrics($('mirror-metrics'),cloudRoutes.length?[['云端已核验副本',number(cloudFiles),'已写入对象存储'],['云端占用',mirrorBytes(cloudBytes),'按所选云空间分别计费'],['本机暂存与历史副本',mirrorBytes(data.cached_bytes),'上传核验后可安全释放'],['当前回源下载',number(data.active_downloads),'并发上限 '+s.concurrency]]:[['本机已缓存文件',number(data.cached_files),'完成文件才会对外发布'],['本机占用',mirrorBytes(data.cached_bytes),'本机上限 '+s.quota_gib+' GiB'],['磁盘可用',data.disk_free==null?'进程离线':mirrorBytes(data.disk_free),'保留 '+s.reserve_gib+' GiB'],['当前回源下载',number(data.active_downloads),'并发上限 '+s.concurrency]]);
  if(fillSettings){$('mirror-url').value=s.public_url;$('mirror-concurrency').value=s.concurrency;$('mirror-ttl').value=s.metadata_ttl;$('mirror-enabled').checked=s.enabled;mirrorLoaded=true;}
  if(fillSettings){
    for(const [id,key] of [['mirror-refresh-minutes','refresh_minutes'],['mirror-node-weight','node_weight'],['mirror-monthly-egress','monthly_egress_gib'],['mirror-egress-threshold','egress_threshold'],['mirror-bandwidth','bandwidth_mbps'],['mirror-max-serving','max_serving']])$(id).value=s[key];
    $('mirror-auto-refresh').checked=s.auto_refresh;
    $('mirror-nodes').value=(s.nodes||[]).map(n=>n.url+' '+n.weight+(n.enabled?'':' off')).join('\n');
    renderSourceProviders(s.source_registries||[]);
    $('mirror-auto-sources').checked=s.auto_sources!==false;$('mirror-source-prefer-cn').checked=s.source_prefer_cn!==false;$('mirror-source-cleanup').checked=!!s.source_cleanup;
  }

  const schedule=s.auto_refresh?'下次自动同步：'+(data.refresh?.next_at?new Date(data.refresh.next_at*1000).toLocaleString('zh-CN'):'正在安排'):'定时同步已关闭';
  $('mirror-overview-schedule').textContent=schedule;
  $('mirror-task-schedule').textContent=schedule+' · 周期 '+s.refresh_minutes+' 分钟';
  const last=data.refresh?.last;
  $('mirror-overview-last').textContent=last?'上次同步：'+qualityTime(last.at)+' · '+(last.state||'已完成'):'尚无自动同步记录';
  const room=data.room_client_cache||{};
  $('room-cache-overview').textContent=number(room.files)+' 个文件索引 · 本地保留 '+mirrorBytes(room.local_bytes)+' / '+mirrorBytes((s.room_cache_quota_mib||0)*1024**2)+' · 云端中转暂存 '+mirrorBytes(room.staging_bytes)+' / '+mirrorBytes((s.room_cache_staging_mib||0)*1024**2);
  const storageRows=cloudProfiles.map(profile=>[profile.name||profile.bucket||profile.id,profile.bytes,profile.quota_gib*(profile.quota_unit==='GB'?1e9:1024**3)]);
  if(localRoutes.length||Number(data.cached_bytes)>0)storageRows.push([cloudOnly?'本机暂存 / 待迁移':'普通镜像 · 本机',data.cached_bytes,s.quota_gib*1024**3]);
  if(Number(room.local_bytes)>0||!cloudOnly)storageRows.push(['房间文件 · 本地保留',room.local_bytes,s.room_cache_quota_mib*1024**2]);
  if(Number(room.staging_bytes)>0)storageRows.push(['房间文件 · 云端上传中转暂存',room.staging_bytes,s.room_cache_staging_mib*1024**2]);
  $('mirror-storage-bars').replaceChildren(...storageRows.map(([label,used,limit])=>{
    const host=sourceElement('div');host.className='mirror-storage-row';const top=sourceElement('div');top.append(sourceElement('strong',label),sourceElement('span',mirrorBytes(used)+' / '+mirrorBytes(limit)));
    const meter=document.createElement('meter');meter.min=0;meter.max=Number(limit)||1;meter.value=Math.min(Number(used)||0,Number(limit)||1);host.append(top,meter);return host;
  }));
  const cachedGiB=Number(data.cached_bytes||0)/1024**3,freeGiB=Number(data.disk_free||0)/1024**3;
  const available=Math.max(0,Math.min(s.quota_gib-cachedGiB,freeGiB-s.reserve_gib));
  const recentPrefetch=(data.operations?.history||[]).find(item=>item.action==='prefetch');
  const stopReason=recentPrefetch?.state==='cancelled'?'上次预下载被停止，可在同步任务中继续。':recentPrefetch?.state==='disk_limit'?'上次触发空间保护线。':'按策略跳过的文件不会写入本地。';
  $('mirror-quota-reason').textContent=cloudRoutes.length?cloudRoutes.length+' 类资源已指向云空间；本机配额只约束下载暂存与仍选本机的类别。云端上传'+(s.qiniu_paused?'已暂停':'已启用')+'，云空间容量和流量分别在「存储与容量」管理。当前本机暂存或历史副本 '+cachedGiB.toFixed(2)+' GiB；可继续暂存约 '+available.toFixed(2)+' GiB。'+stopReason:'本机已用 '+cachedGiB.toFixed(2)+' / '+s.quota_gib+' GiB；按配额与磁盘保留量，目前最多还能新增约 '+available.toFixed(2)+' GiB。上限不是自动填满的目标。'+stopReason;
  $('mirror-resource-summary').textContent='已核验云端副本 '+number(cloudFiles)+' 个（'+mirrorBytes(cloudBytes)+'）；本机暂存与历史副本实际占用 '+mirrorBytes(data.cached_bytes)+'。资源索引、来源映射和清单不等于本机实体；同一文件的多个索引不重复计算磁盘占用。';
  const catalogue=data.inventory||{};
  $('mirror-catalog-summary').textContent=catalogue.known_bytes?'资源清单登记全部版本与平台，潜在文件约 '+mirrorBytes(catalogue.known_bytes)+'；预下载范围：'+prefetchStore.describe(s.prefetch_platform)+'。通用文件按哈希复用，再按来源映射、类别策略及容量筛选。清单总量不是所选平台的下载量，也不是本机或云端已用容量；可在「镜像设置 → 同步调度」调整平台。':'尚未扫描资源清单；清单扫描不下载实体文件。';
  $('mirror-node-status').replaceChildren(...(data.nodes||[]).map(n=>{const span=document.createElement('span');span.textContent=new URL(n.url).hostname+' · '+(!n.enabled?'停用':n.healthy?'在线':'检查中 / 不可用');return span;}));
  $('mirror-egress-state').textContent=data.egress?`本月（UTC ${data.egress.period}）已接受资源 ${mirrorBytes(data.egress.bytes)} · 最近一分钟 ${mirrorBytes(data.egress.window_bytes)} · ${data.accepting?'本机可继续接收':'将优先转移下载'}`:'镜像上线后显示资源出口统计';
  renderSourceStatistics(data);renderQuality(data);renderRoomCache(data,fillSettings);renderCloud(data.cloud_storage||{},s,fillSettings,data);mirrorRules(s.public_url);
  const states={idle:'空闲',running:'运行中',finished:'已完成',failed:'失败',cancelled:'已停止',disk_limit:'已到空间保护线',incomplete:'部分完成 · 失败项已入补齐队列'};
  const actions={refresh:'增量同步',inventory:'清单扫描',prefetch:'预下载',retry_failures:'失败文件重试',source_cleanup:'本地副本清理',source_scan:'来源映射扫描'};
  const job=data.job||{state:'idle'};
  const op=data.operations||{};
  $('mirror-overview-job').textContent=job.action?((actions[job.action]||job.action)+' · '+(states[job.state]||job.state)):'暂无正在执行的同步任务';
  $('mirror-job-state').textContent=(states[job.state]||job.state)+(job.total!=null?` · ${number(job.done)} / ${number(job.total)}`:'')+(job.skipped!=null?` · 策略跳过 ${number(job.skipped)} · 下载失败 ${number(job.errors)}`:'')+(job.error?' · '+job.error:'');
  if(job.prefetch_platform)$('mirror-job-state').textContent+=' · 本轮范围：'+prefetchStore.describe(job.prefetch_platform);
  if(job.action==='source_cleanup')$('mirror-job-state').textContent='本地副本清理 · '+(states[job.state]||job.state)+' · 已检查 '+number(job.done)+' / '+number(job.total)+' · 移除 '+number(job.removed)+' · 释放 '+mirrorBytes(job.freed_bytes)+' · 跳过 '+number(job.skipped);
  if(job.platform_skipped)$('mirror-job-state').textContent+=' · 超出本轮平台范围跳过 '+number(job.platform_skipped);
  const progress=job.progress||{};
  const processed=Number(job.done??progress.done??0),total=Number(job.total??progress.total??0);
  $('mirror-job-progress').replaceChildren();
  if(total>0){const label=sourceElement('p',number(processed)+' / '+number(total)+' 个文件 · '+Math.min(100,processed/total*100).toFixed(1)+'%');const bar=document.createElement('progress');bar.max=total;bar.value=Math.min(processed,total);$('mirror-job-progress').append(label,bar);}
  const queued=op.failed_queue||{count:0,files:[]};
  const catalogFailures=[...(job.catalog_errors||[]),...(job.report?.errors||[])];
  const shown=[...(queued.files||[]),...catalogFailures].slice(0,8);
  $('mirror-job-failures').replaceChildren(...(shown.length?shown.map(mirrorFailure):[sourceElement('p','待补齐队列为空；当前没有清单扫描错误。')]));
  $('mirror-job-failures').classList.toggle('empty',!shown.length);
  $('mirror-failure-count').textContent=number(queued.count)+' 个文件待补齐'+(catalogFailures.length?' · 清单扫描失败 '+number(catalogFailures.length)+' 项':'');
  $('mirror-transfer-capacity').textContent='活跃 '+number((op.transfers||[]).length)+' / 后台最多 '+Math.max(1,s.concurrency-2)+' 路 · 全站上限 '+s.concurrency;
  const phaseText={catalogue:'读取'+({manifest:'Mojang 版本总目录',versions:'各版本文件目录',assets:'assets 资源索引',java:'Java 平台文件目录'}[progress.phase]||'版本与平台元数据')+'；只建立文件索引，不下载游戏实体。',sources:'分批核验公开来源，生成文件到来源的映射，不下载完整实体。',selection:'按平台、来源映射和类别策略生成本批处理计划。',download:'下载需要的实体文件；失败项进入待补齐队列，其他文件继续。',loader_metadata:'更新 Forge / NeoForge / Fabric / Quilt 的加载器版本目录。'};
  $('mirror-task-activity').textContent=job.state==='running'?'当前阶段：'+(phaseText[job.phase]||'处理'+(actions[job.action]||'后台任务')+'，具体传输见下方文件列表。'):data.refresh?.deferred_at?'上一轮运行时定时同步已顺延；下一次按显示时间执行。':'当前没有同步任务。用户请求触发的后台备份、云端上传仍可能继续，见下方文件列表。失败文件可单独批量重试。';
  const planRows=Object.entries(job.plan||{}).filter(([,row])=>row.files).map(([category,row])=>sourceElement('p',(mirrorCategoryNames[category]||category)+'：'+number(row.files)+' 个 · 已知 '+mirrorBytes(row.known_bytes)+' · '+(s.policies[category]==='redirect'||s.policies[category]==='disabled'?'只查来源，不存实体':mirrorDestination(s,category)+(s.qiniu_routes?.[category]?.mode==='cloud'?' · 先本机暂存再上传校验':''))));
  $('mirror-sync-plan').replaceChildren(sourceElement('strong',job.action==='inventory'?'扫描全部平台资源清单':'当前批次按类别去向'),...(planRows.length?planRows:[sourceElement('p',job.action==='inventory'?'正在整理游戏核心、Java、依赖库和 assets 的目录；此阶段不占实体缓存。':'本批次尚无实体下载计划；可能都已命中来源映射或当前无新增文件。')]));
  if(job.skip_reasons)$('mirror-sync-plan').append(sourceElement('p','实际处理：来源已覆盖 '+number(job.skip_reasons.source_mapping)+' · 纯重定向跳过 '+number(job.skip_reasons.redirect_policy)+' · 云端暂停跳过 '+number(job.skip_reasons.cloud_paused)+' · 复用已有副本 '+number(job.reused)+' · 新完成缓存 '+number(Math.max(0,Number(job.cached||0)-Number(job.reused||0)))+' · 失败 '+number(job.errors)));
  const transferRows=(op.transfers||[]).map(item=>{
    const destination=item.destination?mirrorDestination({...s,qiniu_routes:{...s.qiniu_routes,[item.category]:{mode:item.destination,profile:item.profile}}},item.category):mirrorDestination(s,item.category||'core');
    return [new URL(item.url).pathname,transferPlatform(item.url),item.source_host||new URL(item.url).hostname,destination,mirrorSize(item.bytes)+(item.total?' / '+mirrorSize(item.total):''),qualitySpeed(item.speed),qualityTime(item.started_at)];
  });
  const makeRow=values=>{const tr=sourceElement('tr');for(const value of values)tr.append(sourceElement('td',value));return tr;};
  $('mirror-transfers').replaceChildren(...(transferRows.length?transferRows:[['当前没有实体文件下载','—','—','扫描映射或云端上传请查看各自任务状态','—','—','—']]).map(makeRow));
  const scan=op.source_scan||{};
  const scanCard=sourceElement('div');scanCard.append(sourceElement('strong','来源映射扫描'),sourceElement('p',op.source_scanning?'正在分批核验来源；不影响文件下载。':scan.finished_at?'最近完成：'+qualityTime(scan.finished_at)+' · '+({finished:'已完成',failed:'失败'}[scan.state]||scan.state):'尚未开始扫描'));
  if(op.source_scanning&&scan.progress){const p=scan.progress;scanCard.append(sourceElement('p',p.phase==='listing'?'正在读取仓库目录：'+(p.source||'准备中'):'本批校验 '+number(p.done)+' / '+number(p.total)+' 项（每批最多 128 项）'));for(const item of p.active||[])scanCard.append(sourceElement('small',item.source+' · '+item.path));}
  if(scan.error)scanCard.append(sourceElement('small','错误：'+scan.error+'；可在「来源覆盖」重新扫描。'));
  const reserveCard=sourceElement('div');reserveCard.append(sourceElement('strong','下载空间预留'),sourceElement('p',mirrorSize(op.reserved_bytes)+' 已预留给正在下载的文件，完成或失败后释放；它不是已缓存文件。'));
  $('mirror-background-activities').replaceChildren(scanCard,reserveCard);
  $('mirror-job-history').replaceChildren(...(op.history?.length?op.history:[{action:'暂无记录',state:'idle'}]).map(item=>{
    const row=makeRow([actions[item.action]||item.action||'同步',states[item.state]||item.state,'完成 '+number(item.done)+' / '+number(item.total)+' · 失败 '+number(item.errors),qualityTime(item.started_at)+(item.finished_at?' / '+qualityTime(item.finished_at):' / 进行中')]);
    const examples=[...(item.failed||[]),...(item.catalog_errors||[]),...(item.report?.errors||[])];
    if(examples.length){const details=sourceElement('details');details.append(sourceElement('summary','查看 '+number(examples.length)+' 项原因'),...examples.slice(-8).map(mirrorFailure));row.children[2].append(details);}
    return row;
  }));
  const report=data.inventory||job.report;
  $('mirror-report').textContent=JSON.stringify(report?{...report,catalogue_potential_GiB:Number((report.known_bytes/1024**3).toFixed(2)),policy_selected_potential_GiB:data.selection?Number((data.selection.local_bytes/1024**3).toFixed(2)):undefined,actual_cached_GiB:Number((data.cached_bytes/1024**3).toFixed(2)),job}:job,null,2);
  const busy=job.state==='running';$('mirror-inventory').disabled=!data.running||busy;$('mirror-sync').disabled=!data.running||busy;$('mirror-prefetch').disabled=!data.running||busy||!report?.complete;$('mirror-retry').disabled=!data.running||busy||!queued.count;$('mirror-cancel').disabled=!busy;
}
let mirrorLoading=false,mirrorViewInitialized=false,lastMirrorPoll=0,mirrorPollFailures=0;
async function loadMirror(){
  if(mirrorLoading)return;
  mirrorLoading=true;
  try{
    const data=await api('v1/admin/mirror');
    renderMirror(data);
    if(!mirrorViewInitialized&&data.running){mirrorViewInitialized=true;await loadMirrorView();}
  }finally{mirrorLoading=false;}
}
$('mirror-refresh').onclick=()=>{void (async()=>{const loaded=mirrorViewInitialized;await loadMirror();if(loaded)await loadMirrorView();})().catch(showError);};
function mirrorSettings(){
  return {...qiniuSettings(),...roomCacheSettings(),...qualityStore.payload(),public_url:$('mirror-url').value.trim(),prefetch_platform:prefetchStore.payload(),concurrency:Number($('mirror-concurrency').value),metadata_ttl:Number($('mirror-ttl').value),enabled:$('mirror-enabled').checked,auto_refresh:$('mirror-auto-refresh').checked,refresh_minutes:Number($('mirror-refresh-minutes').value),node_weight:Number($('mirror-node-weight').value),monthly_egress_gib:Number($('mirror-monthly-egress').value),egress_threshold:Number($('mirror-egress-threshold').value),bandwidth_mbps:Number($('mirror-bandwidth').value),max_serving:Number($('mirror-max-serving').value),nodes:$('mirror-nodes').value.split('\n').map(line=>line.trim()).filter(Boolean).map(line=>{const parts=line.split(/\s+/);if(parts.length>3||parts[2]&&parts[2]!=='off')throw Error('节点每行应为：HTTPS 地址、权重，以及可选的 off。');return {url:parts[0],weight:Number(parts[1]||1),enabled:parts[2]!=='off'};}),auto_sources:$('mirror-auto-sources').checked,source_prefer_cn:$('mirror-source-prefer-cn').checked,source_cleanup:$('mirror-source-cleanup').checked,source_registries:readSourceProviders()};
}
$('mirror-settings').onsubmit=async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;try{renderMirror(await api('v1/admin/mirror',mirrorSettings()),true);message('资源分发设置已保存。');}catch(e){showError(e);}finally{button.disabled=false;}};

for(const action of ['inventory','prefetch','cancel','refresh','retry_failures'])$(action==='refresh'?'mirror-sync':action==='retry_failures'?'mirror-retry':'mirror-'+action).onclick=async event=>{const button=event.currentTarget;button.disabled=true;try{renderMirror(await api('v1/admin/mirror',{action}));message(action==='cancel'?'后台同步任务已停止；正在提供服务的文件保留。':'后台任务已提交，可刷新查看进度。');}catch(e){showError(e);button.disabled=false;}};
const mirrorPoll=setInterval(()=>{
  if(document.visibilityState!=='visible'||$('panel').classList.contains('hidden'))return;
  const view=$('panel').dataset.mirrorView,interval=Math.max(['overview','tasks','transfers','temporary'].includes(view)?5000:15000,mirrorPollFailures?Math.min(60000,15000*mirrorPollFailures):0);
  if(Date.now()-lastMirrorPoll<interval)return;
  lastMirrorPoll=Date.now();
  void loadMirror().then(()=>{mirrorPollFailures=0;}).catch(error=>{if(!scope.active||error.name==='AbortError')return;mirrorPollFailures++;const host=$('mirror-state');if(host)host.textContent='自动刷新未完成：'+error.message+' 旧数据已保留，稍后自动重试。';});
},5000);

let sourceOffset=0,sourceNext=null;
function sourceElement(tag,text){const node=document.createElement(tag);if(text!=null)node.textContent=String(text);return node;}
let cachePage=0,cacheCursors=[null],cacheNext=null,roomPage=0,roomCursors=[null],roomNext=null;
function observationRow(values){const row=sourceElement('tr');for(const value of values){const cell=sourceElement('td');if(value instanceof Node)cell.append(value);else cell.textContent=String(value);row.append(cell);}return row;}
function fileNameCell(path,hash){const host=sourceElement('div');host.className='observation-file';host.append(sourceElement('strong',path||'未知文件'),sourceElement('small',hash||'未记录校验值'));return host;}
async function loadCacheFiles(){
  const data=await api('v1/admin/mirror',{action:'cache_files',query:$('cache-files-query').value.trim(),category:$('cache-files-category').value,cursor:cacheCursors[cachePage]});
  cacheNext=data.next_cursor;
  const rows=(data.files||[]).map(file=>observationRow([fileNameCell(file.path,file.sha1||file.object),({core:'游戏核心',loaders:'加载器',mods:'模组',java:'Java',libraries:'依赖库',assets:'原版资源'})[file.category]||file.category,(file.size/1024**2).toFixed(2)+' MiB',[(file.local?'本地':'无本地副本'),file.cloud_profiles?'云端 '+file.cloud_profiles:''].filter(Boolean).join(' · '),qualityTime(file.fetched)]));
  $('cache-files').replaceChildren(...(rows.length?rows:[observationRow(['当前筛选没有已缓存文件','—','—','—','—'])]));
  $('cache-files-count').textContent='第 '+(cachePage+1)+' 页 · 本页 '+rows.length+' 个文件；仅显示实际缓存索引，不把重定向映射算作本地文件。';
  $('cache-files-page').textContent='第 '+(cachePage+1)+' 页';$('cache-files-prev').disabled=cachePage===0;$('cache-files-next').disabled=!cacheNext;
}
async function loadRoomFiles(){
  const data=await api('v1/admin/mirror',{action:'room_files',query:$('room-files-query').value.trim(),cursor:roomCursors[roomPage]});
  roomNext=data.next_cursor;
  const states={uploading:'正在补传',cached:'已缓存',awaiting_upload:'等待补传',redirect:'外部直下',expired:'已到期'};
  const rows=(data.files||[]).map(file=>observationRow([fileNameCell(file.path,file.digest),(states[file.state]||file.state)+' · '+(file.reason||'按来源计划'),(file.size/1024**2).toFixed(2)+' / '+((file.stored_bytes||0)/1024**2).toFixed(2)+' MiB',[(file.stored_bytes?'本地缓存':''),file.cloud_profiles?'云端 '+file.cloud_profiles:'',file.state==='uploading'?'正在上传':''].filter(Boolean).join(' · ')||'不占本地空间',qualityTime(file.expires_at)+' · '+number(file.references_count)+' 处引用'+(file.readers?' · '+number(file.readers)+' 个读取':'')]));
  $('room-files').replaceChildren(...(rows.length?rows:[observationRow(['当前筛选没有临时文件','—','—','—','—'])]));
  $('room-files-count').textContent='第 '+(roomPage+1)+' 页 · 本页 '+rows.length+' 个文件；按哈希合并多房间引用。';
  $('room-files-page').textContent='第 '+(roomPage+1)+' 页';$('room-files-prev').disabled=roomPage===0;$('room-files-next').disabled=!roomNext;
}
function loadMirrorView(){const view=$('panel').dataset.mirrorView;return view==='resources'?loadCacheFiles():view==='temporary'?loadRoomFiles():view==='coverage'?loadSourceFiles(0):Promise.resolve();}
scope.listen(window,'buggcraft:mirror-view',()=>{if(mirrorViewInitialized)void loadMirrorView().catch(showError);});
$('cache-files-filter').onsubmit=event=>{event.preventDefault();cachePage=0;cacheCursors=[null];void loadCacheFiles().catch(showError);};
$('cache-files-prev').onclick=()=>{if(cachePage){cachePage--;void loadCacheFiles().catch(showError);}};
$('cache-files-next').onclick=()=>{if(cacheNext){cacheCursors[++cachePage]=cacheNext;void loadCacheFiles().catch(showError);}};
$('room-files-filter').onsubmit=event=>{event.preventDefault();roomPage=0;roomCursors=[null];void loadRoomFiles().catch(showError);};
$('room-files-prev').onclick=()=>{if(roomPage){roomPage--;void loadRoomFiles().catch(showError);}};
$('room-files-next').onclick=()=>{if(roomNext){roomCursors[++roomPage]=roomNext;void loadRoomFiles().catch(showError);}};
function sourceSelect(values,value){const node=sourceElement('select');for(const [key,title] of values){const option=sourceElement('option',title);option.value=key;node.append(option);}node.value=value;return node;}
function addSourceProvider(value){
  const row=sourceElement('div');row.className='source-provider';row.dataset.id=value.id;
  for(const [key,label] of [['name','名称'],['url','仓库 / 清单地址'],['kind','格式'],['region','地区']]){
    const field=sourceElement('label',label);
    const input=key==='kind'?sourceSelect([['maven','Maven 仓库'],['minecraft','Minecraft 源'],['catalogue','文件清单 JSON']],value[key]):key==='region'?sourceSelect([['cn','国内'],['global','其他地区']],value[key]):sourceElement('input');
    if(key==='name'||key==='url'){input.type=key==='url'?'url':'text';input.value=value[key];input.maxLength=key==='url'?1400:80;input.required=true;}
    input.dataset.field=key;field.append(input);row.append(field);
  }
  const field=sourceElement('label');const input=sourceElement('input');input.type='checkbox';input.checked=value.enabled;input.dataset.field='enabled';field.append(input,document.createTextNode(' 启用'));row.append(field);
  const remove=sourceElement('button','移除');remove.type='button';remove.className='secondary';remove.onclick=()=>row.remove();row.append(remove);$('mirror-source-providers').append(row);
}
function renderSourceProviders(values){$('mirror-source-providers').replaceChildren();for(const value of values)addSourceProvider(value);}
function readSourceProviders(){return [...$('mirror-source-providers').children].map(row=>{const value={id:row.dataset.id};for(const input of row.querySelectorAll('[data-field]'))value[input.dataset.field]=input.type==='checkbox'?input.checked:input.value.trim();return value;});}
function renderSourceStatistics(data){
  const providers=data.settings.source_registries||[],counts=new Map((data.source_statistics||[]).map(row=>[row.source,row]));
  const verified=new Map((data.automatic_sources?.providers||[]).map(row=>[row.id,row]));
  const table=$('mirror-source-statistics');table.replaceChildren();
  table.closest('table').querySelector('thead tr').replaceChildren(...['来源','已核验文件','本地副本数','本地占用','下载重定向','文件明细'].map(label=>sourceElement('th',label)));
  for(const source of providers){
    const stats=counts.get(source.id)||{},check=verified.get(source.id)||{},tr=sourceElement('tr');
    for(const value of [source.name+(source.enabled?'':' · 停用'),number(stats.files??check.verified??0),number(stats.local_files||0),mirrorSize(stats.local_bytes),number(stats.redirects||0)])tr.append(sourceElement('td',value));
    const cell=sourceElement('td'),button=sourceElement('button','查看文件');button.type='button';button.className='secondary';button.disabled=!data.running;button.onclick=()=>{$('mirror-source-filter').value=source.id;void loadSourceFiles(0).catch(showError);};cell.append(button);tr.append(cell);table.append(tr);
  }
  const filter=$('mirror-source-filter'),selection=filter.value,options=[sourceElement('option','全部来源')];options[0].value='';
  for(const source of providers){const option=sourceElement('option',source.name);option.value=source.id;options.push(option);}filter.replaceChildren(...options);filter.value=selection;
  const status=data.automatic_sources||{};
  const sourceNotice=(status.generated_at?'最近扫描 '+new Date(status.generated_at*1000).toLocaleString('zh-CN')+' · 覆盖 '+number(status.matched)+' 个文件 · 待核验 '+number(status.remaining):'内置来源已就绪；等待完整资源清单，后台将分批核验并生成映射。')+(status.last_error?' · 扫描错误：'+status.last_error+'。旧映射保留，可手动重新扫描。':'');
  $('mirror-source-notice').textContent=sourceNotice;
  $('mirror-source-notice-settings').textContent=sourceNotice;
  $('mirror-source-scan').disabled=!data.running||data.settings.auto_sources===false;
  $('mirror-source-preview').disabled=!data.running;
}
async function loadSourceFiles(offset=0){
  const data=await api('v1/admin/mirror',{action:'source_files',source:$('mirror-source-filter').value,offset});
  sourceOffset=data.offset;sourceNext=data.next_offset;
  $('mirror-source-file-count').textContent='共 '+number(data.total)+' 条 · 每页 50 条 · 当前 '+(data.total?offset+1:0)+'–'+Math.min(offset+50,data.total);
  const table=$('mirror-source-files');table.replaceChildren();
  for(const file of data.files){
    const tr=sourceElement('tr'),cell=sourceElement('td');cell.className='source-file-path';
    const link=sourceElement('a',file.path);link.href=file.target;link.target='_blank';link.rel='noopener';cell.append(link,sourceElement('small',file.sha1));tr.append(cell,sourceElement('td',file.source),sourceElement('td',file.local_bytes==null?'无':mirrorBytes(file.local_bytes)),sourceElement('td',number(file.redirects)));table.append(tr);
  }
  $('mirror-source-prev').disabled=offset===0;$('mirror-source-next').disabled=data.next_offset==null;
}
$('mirror-source-add').onclick=()=>addSourceProvider({id:'custom-'+crypto.randomUUID(),name:'新来源',url:'',kind:'maven',region:'cn',enabled:true});
$('mirror-source-filter').onchange=()=>void loadSourceFiles(0).catch(showError);
$('mirror-source-prev').onclick=()=>void loadSourceFiles(Math.max(0,sourceOffset-50)).catch(showError);
$('mirror-source-next').onclick=()=>void loadSourceFiles(sourceNext||0).catch(showError);
$('mirror-source-scan').onclick=async event=>{const button=event.currentTarget;button.disabled=true;try{renderMirror(await api('v1/admin/mirror',{action:'source_scan'}));message('增量扫描已提交，结果自动更新；未扫描资源清单时，请先在存储与同步中扫描清单。');}catch(error){showError(error);}finally{button.disabled=false;}};
$('mirror-source-preview').onclick=async()=>{try{const data=await api('v1/admin/mirror',{action:'source_preview'});$('mirror-source-cleanup-preview').textContent='已匹配 '+number(data.files)+' 个本地副本，预计释放 '+mirrorBytes(data.reclaimable_bytes)+'。执行时会再次核验；共享对象及正在使用的文件可能减少实际释放量。';$('mirror-source-clean').disabled=!data.files;$('mirror-source-cleanup-files').replaceChildren(...(data.examples||[]).map(row=>sourceElement('li',row.path)));}catch(error){showError(error);}};
$('mirror-source-clean').onclick=async event=>{const button=event.currentTarget;button.disabled=true;try{renderMirror(await api('v1/admin/mirror',{action:'source_cleanup'}));message('清理任务已提交，正在重新核验外部文件；进度见存储与同步。');}catch(error){showError(error);}};

const qualityGrades={excellent:'优',good:'良',poor:'待优化',observing:'观察中',stale:'已过期'};
function qualitySpeed(value){return value==null?'尚无数据':value>=1024**2?(value/1024**2).toFixed(2)+' MiB/s':(value/1024).toFixed(0)+' KiB/s';}
function qualityTier(value){const kib=Number(value||0)/1024;return !value?'待采样':kib<64?'低于 64 KiB/s':kib<128?'64–128 KiB/s':kib<256?'128–256 KiB/s':kib<512?'256–512 KiB/s':kib<1024?'512–1024 KiB/s':kib<2048?'1–2 MiB/s':kib<4096?'2–4 MiB/s':'4 MiB/s 以上';}
function qualityTime(value){return value?new Date(value*1000).toLocaleString('zh-CN'):'等待安排';}
function renderQuality(data){qualityStore.hydrate(data);}
