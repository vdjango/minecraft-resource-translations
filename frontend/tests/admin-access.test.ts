import {afterEach,describe,expect,it,vi} from 'vitest';
import {createApp,nextTick,type App} from 'vue';
import AdminAccess from '../src/AdminAccess.vue';

let app:App|undefined;
afterEach(()=>{app?.unmount();document.body.replaceChildren();history.replaceState(null,'','/');vi.unstubAllGlobals();});

describe('administrator session restoration',()=>{
  it('shows the first-use guide and consumes its private fragment only on the setup route',async()=>{
    history.replaceState(null,'','/admin/setup#private-initialization-link');
    const fetch=vi.fn();vi.stubGlobal('fetch',fetch);
    const host=document.createElement('div');document.body.append(host);
    app=createApp(AdminAccess,{visible:true,options:{mode:'setup',session:'/session',login:'/login',setup:'/setup',authorized:vi.fn()}});
    app.mount(host);await nextTick();
    expect(host.querySelector('h1')?.textContent).toBe('设置管理员账户');
    expect(host.querySelector('input[name="password_confirm"]')).not.toBeNull();
    expect((host.querySelector('button[type="submit"]') as HTMLButtonElement).disabled).toBe(false);
    expect(location.hash).toBe('');expect(fetch).not.toHaveBeenCalled();
  });
  it('preserves settings tab fragments without treating them as setup credentials',async()=>{
    history.replaceState(null,'','/admin/settings#distribution');
    vi.stubGlobal('fetch',vi.fn(async()=>({ok:true,json:async()=>({authenticated:true})})));
    const host=document.createElement('div');document.body.append(host);
    app=createApp(AdminAccess,{visible:false,options:{mode:'session',session:'/session',login:'/login',setup:'/setup',authorized:vi.fn()}});
    app.mount(host);await nextTick();expect(location.hash).toBe('#distribution');
  });
  it('loads data without another session request when the HTML was authenticated by the server',async()=>{
    const fetch=vi.fn();vi.stubGlobal('fetch',fetch);
    const authorized=vi.fn(),host=document.createElement('div');document.body.append(host);
    app=createApp(AdminAccess,{visible:false,options:{mode:'session',session:'/session',login:'/login',setup:'/setup',authorized}});
    app.mount(host);await nextTick();
    expect(authorized).toHaveBeenCalledOnce();expect(fetch).not.toHaveBeenCalled();
  });
  it('ignores an old unauthorized session response after an interactive login succeeds',async()=>{
    let finishSession!:(value:unknown)=>void;
    const delayed=new Promise(resolve=>{finishSession=resolve;});
    vi.stubGlobal('fetch',vi.fn((url:string)=>url==='/session'?delayed:Promise.resolve({ok:true,json:async()=>({authenticated:true})})));
    const authorized=vi.fn(),unauthorized=vi.fn();
    const host=document.createElement('div');document.body.append(host);
    app=createApp(AdminAccess,{visible:true,options:{mode:'login',session:'/session',login:'/login',setup:'/setup',authorized,unauthorized}});
    app.mount(host);await nextTick();
    for(const [name,value] of [['username','owner'],['password','chosen-password-2026']]){
      const input=host.querySelector<HTMLInputElement>('input[name="'+name+'"]')!;
      input.value=value;input.dispatchEvent(new Event('input',{bubbles:true}));
    }
    host.querySelector('form')!.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));
    await vi.waitFor(()=>expect(authorized).toHaveBeenCalledOnce());
    const json=vi.fn(async()=>({code:'unauthorized'}));
    finishSession({ok:false,status:401,json});
    await vi.waitFor(()=>expect(json).toHaveBeenCalledOnce());
    for(let i=0;i<8;i++)await Promise.resolve();
    expect(unauthorized).not.toHaveBeenCalled();
    expect(authorized).toHaveBeenCalledOnce();
  });
});

