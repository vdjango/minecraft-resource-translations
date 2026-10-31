type Markdown = {render: (text: string) => string};
type Libraries = Window & {
  markdownit?: (options: {html: boolean; linkify: boolean}) => Markdown;
  DOMPurify?: {sanitize: (html: string, options?: unknown) => string};
};

const libraries = window as Libraries;
let pending: Promise<Markdown> | undefined;

function script(url: string, available: () => boolean): Promise<void> {
  if (available()) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const element = document.createElement('script');
    const deadline = window.setTimeout(() => finish(new Error('内容解析组件加载超时，请重试')), 10000);
    const finish = (error?: Error) => {
      window.clearTimeout(deadline);
      element.onload = element.onerror = null;
      if (error) { element.remove(); reject(error); } else resolve();
    };
    element.src = url;
    element.async = true;
    element.onload = () => finish(available() ? undefined : new Error('内容解析组件不可用，请重试'));
    element.onerror = () => finish(new Error('内容解析组件加载失败，请重试'));
    document.head.append(element);
  });
}

/** Download detail-only libraries together, on the first actual detail request. */
export function prepareContentRenderer(): Promise<Markdown> {
  if (!pending) {
    pending = Promise.all([
      script('/assets/markdown-it.min.js?v=391f2aa638e7', () => Boolean(libraries.markdownit)),
      script('/assets/purify.min.js?v=cd37898f9782', () => Boolean(libraries.DOMPurify)),
    ]).then(() => libraries.markdownit!({html: true, linkify: false})).catch(error => {
      pending = undefined;
      throw error;
    });
  }
  return pending;
}
