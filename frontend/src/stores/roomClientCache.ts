import {defineStore} from 'pinia';
import {reactive, ref} from 'vue';

type RoomNode = {issuer:string; name:string; enabled:boolean; state:string;node_id?:string;claimed_at?:number};
type CatalogStats = {files:number; available:number; slow:number; unavailable:number; updated:number|null};
type Discovery = {enabled:boolean;active:number;counts:Record<string,number>;states:Record<string,string>;recent:{path:string;size:number;state:string;checked:number;next_at:number;error:string|null;url:string|null;project:number|null;file_id:number|null}[]};
type Settings = {
  room_cache_enabled:boolean;
  room_cache_public_lookup_enabled:boolean;
  room_cache_probe_enabled:boolean;
  room_cache_quota_mib:number;
  room_cache_staging_mib:number;
  room_cache_ttl_hours:number;
  room_cache_max_file_mib:number;
  room_cache_slow_kibps:number;
  room_cache_probe_hours:number;
  room_cache_probe_daily_mib:number;
};

export const useRoomClientCacheStore=defineStore('room-client-cache',()=>{
  const settings=reactive<Settings>({
    room_cache_enabled:false,room_cache_probe_enabled:false,room_cache_public_lookup_enabled:true,
    room_cache_quota_mib:1024,room_cache_staging_mib:1024,room_cache_ttl_hours:8,
    room_cache_max_file_mib:512,room_cache_slow_kibps:128,
    room_cache_probe_hours:6,room_cache_probe_daily_mib:32,
  });
  const nodes=ref<RoomNode[]>([]), invitation=ref(''), nodeName=ref('');
  const catalog=ref<CatalogStats>({files:0,available:0,slow:0,unavailable:0,updated:null});
  const discovery=ref<Discovery>({enabled:false,active:0,counts:{},states:{},recent:[]});
  const busy=ref(false), feedback=ref(''), error=ref('');
  const ready=ref(false);
  function hydrate(value:Partial<Settings>, replace=false){
    if(ready.value&&!replace)return;
    for(const key of Object.keys(settings) as (keyof Settings)[]){
      const next=value[key];
      if(next!==undefined)(settings as Record<string,unknown>)[key]=next;
    }
    ready.value=true;
  }
  function payload():Settings{return {...settings};}
  async function loadCatalog(){
    const response=await fetch('/v1/admin/mirror',{credentials:'same-origin',signal:AbortSignal.timeout(10000)});
    if(!response.ok)throw Error(response.status===401?'管理会话已过期，请重新登录。':'资源源站未连接，暂时无法读取文件目录。');
    const data=await response.json();
    catalog.value={...catalog.value,...data.room_client_cache?.catalog};
    discovery.value={...discovery.value,...data.room_client_cache?.discovery};
  }
  async function importCatalog(files:unknown[],mode:'merge'|'replace'){
    busy.value=true;error.value='';
    try{
      const response=await fetch('/v1/admin/mirror',{
        method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({action:'room_catalog_import',mode,files}),signal:AbortSignal.timeout(30000),
      });
      const data=await response.json();
      if(!response.ok)throw Error(data.message||data.code||'文件目录导入失败，请检查格式和源站状态。');
      feedback.value='已导入 '+data.imported+' 条文件信息；未上传或下载文件实体。';
      await loadCatalog();
      return data;
    }catch(cause){error.value=cause instanceof Error?cause.message:String(cause);throw cause;}
    finally{busy.value=false;}
  }
  async function request(body?:Record<string,unknown>){
    busy.value=true;error.value='';
    try{
      const response=await fetch('/v1/admin/room-cache/nodes',{
        method:body?'POST':'GET',credentials:'same-origin',
        headers:{'Content-Type':'application/json'},
        ...(body?{body:JSON.stringify(body)}:{}),signal:AbortSignal.timeout(10000),
      });
      const data=await response.json();
      if(!response.ok)throw Error(response.status===401?'管理会话已过期，请重新登录。':data.message||data.code||'节点操作失败，请稍后重试。');
      nodes.value=data.nodes||[];
      return data;
    }catch(cause){error.value=cause instanceof Error?cause.message:String(cause);throw cause;}
    finally{busy.value=false;}
  }
  async function refresh(){await request();feedback.value='';}
  async function invite(){invitation.value='';feedback.value='';const data=await request({action:'invite',name:nodeName.value.trim()});invitation.value=data.invitation||'';feedback.value='请在 10 分钟内将邀请粘贴到联机节点后台 → 节点设置 → 资源下载加速。';}
  async function revoke(issuer:string){await request({action:'revoke',issuer});feedback.value='节点接入已撤销；公开下载不受影响。';}
  async function copy(){try{await navigator.clipboard.writeText(invitation.value);feedback.value='邀请已复制。';}catch{feedback.value='复制失败，请手动选择邀请内容。';}}
  return {settings,nodes,invitation,nodeName,catalog,discovery,busy,feedback,error,ready,hydrate,payload,refresh,invite,revoke,copy,loadCatalog,importCatalog};
});
