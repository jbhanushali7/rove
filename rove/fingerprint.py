import hashlib

SKELETON_JS = """
() => {
  const SIG = 'a,button,input,select,textarea,form,nav,header,footer,main,section,article,table,ul,ol,iframe';
  const parts = [];
  document.querySelectorAll(SIG).forEach(el => {
    const cls = (typeof el.className === 'string' && el.className)
      ? el.className.trim().split(/\\s+/).sort().join('.') : '';
    parts.push(el.tagName + '#' + (el.id || '') + '.' + cls);
  });
  return parts.join('|');
}
"""

SPA_HOOK_JS = """
() => {
  window.__spaNavigations = [];
  const note = t => window.__spaNavigations.push({type: t, url: location.href});
  const wrap = name => {
    const orig = history[name].bind(history);
    history[name] = (...a) => { const r = orig(...a); note(name); return r; };
  };
  wrap('pushState'); wrap('replaceState');
  window.addEventListener('hashchange', () => note('hashchange'));
}
"""


def skeleton_hash(skeleton: str) -> str:
    return hashlib.sha256(skeleton.encode()).hexdigest()


async def page_fingerprint(page) -> str:
    return skeleton_hash(await page.evaluate(SKELETON_JS))
