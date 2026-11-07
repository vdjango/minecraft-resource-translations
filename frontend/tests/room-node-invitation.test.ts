import {afterEach,expect,it,vi} from 'vitest';
import {createApp,nextTick,type App} from 'vue';
import {createPinia,setActivePinia} from 'pinia';
import {createMemoryHistory,createRouter} from 'vue-router';
import RoomClientCacheSettings from '../src/RoomClientCacheSettings.vue';

let app:App|undefined;
afterEach(()=>{app?.unmount();app=undefined;document.body.replaceChildren();vi.unstubAllGlobals();});
const settle=async()=>{for(let i=0;i<6;i++)await nextTick();};

it('opens the origin invitation from file acceleration, generates and copies it',async()=>{
  const fetch=vi.fn().mockResolvedValue({ok:true,json:async()=>({nodes:[],invitation:'buggcache1.test-ticket'})});
  const copy=vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal('fetch',fetch);Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:copy}});
  const pinia=createPinia();setActivePinia(pinia);
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{template:'<div />'}}]});
  await router.push('/admin/mirror/settings/room-cache');await router.isReady();
  const root=document.createElement('div');document.body.append(root);
  app=createApp(RoomClientCacheSettings);app.use(pinia);app.use(router);app.mount(root);
  const open=[...root.querySelectorAll('button')].find(button=>button.textContent==='接入联机节点')!;
  open.click();await settle();
  const dialog=document.querySelector('[role="dialog"]')!;
  expect(dialog.textContent).toContain('自动生成并保存密钥');
  const input=dialog.querySelector('input')!;input.value='上海';input.dispatchEvent(new Event('input',{bubbles:true}));
  await settle();dialog.querySelector('form')!.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));
  await settle();
  expect(fetch).toHaveBeenCalledOnce();
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({action:'invite',name:'上海'});
  expect(dialog.textContent).toContain('buggcache1.test-ticket');
  expect(dialog.textContent).toContain('资源下载加速 → 接入源站');
  [...dialog.querySelectorAll('button')].find(button=>button.textContent==='复制接入邀请')!.click();await settle();
  expect(copy).toHaveBeenCalledWith('buggcache1.test-ticket');
  (dialog.querySelector('[aria-label="关闭详情"]') as HTMLButtonElement).click();await settle();
  expect(document.querySelector('[role="dialog"]')).toBeNull();
});
