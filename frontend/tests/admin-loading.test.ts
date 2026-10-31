import {afterEach,expect,it,vi} from 'vitest';
import {readFileSync} from 'node:fs';

const source=(name:string)=>readFileSync(new URL('../../src/mc_translate/web/'+name,import.meta.url),'utf8');
const reply=(data:unknown,status=200)=>({ok:status===200,status,json:async()=>data});
afterEach(()=>{document.body.replaceChildren();vi.unstubAllGlobals();});

it('loads independent overview sections in parallel and preserves successful data on failure',async()=>{
  document.body.innerHTML=['panel','admin-access','logout','message','dashboard','metrics','task-states','budget-notice','traffic-summary','refresh-dashboard'].map(id=>`<div id="${id}"></div>`).join('');
  let finish!:(value:unknown)=>void;
  const overview=new Promise(resolve=>{finish=resolve;});
  const fetch=vi.fn((url:string)=>url.includes('/overview')?overview:Promise.resolve(reply({today:{requests:12,launcher_instances:3,reported_instances:2,estimated_instances:1,active_integrations:1,field_hit_ratio:1,ready_fields:4,fields:4}})));
  vi.stubGlobal('fetch',fetch);
  const controller=new Function('pageViews',source('admin.js')+'\n'+source('admin-traffic.js')+'\nreturn {load};')({});
  const pending=controller.load();
  expect(fetch.mock.calls.map(c=>c[0])).toEqual(['/v1/admin/overview','/v1/admin/traffic?days=1']);
  await vi.waitFor(()=>expect(document.querySelectorAll('#traffic-summary .metric')).toHaveLength(4));
  expect(document.getElementById('metrics')?.getAttribute('aria-busy')).toBe('true');
  finish(reply({code:'unavailable'},503));await pending;
  expect(document.querySelectorAll('#traffic-summary .metric')).toHaveLength(4);
  expect(document.getElementById('message')?.classList.contains('error')).toBe(true);
  expect(document.getElementById('metrics')?.getAttribute('aria-busy')).toBe('false');
  expect((document.getElementById('refresh-dashboard') as HTMLButtonElement).disabled).toBe(false);
});

it('shows saved model and quotas while the provider directory stalls, and permits retry after failure',async()=>{
  const ids=['panel','admin-access','logout','message','settings','base-url','api-key','model','effort','refresh-models','model-state','key-state','daily-tokens','monthly-tokens','daily-calls','completion-tokens','timeout','price-input','price-cached','price-output'];
  document.body.innerHTML=ids.map(id=>id==='model'?'<select id="model"></select>':`<input id="${id}">`).join('');
  let finish!:(value:unknown)=>void;
  const models=new Promise(resolve=>{finish=resolve;});
  vi.stubGlobal('fetch',vi.fn((url:string)=>url.endsWith('/models')?models:Promise.resolve(reply({base_url:'https://provider.test/v1',model:'configured-model',reasoning_effort:'medium',daily_token_limit:1234,model_prices:{},api_key_configured:true}))));
  const controller=new Function('pageViews',source('admin.js')+'\nreturn {load,refreshModels};')({});
  await controller.load();
  expect((document.getElementById('model') as HTMLSelectElement).value).toBe('configured-model');
  expect((document.getElementById('daily-tokens') as HTMLInputElement).value).toBe('1234');
  expect((document.getElementById('refresh-models') as HTMLButtonElement).disabled).toBe(true);
  finish(reply({code:'model_list_unavailable'},502));
  await vi.waitFor(()=>expect((document.getElementById('refresh-models') as HTMLButtonElement).disabled).toBe(false));
  expect(document.getElementById('model-state')?.textContent).toContain('当前保存的模型仍可使用');
  expect((document.getElementById('model') as HTMLSelectElement).value).toBe('configured-model');
});

it('shares in-flight node discovery so a second click waits for the same results',async()=>{
  const Network=new Function(source('network-client.js').split('window.translationNetwork=new TranslationNetwork();')[0]+'\nreturn TranslationNetwork;')();
  const network=Object.create(Network.prototype);
  let finish!:()=>void;
  network.discover=vi.fn(()=>new Promise<void>(resolve=>{finish=resolve;}));
  const first=network.select(),second=network.select();
  expect(second).toBe(first);
  expect(network.refreshing).toBe(true);
  expect(network.discover).toHaveBeenCalledTimes(1);
  finish();await second;
  expect(network.refreshing).toBe(false);
  expect(network.selection).toBe(null);
});

it('does not wait for federation discovery before sending hub writes',async()=>{
  const Network=new Function(source('network-client.js').split('window.translationNetwork=new TranslationNetwork();')[0]+'\nreturn TranslationNetwork;')();
  const network=Object.create(Network.prototype);
  network.hub='https://hub.test'; network.selected='https://hub.test'; network.failedUntil=new Map();
  network.bootstrapped=true;
  const fetch=vi.fn(()=>Promise.resolve(reply({ok:true})));
  vi.stubGlobal('fetch',fetch);
  network.ready=new Promise(()=>{});
  const response=await Promise.race([network.fetch('sessions/anonymous',{method:'POST'}),new Promise((_,reject)=>setTimeout(()=>reject(Error('blocked by discovery')),100))]);
  expect(response.ok).toBe(true);
  expect(fetch).toHaveBeenCalledWith('https://hub.test/v1/sessions/anonymous',expect.objectContaining({method:'POST'}));
});

it('routes writes from a bootstrapped mirror to the hub while discovery stalls',async()=>{
  const meta=document.createElement('meta');meta.name='translation-hub';meta.content='https://hub.test';document.head.append(meta);
  try{
    const Network=new Function(source('network-client.js').split('window.translationNetwork=new TranslationNetwork();')[0]+'\nreturn TranslationNetwork;')();
    const fetch=vi.fn((url:string)=>url.endsWith('/nodes')?new Promise(()=>{}):Promise.resolve(reply({ok:true})));
    vi.stubGlobal('fetch',fetch);
    const network=new Network();
    await network.fetch('translations:ensure',{method:'POST'});
    expect(fetch.mock.calls.map(c=>c[0])).toEqual(['https://hub.test/v1/federation/nodes','https://hub.test/v1/translations:ensure']);
  }finally{meta.remove();}
});
