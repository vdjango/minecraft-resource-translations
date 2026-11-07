import {defineStore} from 'pinia';
import {computed,reactive,ref} from 'vue';
import {useRoomClientCacheStore} from './roomClientCache';
import {useNotificationsStore} from './notifications';

export const cloudKinds=[['core','游戏核心'],['loaders','加载器'],['mods','模组'],['java','Java 运行时'],['libraries','依赖库'],['assets','原版资源'],['room_mods','客户端 · 模组'],['room_resourcepacks','客户端 · 资源包'],['room_shaderpacks','客户端 · 光影']];
export type CloudProfile={id:string;name?:string;provider?:string;bucket:string;domain:string;prefix:string;upload_host:string;private:boolean;quota_gib:number;daily_upload_gib:number;endpoint?:string;region?:string;addressing_style?:string;quota_unit?:string;monthly_egress_gib?:number;egress_threshold?:number;tls_auto?:boolean};

export function providerName(provider?:string){return provider==='rainyun'?'雨云':provider==='s3'?'S3 兼容':'七牛云';}
export function profileName(profile:CloudProfile){return `${providerName(profile.provider)} · ${profile.name||profile.bucket||profile.id}（${profile.id}）`;}
function fingerprint(value:unknown):string{return JSON.stringify(value,(_key,item)=>item&&typeof item==='object'&&!Array.isArray(item)?Object.fromEntries(Object.entries(item).sort(([a],[b])=>a.localeCompare(b))):item);}
type Route={mode:string;profile:string;path:string};
export const useCloudStorageStore=defineStore('cloud-storage',()=>{
  const roomCache=useRoomClientCacheStore();
  const notices=useNotificationsStore();
  const local=reactive({quota_gib:20,reserve_gib:10,eviction:'oldest_version',protect_latest:2});
  const usage=ref({bytes:0,files:0,roomBytes:0,roomLocalBytes:0,roomStagingBytes:0,free:0,reserved:0});
  const profiles=ref<CloudProfile[]>([]),routes=ref<Record<string,Route>>({});
  const policies=ref<Record<string,string>>({core:'cache',loaders:'cache',mods:'redirect',java:'backup',libraries:'backup',assets:'backup'});
  const paused=ref(true),signedMinutes=ref(240),ready=ref(false),busy=ref(false),feedback=ref(''),error=ref('');
  const saving=ref(false),attempted=ref(false),saved=ref('');
  const snapshot=ref<any>({profiles:[],jobs:[],paused:true});
  const delivery=ref<any>({policies:{},routes:{},profiles:[],prefetch_platform:'windows',job:{}});
  const migrateRoute=ref('core'),migrateLimit=ref(20);
  const uploadChanging=ref(false),jobsLoading=ref(false),jobsError=ref('');
  const jobFilter=reactive({route:'',state:'',offset:0});
  const jobPage=ref<any>({jobs:[],total:0,offset:0,limit:20});
  const cleanupPage=ref<any>({files:[],next_cursor:null,bytes:0});
  const cleanupSelected=ref<string[]>([]),cleanupLoading=ref(false),cleanupReady=ref(false);
  let jobRequest=0;
  const copies=computed(()=>snapshot.value.profiles.reduce((n:number,p:any)=>n+(p.files||0),0));
  const bytes=computed(()=>snapshot.value.profiles.reduce((n:number,p:any)=>n+(p.bytes||0),0));
  const dirty=computed(()=>ready.value&&fingerprint(payload())!==saved.value);
  const routeErrors=computed(()=>Object.fromEntries(cloudKinds.map(([key,label])=>{
    const route=routes.value[key],profile=profiles.value.find(p=>p.id===route?.profile);
    const message=!route?'配置尚未加载':route.mode!=='local'&&!profile?`${label}：请选择存储空间。`:key.startsWith('room_')&&route.mode!=='local'&&!profile?.private?`${label}：临时文件需要私有空间，请开启目标空间的私有下载。`:!validPath(route.path)?`${label}：对象路径只能包含小写字母、数字、/、_、-，不能以 / 结尾。`:'';
    return [key,message];
  })));
  function validPath(value:string){return /^[a-z0-9][a-z0-9/_-]{0,79}$/.test(value)&&!value.includes('//')&&!value.endsWith('/');}
  function hydrate(cloud:any,settings:any,replace=false,observation?:any){
    snapshot.value={profiles:[],jobs:[],paused:true,...cloud};
    delivery.value={policies:{...settings.policies},routes:structuredClone(settings.qiniu_routes||{}),profiles:(settings.qiniu_profiles||[]).map(({id,name,bucket}:CloudProfile)=>({id,name,bucket})),prefetch_platform:settings.prefetch_platform||'windows',root:observation?.root||'',job:observation?.job||{}};
    if(observation)usage.value={bytes:observation.cached_bytes||0,files:observation.cached_files||0,roomBytes:observation.room_client_cache?.bytes||0,roomLocalBytes:observation.room_client_cache?.local_bytes||0,roomStagingBytes:observation.room_client_cache?.staging_bytes||0,free:observation.disk_free||0,reserved:observation.operations?.reserved_bytes||0};
    if(ready.value&&!replace)return;
    policies.value=Object.fromEntries(Object.entries(settings.policies||policies.value).map(([key,mode])=>[key,mode==='disabled'?'redirect':String(mode)]));
    for(const key of Object.keys(local) as (keyof typeof local)[])if(settings[key]!==undefined)(local as Record<string,unknown>)[key]=settings[key];
    if(settings.room_cache_quota_mib!==undefined)roomCache.settings.room_cache_quota_mib=settings.room_cache_quota_mib;
    if(settings.room_cache_staging_mib!==undefined)roomCache.settings.room_cache_staging_mib=settings.room_cache_staging_mib;
    profiles.value=(settings.qiniu_profiles||[]).map((p:CloudProfile)=>({quota_unit:'GiB',monthly_egress_gib:0,egress_threshold:90,...p}));
    routes.value=Object.fromEntries(cloudKinds.map(([key])=>[key,{...(settings.qiniu_routes?.[key]||{mode:'local',profile:'',path:key})}]));
    paused.value=settings.qiniu_paused!==false;signedMinutes.value=settings.qiniu_signed_url_minutes||240;ready.value=true;saved.value=fingerprint(payload());
  }
  function add(provider='rainyun'){
    let index=1;while(profiles.value.some(p=>p.id===provider+index))index++;
    const p:CloudProfile={id:provider+index,provider,bucket:'',domain:'',prefix:'buggcraft-mirror',upload_host:'https://up-z0.qiniup.com',private:true,quota_gib:10,daily_upload_gib:1,quota_unit:'GiB',monthly_egress_gib:0,egress_threshold:90};
    if(provider!=='qiniu')Object.assign(p,{endpoint:'https://cn-nb1.rains3.com',region:'cn-nb1',addressing_style:'path'});
    if(provider==='rainyun')Object.assign(p,{bucket:'bugg',domain:'https://s3oss.bugg.icu',quota_gib:80,monthly_egress_gib:400,quota_unit:'GB',tls_auto:false});
    profiles.value.push(p);
  }
  function selectStorage(key:string,identifier:string){
    const route=routes.value[key];if(!route)return;
    // Selecting a cloud space means cloud-only by default; an extra local copy is opt-in.
    route.profile=identifier;route.mode=identifier?(route.mode==='local'?'cloud':route.mode):'local';
  }
  function remove(index:number){
    const profile=profiles.value[index];if(!profile)return;
    for(const route of Object.values(routes.value))if(route.profile===profile.id){route.profile='';route.mode='local';}
    profiles.value.splice(index,1);
  }
  function payload(){return {...local,policies:{...policies.value},room_cache_quota_mib:roomCache.settings.room_cache_quota_mib,room_cache_staging_mib:roomCache.settings.room_cache_staging_mib,qiniu_profiles:structuredClone(profiles.value.map(p=>({...p}))),qiniu_routes:Object.fromEntries(Object.entries(routes.value).map(([k,r])=>[k,{...r,profile:r.mode==='local'?'':r.profile}])),qiniu_paused:paused.value,qiniu_signed_url_minutes:signedMinutes.value};}
  function fail(cause:unknown){
    const timed=cause instanceof Error&&['AbortError','TimeoutError'].includes(cause.name);
    error.value=timed?'请求超时，请刷新确认是否已生效，再重试。':cause instanceof Error?cause.message:String(cause);
    notices.notify(error.value,true);
  }
  async function request(body:Record<string,unknown>,timeout=20000){
    const response=await fetch('/v1/admin/mirror',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:AbortSignal.timeout(timeout)});
    let data:any;
    try{data=await response.json();}catch{throw Error('服务未返回有效数据，请稍后刷新重试。');}
    if(!response.ok||data.ok===false)throw Error(response.status===401?'管理会话已过期，请重新登录。':data.message||data.code||'操作未通过，请检查配置。');
    return data;
  }
  async function setUploadsPaused(value:boolean){
    if(uploadChanging.value||saving.value)return;
    uploadChanging.value=true;error.value='';feedback.value='';
    try{
      const data=await request({qiniu_paused:value});
      const baseline=JSON.parse(saved.value||'{}');baseline.qiniu_paused=value;saved.value=fingerprint(baseline);
      paused.value=value;
      snapshot.value={...snapshot.value,...data.cloud_storage,paused:value};
      feedback.value=value?'已暂停新上传，当前文件完成后停止；队列和云端副本保留。':'已启用后台上传，将按容量和每日预算处理队列。';
      notices.notify(feedback.value);
    }catch(cause){fail(cause);}finally{uploadChanging.value=false;}
  }
  async function loadJobs(){
    const revision=++jobRequest;
    jobsLoading.value=true;jobsError.value='';
    try{
      const data=await request({action:'cloud_jobs',...jobFilter});
      if(revision===jobRequest)jobPage.value=data;
    }catch(cause){if(revision===jobRequest)jobsError.value=cause instanceof Error?cause.message:String(cause);}
    finally{if(revision===jobRequest)jobsLoading.value=false;}
  }
  async function save(){
    if(saving.value||busy.value||!ready.value)return;
    attempted.value=true;error.value='';feedback.value='';
    const invalid=Object.values(routeErrors.value).find(Boolean);
    if(invalid){fail(Error(invalid));return;}
    saving.value=true;
    try{
      const body=payload();
      const data=await request(body);
      const unchanged=fingerprint(payload())===fingerprint(body);
      hydrate(data.cloud_storage||snapshot.value,data.settings||body,unchanged,data);
      if(!unchanged)saved.value=fingerprint(body);
      feedback.value=unchanged?'存储设置已保存。':'存储设置已保存，当前仍有尚未保存的修改。';
      notices.notify(feedback.value);
    }catch(cause){fail(cause);}
    finally{saving.value=false;}
  }
  async function action(action:string,values:Record<string,unknown>={}){
    if(busy.value)return;
    busy.value=true;error.value='';feedback.value='';
    try{
      const data=await request({action,...values},action==='cloud_test'?90000:40000);
      if(data.cloud_storage)snapshot.value=data.cloud_storage;
      feedback.value=action==='cloud_test'?'32 字节上传、下载及删除验证通过。':action==='cloud_tls_issue'?'证书任务已提交，进度将在此更新。':action==='cloud_migrate'?(data.queued?`已安排 ${data.queued} 个文件进入队列。${snapshot.value.paused?'上传已暂停，请点击“启用后台上传”。':''}`:'此类别没有可新增的本地文件；已有队列或云端副本不会重复入队。'):'操作已完成。';
      notices.notify(feedback.value);
      if(['cloud_migrate','cloud_retry'].includes(action))void loadJobs();
    }catch(cause){fail(cause);}
    finally{busy.value=false;}
  }
  async function loadCleanup(after=''){
    if(cleanupLoading.value)return;
    cleanupLoading.value=true;
    try{cleanupPage.value=await request({action:'cloud_cleanup_preview',after},35000);cleanupSelected.value=[];cleanupReady.value=true;}
    catch(cause){fail(cause);}finally{cleanupLoading.value=false;}
  }
  async function cleanupLocal(){
    if(cleanupLoading.value||!cleanupSelected.value.length)return;
    cleanupLoading.value=true;
    try{
      const result=await request({action:'cloud_cleanup_local',digests:[...cleanupSelected.value]},35000);
      notices.notify(`已清理 ${result.removed} 个本地副本，释放 ${(result.freed_bytes/1e9).toFixed(2)} GB。${result.skipped?'其余文件正在使用或策略已变化，已跳过。':''}`);
      cleanupSelected.value=[];cleanupPage.value={files:[],next_cursor:null,bytes:0};cleanupReady.value=false;
    }catch(cause){fail(cause);}finally{cleanupLoading.value=false;}
  }
  return {local,usage,profiles,routes,policies,paused,signedMinutes,ready,busy,saving,attempted,dirty,routeErrors,feedback,error,snapshot,delivery,copies,bytes,migrateRoute,migrateLimit,hydrate,add,remove,selectStorage,payload,save,action,uploadChanging,setUploadsPaused,jobFilter,jobPage,jobsLoading,jobsError,loadJobs,cleanupPage,cleanupSelected,cleanupLoading,cleanupReady,loadCleanup,cleanupLocal};
});
