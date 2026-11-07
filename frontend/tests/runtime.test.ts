import {afterEach,describe,expect,it,vi} from 'vitest';
import {createScope,mountPage} from '../src/runtime';
import {defineComponent,h,nextTick} from 'vue';

afterEach(()=>{
  window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:false}));
  document.body.replaceChildren();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('page lifecycle',()=>{
  it('mounts visible page content from the server seed while a request stalls',async()=>{
    document.body.innerHTML='<script type="application/json" data-page-seed>{"title":"Server title","body":"<p>Translated content</p>"}</script>';
    const Page=defineComponent({props:['seed'],setup:props=>()=>h('main',{id:'project-page'},[h('h1',props.seed.title),h('div',{id:'project-body',innerHTML:props.seed.body})])});
    const initialize=vi.fn();
    mountPage(initialize,Page);await nextTick();
    expect(document.querySelector('[data-vue-page] main h1')?.textContent).toBe('Server title');
    expect(document.getElementById('project-body')?.textContent).toBe('Translated content');
    expect(document.querySelector('[data-vue-page]')?.hasAttribute('hidden')).toBe(false);
    expect(initialize).toHaveBeenCalledOnce();
  });
  it('cancels timers, global listeners and resource subscriptions when closed',()=>{
    vi.useFakeTimers();
    const scope=createScope(), callback=vi.fn(), cleanup=vi.fn();
    scope.setTimeout(callback,20);scope.setInterval(callback,10);
    scope.listen(window,'example',callback);
    scope.onDispose(cleanup);
    scope.dispose();scope.dispose();
    window.dispatchEvent(new Event('example'));
    vi.advanceTimersByTime(100);
    expect(callback).not.toHaveBeenCalled();expect(cleanup).toHaveBeenCalledOnce();
  });
  it('runs ready callbacks even after DOMContentLoaded has already fired',async()=>{
    const scope=createScope(), callback=vi.fn();
    scope.ready(callback);
    await Promise.resolve();
    expect(callback).toHaveBeenCalledOnce();
    scope.dispose();
  });
  it('aborts a response whose headers arrived but body is still pending',async()=>{
    let signal:AbortSignal|undefined;
    vi.stubGlobal('fetch',vi.fn(async (_input,init)=>{
      signal=init.signal;
      return {json:()=>new Promise((_,reject)=>signal!.addEventListener('abort',()=>reject(signal!.reason),{once:true}))};
    }));
    const scope=createScope();
    const response=await scope.fetch('/slow');
    const body=response.json();
    const rejection=expect(body).rejects.toMatchObject({name:'AbortError'});
    expect(scope.state.pending).toBe(1);
    scope.dispose();
    await rejection;
    expect(signal!.aborted).toBe(true);expect(scope.state.pending).toBe(0);
  });
  it('times out stalled requests and forwards caller cancellation',async()=>{
    vi.useFakeTimers();
    vi.stubGlobal('fetch',vi.fn((_input,init)=>new Promise((_,reject)=>{
      if(init.signal.aborted)reject(init.signal.reason);
      else init.signal.addEventListener('abort',()=>reject(init.signal.reason),{once:true});
    })));
    const scope=createScope();
    const pending=scope.fetch('/never');
    const rejection=expect(pending).rejects.toMatchObject({name:'TimeoutError'});
    await vi.advanceTimersByTimeAsync(25000);
    await rejection;
    expect(scope.state.pending).toBe(0);
    const caller=new AbortController();
    const canceled=scope.fetch('/canceled',{signal:caller.signal});
    const canceledRejection=expect(canceled).rejects.toMatchObject({name:'AbortError'});
    caller.abort();await canceledRejection;
    scope.dispose();
  });
  it('finishes response accounting once and restores exposed compatibility functions',async()=>{
    const previous=()=>1;
    (window as any).legacyFunction=previous;
    vi.stubGlobal('fetch',vi.fn(async()=>({status:200,json:async()=>({ready:true})})));
    const scope=createScope();
    scope.expose('legacyFunction',()=>2);
    const response=await scope.fetch('/ready');
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ready:true});
    expect(scope.state.pending).toBe(0);
    scope.dispose();
    expect((window as any).legacyFunction).toBe(previous);
    delete (window as any).legacyFunction;
  });
  it('respects the longer caller deadline while retaining page cancellation',async()=>{
    vi.useFakeTimers();
    vi.stubGlobal('fetch',vi.fn((_input,init)=>new Promise((_,reject)=>init.signal.addEventListener('abort',()=>reject(init.signal.reason),{once:true}))));
    const scope=createScope(),caller=new AbortController();
    const request=scope.fetch('/long-operation',{signal:caller.signal});
    const rejected=expect(request).rejects.toMatchObject({name:'AbortError'});
    await vi.advanceTimersByTimeAsync(30000);
    expect(scope.state.pending).toBe(1);
    scope.dispose();await rejected;expect(scope.state.pending).toBe(0);
  });
  it('mounts once and preserves BFCache state until the document really closes',async()=>{
    vi.stubGlobal('scrollTo',vi.fn());
    const initialize=vi.fn();
    const Page=defineComponent({render:()=>h('main')});
    mountPage(initialize,Page,'index');mountPage(initialize,Page,'index');
    await vi.waitFor(()=>expect(initialize).toHaveBeenCalledOnce());
    window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}));
    expect(document.querySelector('[data-vue-page]')).not.toBeNull();
    window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:false}));
    expect(document.querySelector('[data-vue-page]')).toBeNull();
  });
});
