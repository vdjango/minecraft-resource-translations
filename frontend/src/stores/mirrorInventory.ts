import {defineStore} from 'pinia';
import {ref} from 'vue';
import {useMirrorWorkspaceStore} from './mirrorWorkspace';

export const inventoryReasons:Record<string,string>={redirect_policy:'仅重定向',platform:'未选择的平台',server_file:'服务端文件',developer_mapping:'开发映射',duplicate_compression:'重复压缩格式'};
export const inventoryLink=(category='',handling='')=>({path:'/admin/mirror/sources',query:{view:'inventory',...(category?{category}:{}),...(handling?{handling}:{})}});
export const useMirrorInventoryStore=defineStore('mirror-inventory',()=>{
  const workspace=useMirrorWorkspaceStore();
  const summary=ref<any>(null),page=ref<any>(null),loading=ref(false),error=ref('');
  let revision=0;
  async function load(filters:Record<string,unknown>={},summaryOnly=false){
    const request=++revision;loading.value=true;
    try{
      const value=await workspace.request({action:'inventory_files',...filters,summary_only:summaryOnly});
      if(request!==revision)return;
      summary.value=value;if(!summaryOnly)page.value=value;error.value='';
    }catch(cause){if(request===revision)error.value=(cause as Error).message;}
    finally{if(request===revision)loading.value=false;}
  }
  return {summary,page,loading,error,load};
});
