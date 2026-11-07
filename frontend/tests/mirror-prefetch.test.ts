import {expect,it} from 'vitest';
import {createApp,nextTick} from 'vue';
import {createPinia,setActivePinia} from 'pinia';
import PrefetchPlatformSettings from '../src/PrefetchPlatformSettings.vue';
import {useMirrorPrefetchStore} from '../src/stores/mirrorPrefetch';

it('uses Vue multi-selection and preserves edits while status polling refreshes',async()=>{
  const pinia=createPinia();setActivePinia(pinia);
  const store=useMirrorPrefetchStore();store.hydrate('windows');
  const host=document.createElement('div');document.body.append(host);
  const app=createApp(PrefetchPlatformSettings);app.use(pinia);app.mount(host);
  try{
    const toggle=async(value:string)=>{const input=host.querySelector<HTMLInputElement>(`input[value="${value}"]`)!;input.click();await nextTick();};
    await toggle('macos');
    expect(store.payload()).toEqual(['windows','macos']);
    store.hydrate('windows');
    expect(store.payload()).toEqual(['windows','macos']);
    expect(host.textContent).toContain('Windows、macOS');
    store.hydrate(['windows','macos'],true);
    await toggle('windows');await toggle('linux');
    expect(store.payload()).toEqual(['macos','linux']);
    await toggle('macos');await toggle('linux');
    expect(store.payload()).toEqual([]);
    expect(host.textContent).toContain('只更新索引');
    store.hydrate('all',true);
    expect(store.payload()).toEqual(['windows','macos','linux']);
  }finally{app.unmount();host.remove();}
});
