"use strict";
function mirrorBytes(value){return (Number(value||0)/1024**3).toFixed(2)+' GiB';}
const mirrorCategories=['core','loaders','mods','java','libraries','assets'];
const roomCacheFields=['ttl_hours','quota_mib','max_file_mib','slow_kibps','probe_hours','probe_daily_mib'];
function roomCacheSettings(){
  return {room_cache_enabled:$('room-cache-enabled').checked,room_cache_probe_enabled:$('room-cache-probe-enabled').checked,
    ...Object.fromEntries(roomCacheFields.map(key=>['room_cache_'+key,Number($('room-cache-'+key.replaceAll('_','-')).value)]))};
}
function renderRoomCache(data,fill){
  const s=data.settings,state=data.room_client_cache||{};
  if(fill){for(const key of roomCacheFields)$('room-cache-'+key.replaceAll('_','-')).value=s['room_cache_'+key];
    $('room-cache-enabled').checked=s.room_cache_enabled;$('room-cache-probe-enabled').checked=s.room_cache_probe_enabled;}
  $('room-cache-state').textContent=!data.running?'资源进程离线，设置仍可保存。':!state.configured?'尚未配置节点授权密钥；不会开放匿名上传。':s.room_cache_enabled?'节点授权已配置，按房间权限提供兜底。':'客户端文件兜底已关闭。';
  metrics($('room-cache-metrics'),[['临时文件',number(state.files),'按 SHA-256 去重'],['临时占用',mirrorBytes(state.bytes),'上限 '+s.room_cache_quota_mib+' MiB'],['正在补传',number(state.uploads),'最多同时 2 个'],['正在读取',number(state.downloads),'到期清理会跳过正在读取的文件']]);
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
  $('mirror-state').textContent=data.running?'资源下载在线 · 缓存目录 '+data.root:'资源下载未启动 · 可保存设置，翻译配置与任务继续独立运行。';
  metrics($('mirror-metrics'),[['已缓存文件',number(data.cached_files),'完成文件才会对外发布'],['本地缓存',mirrorBytes(data.cached_bytes),'缓存上限 '+s.quota_gib+' GiB'],['磁盘可用',data.disk_free==null?'进程离线':mirrorBytes(data.disk_free),'保留 '+s.reserve_gib+' GiB'],['当前下载',number(data.active_downloads),'并发上限 '+s.concurrency]]);
  if(fillSettings){$('mirror-url').value=s.public_url;$('mirror-quota').value=s.quota_gib;$('mirror-reserve').value=s.reserve_gib;$('mirror-concurrency').value=s.concurrency;$('mirror-ttl').value=s.metadata_ttl;$('mirror-enabled').checked=s.enabled;for(const key of mirrorCategories)$('mirror-policy-'+key).value=s.policies[key];mirrorLoaded=true;}
  if(fillSettings){
    for(const [id,key] of [['mirror-protect','protect_latest'],['mirror-refresh-minutes','refresh_minutes'],['mirror-node-weight','node_weight'],['mirror-eviction','eviction'],['mirror-monthly-egress','monthly_egress_gib'],['mirror-egress-threshold','egress_threshold'],['mirror-bandwidth','bandwidth_mbps'],['mirror-max-serving','max_serving']])$(id).value=s[key];
    $('mirror-auto-refresh').checked=s.auto_refresh;
    $('quality-enabled').checked=s.quality_enabled;$('quality-adaptive-cache').checked=s.quality_adaptive_cache;
    for(const key of ['interval_hours','daily_mib','sample_kib','fast_kibps','slow_kibps'])$('quality-'+key.replaceAll('_','-')).value=s['quality_'+key];
    $('mirror-nodes').value=(s.nodes||[]).map(n=>n.url+' '+n.weight+(n.enabled?'':' off')).join('\n');
    renderSourceProviders(s.source_registries||[]);
    $('mirror-auto-sources').checked=s.auto_sources!==false;$('mirror-source-prefer-cn').checked=s.source_prefer_cn!==false;$('mirror-source-cleanup').checked=!!s.source_cleanup;
  }
  for(const key of mirrorCategories)$('mirror-policy-'+key).closest('label').dataset.mode=$('mirror-policy-'+key).value;
  const schedule=s.auto_refresh?'下次自动同步：'+(data.refresh?.next_at?new Date(data.refresh.next_at*1000).toLocaleString('zh-CN'):'正在安排'):'定时同步已关闭';
  $('mirror-overview-schedule').textContent=schedule;
  $('mirror-task-schedule').textContent=schedule+' · 周期 '+s.refresh_minutes+' 分钟';
  const last=data.refresh?.last;
  $('mirror-overview-last').textContent=last?'上次同步：'+qualityTime(last.at)+' · '+(last.state||'已完成'):'尚无自动同步记录';
  const room=data.room_client_cache||{};
  $('room-cache-overview').textContent=number(room.files)+' 个实际文件 · '+mirrorBytes(room.bytes)+' / '+(s.room_cache_quota_mib||0)+' MiB 限额';
  $('mirror-storage-bars').replaceChildren(...[['镜像缓存',data.cached_bytes,s.quota_gib*1024**3],['客户端临时缓存',room.bytes,s.room_cache_quota_mib*1024**2]].map(([label,used,limit])=>{
    const host=sourceElement('div');host.className='mirror-storage-row';const top=sourceElement('div');top.append(sourceElement('strong',label),sourceElement('span',mirrorBytes(used)+' / '+mirrorBytes(limit)));
    const meter=document.createElement('meter');meter.min=0;meter.max=Number(limit)||1;meter.value=Math.min(Number(used)||0,Number(limit)||1);host.append(top,meter);return host;
  }));
  $('mirror-node-status').replaceChildren(...(data.nodes||[]).map(n=>{const span=document.createElement('span');span.textContent=new URL(n.url).hostname+' · '+(!n.enabled?'停用':n.healthy?'在线':'检查中 / 不可用');return span;}));
  $('mirror-egress-state').textContent=data.egress?`本月（UTC ${data.egress.period}）已接受资源 ${mirrorBytes(data.egress.bytes)} · 最近一分钟 ${mirrorBytes(data.egress.window_bytes)} · ${data.accepting?'本机可继续接收':'将优先转移下载'}`:'镜像上线后显示资源出口统计';
  renderSourceStatistics(data);renderQuality(data);renderRoomCache(data,fillSettings);renderCloud(data.cloud_storage||{},s,fillSettings);mirrorRules(s.public_url);
  const states={idle:'空闲',running:'运行中',finished:'已完成',failed:'失败',cancelled:'已停止',disk_limit:'已到空间保护线',incomplete:'部分完成，请检查错误后重试'};
  const actions={refresh:'自动同步',inventory:'清单扫描',prefetch:'预下载',source_cleanup:'本地副本清理',source_scan:'来源映射扫描'};
  const job=data.job||{state:'idle'};
  const op=data.operations||{};
  $('mirror-overview-job').textContent=job.action?((actions[job.action]||job.action)+' · '+(states[job.state]||job.state)):'暂无正在执行的同步任务';
  $('mirror-job-state').textContent=(states[job.state]||job.state)+(job.total!=null?` · ${number(job.done)} / ${number(job.total)}`:'')+(job.skipped!=null?` · 策略跳过 ${number(job.skipped)} · 下载失败 ${number(job.errors)}`:'')+(job.error?' · '+job.error:'');
  if(job.action==='source_cleanup')$('mirror-job-state').textContent='本地副本清理 · '+(states[job.state]||job.state)+' · 已检查 '+number(job.done)+' / '+number(job.total)+' · 移除 '+number(job.removed)+' · 释放 '+mirrorBytes(job.freed_bytes)+' · 跳过 '+number(job.skipped);
  const progress=job.progress||{};
  const processed=Number(job.done??progress.done??0),total=Number(job.total??progress.total??0);
  $('mirror-job-progress').replaceChildren();
  if(total>0){const label=sourceElement('p',number(processed)+' / '+number(total)+' 个文件 · '+Math.min(100,processed/total*100).toFixed(1)+'%');const bar=document.createElement('progress');bar.max=total;bar.value=Math.min(processed,total);$('mirror-job-progress').append(label,bar);}
  $('mirror-job-failures').replaceChildren(...(job.failed||[]).slice(-8).map(row=>sourceElement('p',(row.path||row.url||'未知文件')+' · '+(row.error||'下载失败'))));
  const transferRows=(op.transfers||[]).map(item=>[new URL(item.url).pathname,mirrorBytes(item.bytes)+(item.total?' / '+mirrorBytes(item.total):''),qualitySpeed(item.speed),qualityTime(item.started_at)]);
  const makeRow=values=>{const tr=sourceElement('tr');for(const value of values)tr.append(sourceElement('td',value));return tr;};
  $('mirror-transfers').replaceChildren(...(transferRows.length?transferRows:[['暂无正在回源的文件','—','—','—']]).map(makeRow));
  const scan=op.source_scan||{};
  $('mirror-background-activities').replaceChildren(sourceElement('p',op.source_scanning?'来源映射正在分批扫描':scan.finished_at?'上次来源映射：'+qualityTime(scan.finished_at)+' · '+({finished:'已完成',failed:'失败'}[scan.state]||scan.state):'来源映射扫描空闲'),sourceElement('p','已为进行中的下载预留 '+mirrorBytes(op.reserved_bytes)));
  $('mirror-job-history').replaceChildren(...(op.history?.length?op.history:[{action:'暂无记录',state:'idle'}]).map(item=>makeRow([actions[item.action]||item.action||'同步',states[item.state]||item.state,'完成 '+number(item.done)+' / '+number(item.total)+' · 失败 '+number(item.errors),qualityTime(item.started_at)+(item.finished_at?' / '+qualityTime(item.finished_at):' / 进行中')])));
  const report=data.inventory||job.report;
  $('mirror-report').textContent=JSON.stringify(report?{...report,known_GiB:Number((report.known_bytes/1024**3).toFixed(2)),selected_local_GiB:data.selection?Number((data.selection.local_bytes/1024**3).toFixed(2)):undefined,job}:job,null,2);
  const busy=job.state==='running';$('mirror-inventory').disabled=!data.running||busy;$('mirror-sync').disabled=!data.running||busy;$('mirror-prefetch').disabled=!data.running||busy||!report?.complete;$('mirror-cancel').disabled=!busy;
}
let mirrorLoading=false,mirrorViewInitialized=false,lastMirrorPoll=0;
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
  return {...qiniuSettings(),...roomCacheSettings(),quality_enabled:$('quality-enabled').checked,quality_adaptive_cache:$('quality-adaptive-cache').checked,...Object.fromEntries(['interval_hours','daily_mib','sample_kib','fast_kibps','slow_kibps'].map(key=>['quality_'+key,Number($('quality-'+key.replaceAll('_','-')).value)])),public_url:$('mirror-url').value.trim(),quota_gib:Number($('mirror-quota').value),reserve_gib:Number($('mirror-reserve').value),concurrency:Number($('mirror-concurrency').value),metadata_ttl:Number($('mirror-ttl').value),enabled:$('mirror-enabled').checked,policies:Object.fromEntries(mirrorCategories.map(key=>[key,$('mirror-policy-'+key).value])),eviction:$('mirror-eviction').value,protect_latest:Number($('mirror-protect').value),auto_refresh:$('mirror-auto-refresh').checked,refresh_minutes:Number($('mirror-refresh-minutes').value),node_weight:Number($('mirror-node-weight').value),monthly_egress_gib:Number($('mirror-monthly-egress').value),egress_threshold:Number($('mirror-egress-threshold').value),bandwidth_mbps:Number($('mirror-bandwidth').value),max_serving:Number($('mirror-max-serving').value),nodes:$('mirror-nodes').value.split('\n').map(line=>line.trim()).filter(Boolean).map(line=>{const parts=line.split(/\s+/);if(parts.length>3||parts[2]&&parts[2]!=='off')throw Error('节点每行应为：HTTPS 地址、权重，以及可选的 off。');return {url:parts[0],weight:Number(parts[1]||1),enabled:parts[2]!=='off'};}),auto_sources:$('mirror-auto-sources').checked,source_prefer_cn:$('mirror-source-prefer-cn').checked,source_cleanup:$('mirror-source-cleanup').checked,source_registries:readSourceProviders()};
}
$('mirror-settings').onsubmit=async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;try{renderMirror(await api('v1/admin/mirror',mirrorSettings()),true);message('资源分发设置已保存。');}catch(e){showError(e);}finally{button.disabled=false;}};
for(const key of mirrorCategories)$('mirror-policy-'+key).onchange=event=>{event.target.closest('label').dataset.mode=event.target.value;};
for(const action of ['inventory','prefetch','cancel','refresh'])$(action==='refresh'?'mirror-sync':'mirror-'+action).onclick=async event=>{const button=event.currentTarget;button.disabled=true;try{renderMirror(await api('v1/admin/mirror',{action}));message(action==='cancel'?'后台同步任务已停止；正在提供服务的文件保留。':'后台任务已提交，可刷新查看进度。');}catch(e){showError(e);button.disabled=false;}};
const mirrorPoll=setInterval(()=>{
  if(document.visibilityState!=='visible'||$('panel').classList.contains('hidden'))return;
  const view=$('panel').dataset.mirrorView,interval=['overview','tasks','temporary'].includes(view)?5000:15000;
  if(Date.now()-lastMirrorPoll<interval)return;
  lastMirrorPoll=Date.now();
  void loadMirror().catch(showError);
},5000);

let sourceOffset=0,sourceNext=null;
function sourceElement(tag,text){const node=document.createElement(tag);if(text!=null)node.textContent=String(text);return node;}
let cachePage=0,cacheCursors=[null],cacheNext=null,roomPage=0,roomCursors=[null],roomNext=null;
function observationRow(values){const row=sourceElement('tr');for(const value of values){const cell=sourceElement('td');if(value instanceof Node)cell.append(value);else cell.textContent=String(value);row.append(cell);}return row;}
function fileNameCell(path,hash){const host=sourceElement('div');host.className='observation-file';host.append(sourceElement('strong',path||'未知文件'),sourceElement('small',hash||'未记录校验值'));return host;}
async function loadCacheFiles(){
  const data=await api('v1/admin/mirror',{action:'cache_files',query:$('cache-files-query').value.trim(),category:$('cache-files-category').value,cursor:cacheCursors[cachePage]});
  cacheNext=data.next_cursor;
  const rows=(data.files||[]).map(file=>observationRow([fileNameCell(file.path,file.sha1||file.object),({core:'游戏核心',loaders:'加载器',mods:'模组',java:'Java',libraries:'依赖库',assets:'原版资源'})[file.category]||file.category,(file.size/1024**2).toFixed(2)+' MiB',[(file.local?'本地':'无本地副本'),file.cloud_profiles?'七牛 '+file.cloud_profiles:''].filter(Boolean).join(' · '),qualityTime(file.fetched)]));
  $('cache-files').replaceChildren(...(rows.length?rows:[observationRow(['当前筛选没有已缓存文件','—','—','—','—'])]));
  $('cache-files-count').textContent='第 '+(cachePage+1)+' 页 · 本页 '+rows.length+' 个文件；仅显示实际缓存索引，不把重定向映射算作本地文件。';
  $('cache-files-page').textContent='第 '+(cachePage+1)+' 页';$('cache-files-prev').disabled=cachePage===0;$('cache-files-next').disabled=!cacheNext;
}
async function loadRoomFiles(){
  const data=await api('v1/admin/mirror',{action:'room_files',query:$('room-files-query').value.trim(),cursor:roomCursors[roomPage]});
  roomNext=data.next_cursor;
  const states={uploading:'正在补传',cached:'已缓存',awaiting_upload:'等待补传',redirect:'外部直下',expired:'已到期'};
  const rows=(data.files||[]).map(file=>observationRow([fileNameCell(file.path,file.digest),(states[file.state]||file.state)+' · '+(file.reason||'按来源计划'),(file.size/1024**2).toFixed(2)+' / '+((file.stored_bytes||0)/1024**2).toFixed(2)+' MiB',[(file.stored_bytes?'本地缓存':''),file.cloud_profiles?'七牛 '+file.cloud_profiles:'',file.state==='uploading'?'正在上传':''].filter(Boolean).join(' · ')||'不占本地空间',qualityTime(file.expires_at)+' · '+number(file.references_count)+' 处引用'+(file.readers?' · '+number(file.readers)+' 个读取':'')]));
  $('room-files').replaceChildren(...(rows.length?rows:[observationRow(['当前筛选没有临时文件','—','—','—','—'])]));
  $('room-files-count').textContent='第 '+(roomPage+1)+' 页 · 本页 '+rows.length+' 个文件；按哈希合并多房间引用。';
  $('room-files-page').textContent='第 '+(roomPage+1)+' 页';$('room-files-prev').disabled=roomPage===0;$('room-files-next').disabled=!roomNext;
}
function loadMirrorView(){const view=$('panel').dataset.mirrorView;return view==='resources'?loadCacheFiles():view==='temporary'?loadRoomFiles():view==='coverage'?loadSourceFiles(0):Promise.resolve();}
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
  for(const source of providers){
    const stats=counts.get(source.id)||{},check=verified.get(source.id)||{},tr=sourceElement('tr');
    for(const value of [source.name+(source.enabled?'':' · 停用'),number(stats.files??check.verified??0),number(stats.local_files||0)+' · '+mirrorBytes(stats.local_bytes),number(stats.redirects||0)])tr.append(sourceElement('td',value));
    const cell=sourceElement('td'),button=sourceElement('button','查看文件');button.type='button';button.className='secondary';button.disabled=!data.running;button.onclick=()=>{$('mirror-source-filter').value=source.id;void loadSourceFiles(0).catch(showError);};cell.append(button);tr.append(cell);table.append(tr);
  }
  const filter=$('mirror-source-filter'),selection=filter.value,options=[sourceElement('option','全部来源')];options[0].value='';
  for(const source of providers){const option=sourceElement('option',source.name);option.value=source.id;options.push(option);}filter.replaceChildren(...options);filter.value=selection;
  const status=data.automatic_sources||{};
  const sourceNotice=(status.generated_at?'最近扫描 '+new Date(status.generated_at*1000).toLocaleString('zh-CN')+' · 覆盖 '+number(status.matched)+' 个文件 · 待核验 '+number(status.remaining):'内置来源已就绪；等待完整资源清单，后台将分批核验并生成映射。')+(status.last_error?' · 部分来源暂不可用，将自动重试。':'');
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
function qualityTime(value){return value?new Date(value*1000).toLocaleString('zh-CN'):'等待安排';}
function qualityButton(text,handler){const button=sourceElement('button',text);button.type='button';button.className='secondary';button.onclick=async()=>{button.disabled=true;try{await handler();}catch(error){showError(error);}finally{button.disabled=false;}};return button;}
async function queueQuality(source){renderMirror(await api('v1/admin/mirror',{action:'quality_probe',...(source?{source}:{})}));message('已安排错峰采样；仍受单次间隔、每日预算和启用开关约束。');}
async function qualityHistory(source){
  $('quality-history-title').textContent=source.name+' · 最近 40 次采样';$('quality-history-panel').hidden=false;
  const data=await api('v1/admin/mirror',{action:'quality_history',source:source.id});
  $('quality-history-title').textContent=source.name+' · 最近 40 次采样';$('quality-history-panel').hidden=false;
  $('quality-history').replaceChildren(...(data.samples||[]).map(sample=>{const row=sourceElement('tr');for(const value of [qualityTime(sample.at),{ok:'成功',missing:'文件未覆盖',error:'连接或传输失败'}[sample.outcome]||sample.outcome,qualitySpeed(sample.outcome==='ok'?sample.speed:null),sample.ttfb_ms==null?'—':sample.ttfb_ms.toFixed(0)+' ms',(sample.bytes/1024).toFixed(0)+' KiB'])row.append(sourceElement('td',value));return row;}));
  $('quality-history-panel').scrollIntoView({behavior:'smooth',block:'nearest'});
}
function renderQuality(data){
  const quality=data.source_quality||{};
  $('quality-budget').textContent=data.running?`今日采样预算：${((quality.daily_bytes||0)/1024**2).toFixed(2)} / ${((quality.daily_limit||0)/1024**2).toFixed(0)} MiB · 观测节点：${quality.observer||data.settings.public_url}`:'镜像进程启动后开始采样，历史记录保留在镜像数据库。';
  $('quality-probe-all').disabled=!data.running||!data.settings.quality_enabled;
  $('quality-sources').replaceChildren(...(data.settings.source_registries||[]).map(source=>{
    const q=quality.sources?.[source.id]||{},row=sourceElement('tr'),name=sourceElement('td');
    name.append(sourceElement('strong',source.name),sourceElement('small',(source.enabled?(qualityGrades[q.grade]||'观察中'):'已停用')+(q.cache_priority?' · 优先缓存':'')));name.className='source-quality-name';
    row.append(name,sourceElement('td',qualitySpeed(q.speed_bps)),sourceElement('td',(q.success_rate==null?'—':(q.success_rate*100).toFixed(1)+'%')+' · '+(q.samples||0)+' 次 / '+(q.days||0)+' 天'),sourceElement('td',source.enabled?qualityTime(q.next_at):'—'));
    const actions=sourceElement('td');actions.className='quality-actions';
    const probe=qualityButton('采样',()=>queueQuality(source.id));probe.disabled=!data.running||!source.enabled||!data.settings.quality_enabled;
    const history=qualityButton('记录',()=>qualityHistory(source));history.disabled=!data.running;
    const toggle=qualityButton(source.enabled?'停用':'启用',async()=>{const providers=readSourceProviders().map(item=>item.id===source.id?{...item,enabled:!source.enabled}:item);renderMirror(await api('v1/admin/mirror',{source_registries:providers}),true);message('来源状态已更新。');});
    actions.append(probe,history,toggle);row.append(actions);return row;
  }));
}
$('quality-probe-all').onclick=()=>void queueQuality().catch(showError);
$('quality-history-close').onclick=()=>{$('quality-history-panel').hidden=true;};
