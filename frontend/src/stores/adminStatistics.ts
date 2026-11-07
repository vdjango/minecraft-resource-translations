import {defineStore} from 'pinia';
import {reactive,ref} from 'vue';
import {useMirrorWorkspaceStore} from './mirrorWorkspace';
export const useAdminStatisticsStore=defineStore('admin-statistics',()=>{
  const mirror=useMirrorWorkspaceStore();
  const traffic=ref<any>(null),usage=ref<any>(null),overview=ref<any>(null);
  const busy=reactive({traffic:false,usage:false}),errors=reactive({traffic:'',usage:''});
  const filters=reactive({days:7,period:'day',status:''});
  const revisions={traffic:0,usage:0};
  async function request(path:string){
    const response=await fetch(path,{credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(20000)});
    if(response.status===401){mirror.unauthorized=true;throw Error('管理会话已过期，请重新登录。');}
    const value=await response.json();if(!response.ok)throw Error(value.message||value.code||'统计暂时不可用');return value;
  }
  async function loadTraffic(){const revision=++revisions.traffic;busy.traffic=true;
    try{const value=await request('/v1/admin/traffic?days='+filters.days);if(revision===revisions.traffic){traffic.value=value;errors.traffic='';}}
    catch(error){if(revision===revisions.traffic)errors.traffic=(error as Error).message;}
    finally{if(revision===revisions.traffic)busy.traffic=false;}
  }
  async function loadUsage(append=false){const revision=++revisions.usage;busy.usage=true;
    const params=new URLSearchParams({period:filters.period,status:filters.status});if(append&&usage.value?.next_cursor)params.set('cursor',usage.value.next_cursor);
    try{const [value,summary]=await Promise.all([request('/v1/admin/usage?'+params),request('/v1/admin/overview')]);if(revision===revisions.usage){usage.value={...value,calls:append?[...(usage.value?.calls||[]),...value.calls]:value.calls};overview.value=summary;errors.usage='';}}
    catch(error){if(revision===revisions.usage)errors.usage=(error as Error).message;}
    finally{if(revision===revisions.usage)busy.usage=false;}
  }
  return {traffic,usage,overview,busy,errors,filters,loadTraffic,loadUsage};
});
