import {afterEach, expect, it, vi} from 'vitest';

afterEach(() => {
  document.head.querySelectorAll('script').forEach(element => element.remove());
  vi.unstubAllGlobals();
  vi.resetModules();
});

it('loads detail libraries in parallel once and waits until both are available', async () => {
  const {prepareContentRenderer} = await import('../src/contentRenderer');
  expect(document.head.querySelectorAll('script')).toHaveLength(0);
  const first = prepareContentRenderer();
  expect(prepareContentRenderer()).toBe(first);
  const scripts = [...document.head.querySelectorAll('script')];
  expect(scripts).toHaveLength(2);
  const renderer = {render: (text: string) => '<p>'+text+'</p>'};
  const markdownit = vi.fn(() => renderer);
  vi.stubGlobal('markdownit', markdownit);
  scripts[0].dispatchEvent(new Event('load'));
  await Promise.resolve();
  expect(markdownit).not.toHaveBeenCalled();
  vi.stubGlobal('DOMPurify', {sanitize: vi.fn()});
  scripts[1].dispatchEvent(new Event('load'));
  expect(await first).toBe(renderer);
  expect(markdownit).toHaveBeenCalledWith({html:true,linkify:false});
  expect(await prepareContentRenderer()).toBe(renderer);
  expect(document.head.querySelectorAll('script')).toHaveLength(2);
});

it('allows a retry when a detail library fails', async () => {
  const {prepareContentRenderer} = await import('../src/contentRenderer');
  const pending = prepareContentRenderer();
  const rejected = expect(pending).rejects.toThrow('内容解析组件加载失败');
  document.head.querySelectorAll('script').forEach(element => element.dispatchEvent(new Event('error')));
  await rejected;
  expect(document.head.querySelectorAll('script')).toHaveLength(0);
  const renderer = {render: vi.fn()};
  vi.stubGlobal('markdownit', () => renderer);
  vi.stubGlobal('DOMPurify', {sanitize: vi.fn()});
  expect(await prepareContentRenderer()).toBe(renderer);
});
