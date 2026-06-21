import json
import os
import time
import tempfile
import logging

logger = logging.getLogger(__name__)
SESSION_PATH = "output/session.json"

# JS run on every page (init script + already-open pages) to seed localStorage.
# Origin-scoped: a page only receives the storage captured for its OWN origin, so
# one site's login never leaks tokens into another crawled origin.
_LS_APPLY_JS = """(data) => {
  const items = data[location.origin];
  if (items) for (const it of items) {
    try { localStorage.setItem(it.name, it.value); } catch (e) {}
  }
}"""


def load_session() -> dict | None:
    if not os.path.exists(SESSION_PATH):
        return None
    try:
        with open(SESSION_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load session: {e}")
        return None


def session_has_expired(storage_state: dict) -> bool:
    """True only if every cookie is past expiry. Session-only cookies (expires == -1) count as valid."""
    now = time.time()
    cookies = storage_state.get("cookies", [])
    if not cookies:
        return True
    return all(c.get("expires", -1) != -1 and c.get("expires", 0) < now for c in cookies)


async def inject_session_into_context(context, storage_state: dict) -> None:
    cookies = storage_state.get("cookies", [])
    if cookies:
        await context.add_cookies(cookies)
        logger.info(f"Injected {len(cookies)} cookies into crawl context")

    origins = storage_state.get("origins", [])
    ls_by_origin = {o.get("origin"): o.get("localStorage", []) for o in origins if o.get("origin")}
    if ls_by_origin:
        # Future pages: one origin-scoped init script. (Playwright cannot remove a prior
        # init script, so repeated injections accumulate — bounded by the per-URL escalation
        # cap and made harmless by being idempotent + origin-scoped.)
        await context.add_init_script(f"() => ({_LS_APPLY_JS})({json.dumps(ls_by_origin)})")
        # Already-open pages don't get init scripts retroactively — apply to them now.
        for page in context.pages:
            try:
                await page.evaluate(_LS_APPLY_JS, ls_by_origin)
            except Exception as e:
                logger.debug(f"localStorage apply to open page failed: {e}")
        n = sum(len(v) for v in ls_by_origin.values())
        logger.info(f"Injected {n} localStorage items across {len(ls_by_origin)} origin(s)")


async def save_session(context) -> dict:
    # session.json holds live auth cookies/tokens — write owner-only and atomically so
    # there is no window where it exists world-readable (TOCTOU on a multi-user host).
    directory = os.path.dirname(SESSION_PATH)
    os.makedirs(directory, exist_ok=True)
    _restrict_permissions(directory, 0o700)
    state = await context.storage_state()
    _atomic_write_private(SESSION_PATH, json.dumps(state, indent=2))
    logger.info(f"Session saved to {SESSION_PATH}")
    return state


def _atomic_write_private(path: str, text: str) -> None:
    """Write text to `path` via a temp file created mode 0o600 (mkstemp default), then
    atomically replace — the destination is never momentarily world-readable."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _restrict_permissions(path: str, mode: int) -> None:
    """Best-effort owner-only perms. No POSIX-bit effect on Windows, but harmless;
    correct and important on POSIX hosts."""
    try:
        os.chmod(path, mode)
    except OSError as e:
        logger.warning(f"Could not restrict permissions on {path}: {e}")


from rove.session.base import SessionStore


class FileSessionStore(SessionStore):
    def __init__(self, path: str = None):
        self.path = path or SESSION_PATH

    def load(self) -> dict | None:
        return _load_from(self.path)

    def save_state(self, storage_state: dict) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        _restrict_permissions(os.path.dirname(self.path) or ".", 0o700)
        _atomic_write_private(self.path, json.dumps(storage_state, indent=2))


def _load_from(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)
