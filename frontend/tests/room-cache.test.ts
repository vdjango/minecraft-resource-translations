import {afterEach,expect,it} from 'vitest';
import {createApp,nextTick,type App} from 'vue';
import {createPinia,setActivePinia} from 'pinia';
import {createMemoryHistory,createRouter} from 'vue-router';
import RoomClientCacheSettings from '../src/RoomClientCacheSettings.vue';
import {useRoomClientCacheStore} from '../src/stores/roomClientCache';

let app:App|undefined;
afterEach(()=>{app?.unmount();app=undefined;document.body.replaceChildren();});

it('keeps an edited temporary-cache rule while background settings are hydrated',async()=>{
  const pinia=createPinia();setActivePinia(pinia);
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{template:'<div />'}}]});
  await router.push('/admin/mirror/settings/room-cache');await router.isReady();
  const root=document.createElement('div');document.body.append(root);
  app=createApp(RoomClientCacheSettings);app.use(pinia);app.use(router);app.mount(root);
  const store=useRoomClientCacheStore();
  store.hydrate({room_cache_enabled:true,room_cache_ttl_hours:8,room_cache_quota_mib:4096});
  await nextTick();
  const ttl=[...root.querySelectorAll('label')].find(label=>label.textContent?.includes('保留时间'))?.querySelector('input') as HTMLInputElement;
  expect(ttl.max).toBe('32');expect(ttl.value).toBe('8');
  ttl.value='12';ttl.dispatchEvent(new Event('input',{bubbles:true}));await nextTick();
  expect(store.payload().room_cache_ttl_hours).toBe(12);
  store.hydrate({room_cache_ttl_hours:8});
  expect(store.payload().room_cache_ttl_hours).toBe(12);
  expect(root.querySelector('a[href="/admin/mirror/settings/cloud?view=room"]')).not.toBeNull();
  expect(root.textContent).toContain('接入联机节点');
});
