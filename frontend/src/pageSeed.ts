/** Read the server bootstrap data once; Vue owns all page content after mount. */
export function capturePageSeed() {
  let state: Record<string, string> = {};
  try {
    const value = JSON.parse(document.querySelector('[data-page-seed]')?.textContent || '{}');
    if (value && typeof value === 'object' && !Array.isArray(value)) state = value;
  } catch {
    // An absent seed is valid for routes without server data.
  }
  return { state, restore() {} };
}
