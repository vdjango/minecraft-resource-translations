import {afterEach,expect,it,vi} from 'vitest';
import {createPinia,setActivePinia} from 'pinia';
import {createApp,defineComponent,h,nextTick,type App} from 'vue';
import {createMemoryHistory,createRouter} from 'vue-router';
import ResourceRoutingSettings from '../src/ResourceRoutingSettings.vue';
import {useCloudStorageStore} from '../src/stores/cloudStorage';
import {useNotificationsStore} from '../src/stores/notifications';
import CloudStorageTasks from '../src/CloudStorageTasks.vue';
import RoomStorageSettings from '../src/RoomStorageSettings.vue';
import LocalCacheCleanup from '../src/LocalCacheCleanup.vue';

let app:App|undefined;
afterEach(()=>{app?.unmount();app=undefined;document.body.replaceChildren();vi.unstubAllGlobals();});

it('preserves edited cloud routes across polling and defaults Rainyun to safe budgets',()=>{
  setActivePinia(createPinia());const cloud=useCloudStorageStore();
  cloud.hydrate({profiles:[],jobs:[]},{qiniu_profiles:[],qiniu_routes:{},qiniu_paused:true});
  cloud.add('rainyun');
  expect(cloud.profiles[0].quota_gib).toBe(80);
  expect(cloud.profiles[0].monthly_egress_gib).toBe(400);
  expect(cloud.profiles[0].tls_auto).toBe(false);
  cloud.routes.assets={mode:'copy',profile:'rainyun1',path:'game/assets'};
  cloud.hydrate({profiles:[{id:'rainyun1',files:1}],jobs:[]},{qiniu_profiles:[],qiniu_routes:{}});
  expect(cloud.payload().qiniu_routes.assets.path).toBe('game/assets');
  expect(cloud.payload().qiniu_paused).toBe(true);
  cloud.routes.assets.mode='local';
  expect(cloud.payload().qiniu_routes.assets.profile).toBe('');
  expect(cloud.snapshot.profiles[0].files).toBe(1);
});

it('selects separate Rainyun and S3 spaces and saves category choices through the Vue form',async()=>{
  const pinia=createPinia();setActivePinia(pinia);const cloud=useCloudStorageStore();
  cloud.hydrate({profiles:[],jobs:[]},{qiniu_profiles:[],qiniu_routes:{},qiniu_paused:true,quota_gib:20,reserve_gib:10});
  cloud.add('rainyun');cloud.add('rainyun');cloud.add('s3');
  cloud.profiles[0].name='游戏核心';cloud.profiles[1].name='资源与模组';
  cloud.profiles[2].bucket='other';cloud.profiles[2].domain='https://other.example.com';
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{template:'<div />'}}]});
  await router.push('/admin/mirror/settings/cloud');await router.isReady();
  const host=document.createElement('div');document.body.append(host);
  app=createApp(defineComponent({setup:()=>()=>h('form',{onSubmit:(event:Event)=>{event.preventDefault();void cloud.save();}},[h(ResourceRoutingSettings)])}));
  app.use(pinia);app.use(router);app.mount(host);await nextTick();
  async function edit(label:string){
    const row=[...host.querySelectorAll('tr')].find(row=>row.querySelector('strong')?.textContent===label)!;
    row.querySelector<HTMLButtonElement>('button')!.click();await nextTick();
    return document.querySelector('[role="dialog"]')!;
  }
  const drawer=await edit('模组');
  const policy=drawer.querySelectorAll<HTMLSelectElement>('select')[0];
  const redirectStorage=drawer.querySelectorAll<HTMLSelectElement>('select')[1];
  expect(redirectStorage.disabled).toBe(false);
  redirectStorage.value='rainyun2';redirectStorage.dispatchEvent(new Event('change',{bubbles:true}));await nextTick();
  expect(cloud.payload().policies.mods).toBe('redirect');
  expect(cloud.payload().qiniu_routes.mods.profile).toBe('rainyun2');
  policy.value='backup';policy.dispatchEvent(new Event('change',{bubbles:true}));await nextTick();
  document.querySelector<HTMLButtonElement>('[aria-label="关闭详情"]')!.click();await nextTick();
  for(const [label,value] of [['游戏核心','rainyun1'],['模组','rainyun2'],['依赖库','s31']]){
    const drawer=await edit(label);
    const select=drawer.querySelectorAll<HTMLSelectElement>('select')[1];
    select.value=value;select.dispatchEvent(new Event('change',{bubbles:true}));
    await nextTick();document.querySelector<HTMLButtonElement>('[aria-label="关闭详情"]')!.click();await nextTick();
  }
  await nextTick();
  expect(cloud.payload().qiniu_routes.core).toEqual({mode:'cloud',profile:'rainyun1',path:'core'});
  expect(cloud.payload().qiniu_routes.mods.profile).toBe('rainyun2');
  expect(cloud.payload().qiniu_routes.libraries.profile).toBe('s31');
  const fetch=vi.fn(async(_url:string,init:RequestInit)=>({ok:true,json:async()=>({settings:JSON.parse(String(init.body)),cloud_storage:{profiles:[],jobs:[]}})}));
  vi.stubGlobal('fetch',fetch);
  host.querySelector('form')!.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));
  await vi.waitFor(()=>expect(cloud.feedback).toBe('存储设置已保存。'));
  const sent=JSON.parse(String(fetch.mock.calls[0][1].body));
  expect(sent.qiniu_routes.mods.profile).toBe('rainyun2');expect(sent.quota_gib).toBe(20);
  expect(sent).not.toHaveProperty('quality_enabled');expect(sent.policies.mods).toBe('backup');
  expect(cloud.dirty).toBe(false);
  cloud.hydrate({},sent,true);expect(cloud.routes.core.profile).toBe('rainyun1');
});

it('keeps edits and names the category when a storage choice cannot be saved',async()=>{
  setActivePinia(createPinia());const cloud=useCloudStorageStore();
  cloud.hydrate({},{});cloud.add('rainyun');cloud.selectStorage('assets','rainyun1');
  cloud.profiles.splice(0,1);
  const fetch=vi.fn();vi.stubGlobal('fetch',fetch);
  await cloud.save();expect(fetch).not.toHaveBeenCalled();expect(cloud.error).toContain('原版资源');
  cloud.add('rainyun');
  fetch.mockResolvedValue({ok:false,status:400,json:async()=>({code:'invalid_mirror_settings',message:'原版资源：目标空间配置不正确。'})});
  await cloud.save();expect(cloud.error).toContain('原版资源：目标空间配置不正确');
  expect(cloud.routes.assets.profile).toBe('rainyun1');expect(cloud.saving).toBe(false);
});

it('saves zero local retention independently of cloud staging through the capacity form',async()=>{
  const pinia=createPinia();setActivePinia(pinia);const cloud=useCloudStorageStore();
  cloud.hydrate({profiles:[],jobs:[]},{room_cache_quota_mib:10240,room_cache_staging_mib:1024});
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{template:'<div />'}}]});
  await router.push('/admin/mirror/settings/cloud');await router.isReady();
  const host=document.createElement('div');document.body.append(host);
  app=createApp(RoomStorageSettings);app.use(pinia);app.use(router);app.mount(host);await nextTick();
  const local=host.querySelectorAll<HTMLInputElement>('.workspace-form-grid input')[0];
  const staging=host.querySelectorAll<HTMLInputElement>('.workspace-form-grid input')[1];
  local.value='0';local.dispatchEvent(new Event('input',{bubbles:true}));
  staging.value='2';staging.dispatchEvent(new Event('input',{bubbles:true}));await nextTick();
  expect(cloud.payload().room_cache_quota_mib).toBe(0);
  expect(cloud.payload().room_cache_staging_mib).toBe(2048);
  expect(host.textContent).toContain('0 表示不新增本地保留副本');
});

it('switches uploads immediately without saving or losing other edits and shows a toast',async()=>{
  setActivePinia(createPinia());const cloud=useCloudStorageStore();
  cloud.hydrate({paused:true},{qiniu_paused:true,prefetch_platform:['windows','macos']});
  cloud.local.quota_gib=25;
  const request=vi.fn(async(_url:string,init:RequestInit)=>({ok:true,json:async()=>({settings:{qiniu_paused:false},cloud_storage:{paused:false}})}));
  vi.stubGlobal('fetch',request);
  await cloud.setUploadsPaused(false);
  expect(JSON.parse(String(request.mock.calls[0][1].body))).toEqual({qiniu_paused:false});
  expect(cloud.snapshot.paused).toBe(false);expect(cloud.paused).toBe(false);
  expect(cloud.local.quota_gib).toBe(25);expect(cloud.dirty).toBe(true);
  expect(useNotificationsStore().items.at(-1)?.message).toContain('已启用后台上传');
  cloud.local.quota_gib=20;expect(cloud.dirty).toBe(false);
});

it('shows upload rate and filters remote task pages without replacing the page shell',async()=>{
  const pinia=createPinia();setActivePinia(pinia);const cloud=useCloudStorageStore();
  cloud.hydrate({paused:false,worker_state:'uploading',phase:'uploading',active_job:{route:'assets',profile:'rainyun1',size:2000,file_path:'example-file'},active_jobs:[{id:1,route:'assets',profile:'rainyun1',size:2000,file_path:'example-file',progress:500,phase:'uploading'},{id:2,route:'core',profile:'rainyun1',size:1000,file_path:'client.jar',progress:1000,phase:'verifying'}],upload_concurrency:4,progress:500,upload_bytes_per_second:1024**2,categories:{core:{finished:{count:30}}}},{qiniu_paused:false,qiniu_profiles:[],qiniu_routes:{}});
  const request=vi.fn(async()=>({ok:true,json:async()=>({jobs:[],total:35,offset:0,limit:20})}));vi.stubGlobal('fetch',request);
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{template:'<div />'}}]});
  await router.push('/admin/mirror/tasks');await router.isReady();
  const host=document.createElement('div');document.body.append(host);
  app=createApp(CloudStorageTasks);app.use(pinia);app.use(router);app.mount(host);
  await vi.waitFor(()=>expect(host.textContent).toContain('1.0 MiB/s'));
  expect(host.querySelectorAll('.workspace-active-grid article')).toHaveLength(2);
  expect(host.querySelectorAll('.workspace-active-grid progress')[0].getAttribute('value')).toBe('25');
  expect(host.textContent).toContain('2 / 4 路');
  const category=host.querySelector<HTMLSelectElement>('.workspace-toolbar select')!;
  category.value='java';category.dispatchEvent(new Event('change',{bubbles:true}));
  await vi.waitFor(()=>expect(JSON.parse(String((request.mock.calls.at(-1) as unknown as [string,RequestInit])[1].body))).toMatchObject({action:'cloud_jobs',route:'java',offset:0}));
  expect(host.textContent).toContain('正在上传');
});

it('previews cleanup and sends only explicitly selected local files',async()=>{
  const pinia=createPinia();setActivePinia(pinia);const cloud=useCloudStorageStore();
  const request=vi.fn(async(_url:string,init:RequestInit)=>({ok:true,json:async()=>{
    const body=JSON.parse(String(init.body));
    return body.action==='cloud_cleanup_preview'?{files:[{digest:'a'.repeat(64),path:'old.jar',size:10}],bytes:10,next_cursor:null}:{removed:1,freed_bytes:10,skipped:0};
  }}));vi.stubGlobal('fetch',request);
  const host=document.createElement('div');document.body.append(host);
  app=createApp(LocalCacheCleanup);app.use(pinia);app.mount(host);
  host.querySelector<HTMLButtonElement>('button')!.click();
  await vi.waitFor(()=>expect(cloud.cleanupReady).toBe(true));
  expect(cloud.cleanupSelected).toEqual([]);
  expect(cloud.cleanupPage.files).toHaveLength(1);
  await cloud.cleanupLocal();expect(request).toHaveBeenCalledTimes(1);
  host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();await nextTick();
  expect(cloud.cleanupSelected).toEqual(['a'.repeat(64)]);
  const remove=Array.from(host.querySelectorAll<HTMLButtonElement>('button')).find(button=>button.textContent?.includes('清理选中的'))!;
  remove.click();await vi.waitFor(()=>expect(cloud.cleanupReady).toBe(false));
  expect(JSON.parse(String(request.mock.calls[1][1].body))).toEqual({action:'cloud_cleanup_local',digests:['a'.repeat(64)]});
  expect(useNotificationsStore().items.at(-1)?.message).toContain('1');
});
