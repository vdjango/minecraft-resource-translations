import {defineStore} from 'pinia';
import {ref} from 'vue';
import {useCloudStorageStore} from './cloudStorage';
import {useRoomClientCacheStore} from './roomClientCache';
import {useSourceQualityStore} from './sourceQuality';
import {useMirrorPrefetchStore} from './mirrorPrefetch';
import {useNotificationsStore} from './notifications';

export const mirrorCategories=[['core','游戏核心'],['loaders','加载器'],['mods','模组'],['java','Java 运行时'],['libraries','依赖库'],['assets','原版资源']];
export const bytes=(n:number=0)=>n>=1024**3?(n/1024**3).toFixed(2)+' GiB':n>=1024**2?(n/1024**2).toFixed(1)+' MiB':(n/1024).toFixed(1)+' KiB';
export const stamp=(n:number)=>n?new Date(n*1000).toLocaleString('zh-CN',{hour12:false}):'—';
export const filename=(url:string)=>{try{return decodeURIComponent(url.split('/').pop()||url);}catch{return url;}};
export const useMirrorWorkspaceStore=defineStore('mirror-workspace',()=>{
  const cloud=useCloudStorageStore(),room=useRoomClientCacheStore(),quality=useSourceQualityStore(),prefetch=useMirrorPrefetchStore(),notices=useNotificationsStore();
  const data=ref<any>({}),draft=ref<any>({}),ready=ref(false),loading=ref(false),saving=ref(false),error=ref(''),unauthorized=ref(false),baseline=ref('');
  async function request(body?:any){
    const response=await fetch('/v1/admin/mirror',{method:body?'POST':'GET',credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(20000),headers:{'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})});
    if(response.status===401){unauthorized.value=true;throw Error('管理会话已过期，请重新登录。');}
    const value=await response.json();
    if(!response.ok)throw Error(value.message||value.error||value.code||'暂时无法读取服务');
    return value;
  }
  function hydrate(value:any,replace=false){
    data.value=value;
    if(!ready.value||replace){draft.value=structuredClone(value.settings||{});baseline.value=JSON.stringify(draft.value);}
    cloud.hydrate(value.cloud_storage||{},value.settings||{},replace,value);
    room.hydrate(value.settings||{},replace);
    room.catalog={...room.catalog,...value.room_client_cache?.catalog};room.discovery={...room.discovery,...value.room_client_cache?.discovery};
    quality.hydrate(value,replace);prefetch.hydrate(value.settings?.prefetch_platform,replace);
    ready.value=true;
  }
  async function refresh(){
    if(loading.value)return;
    loading.value=true;
    try{hydrate(await request());error.value='';}
    catch(cause){error.value=cause instanceof Error&&['TimeoutError','AbortError'].includes(cause.name)?'连接超时，稍后自动重试；当前数据保留。':String((cause as Error).message||cause);}
    finally{loading.value=false;}
  }
  async function save(){
    if(saving.value||!ready.value)return;
    const invalid=Object.values(cloud.routeErrors).find(Boolean);
    if(invalid){notices.notify(String(invalid),true);return;}
    saving.value=true;
    try{
      // Effective source mappings are derived server-side and must not be saved.
      const {sources:_,...settings}=draft.value;
      const body={...settings,...cloud.payload(),...room.payload(),...quality.payload(),prefetch_platform:prefetch.payload()};
      const value=await request(body);hydrate(value,true);notices.notify('设置已保存，后台将自动核对并继续处理。');
    }catch(cause){notices.notify((cause as Error).message,true);}
    finally{saving.value=false;}
  }
  return {data,draft,ready,loading,saving,error,unauthorized,baseline,request,hydrate,refresh,save};
});
