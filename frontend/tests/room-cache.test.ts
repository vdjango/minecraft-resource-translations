import {afterEach,expect,it} from 'vitest';
import {readFileSync} from 'node:fs';
import {createApp,type App} from 'vue';
import RoomClientCacheSettings from '../src/RoomClientCacheSettings.vue';
import MirrorOperations from '../src/MirrorOperations.vue';

let app:App,operations:App;
const script=(name:string)=>readFileSync(new URL('../../src/mc_translate/web/'+name,import.meta.url),'utf8');
afterEach(()=>{app?.unmount();operations?.unmount();document.body.replaceChildren();});

it('maps cache settings and refreshes safe status without overwriting unsaved edits',()=>{
  const root=document.createElement('div');document.body.append(root);
  app=createApp(RoomClientCacheSettings);app.mount(root);
  const operationRoot=document.createElement('div');document.body.append(operationRoot);
  operations=createApp(MirrorOperations,{page:'temporary'});operations.mount(operationRoot);
  const source=script('admin-mirror.js').split('function mirrorRules')[0];
  const controller=new Function('$','metrics','number','qualityTime',source+'\nreturn {roomCacheSettings,renderRoomCache};')(
    (id:string)=>document.getElementById(id),()=>{},String,String);
  const settings={room_cache_enabled:true,room_cache_probe_enabled:false,room_cache_ttl_hours:8,
    room_cache_quota_mib:4096,room_cache_max_file_mib:512,room_cache_slow_kibps:100,
    room_cache_probe_hours:6,room_cache_probe_daily_mib:32};
  const data={settings,running:true,room_client_cache:{configured:true,sources:[{host:'<img src=x onerror=alert(1)>',samples:1,speed:200,bad:0,checked:1}]}};
  controller.renderRoomCache(data,true);
  expect(controller.roomCacheSettings()).toEqual(settings);
  const ttl=document.getElementById('room-cache-ttl-hours') as HTMLInputElement;
  expect(ttl.max).toBe('32');ttl.value='12';
  controller.renderRoomCache(data,false);
  expect(controller.roomCacheSettings().room_cache_ttl_hours).toBe(12);
  expect(document.querySelector('#room-cache-sources img')).toBeNull();
  expect(document.querySelector('#room-cache-sources')?.textContent).toContain('<img');
  controller.renderRoomCache({...data,room_client_cache:{configured:false}},false);
  expect(document.getElementById('room-cache-state')?.textContent).toContain('尚未配置节点授权密钥');
});
