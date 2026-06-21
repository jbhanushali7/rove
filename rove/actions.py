import asyncio
import logging

logger = logging.getLogger(__name__)

# The agent's complete action vocabulary. `params` documents expected keys.
ACTIONS = [
    {"name": "CONTINUE", "write": False, "desc": "Nothing to do; keep crawling.", "params": {}},
    {"name": "DISMISS_OVERLAY", "write": False, "desc": "Click a cookie/consent/modal close control.", "params": {"selector": "css"}},
    {"name": "CLICK", "write": True, "desc": "Click a specific element (e.g. 'continue as guest').", "params": {"selector": "css"}},
    {"name": "FILL_FORM", "write": True, "desc": "Fill non-auth form fields and optionally submit. NEVER for password fields.", "params": {"fields": {"name": "value"}, "submit": "bool"}},
    {"name": "DEPRIORITIZE_PREFIX", "write": False, "desc": "Crawl management: lower priority of a URL path prefix.", "params": {"prefix": "/path"}},
    {"name": "STOP_CRAWL", "write": True, "desc": "Crawl management: stop the crawl.", "params": {}},
    {"name": "ESCALATE_HUMAN", "write": True, "desc": "Hand off to the human. Set human_mode + a question/instruction.", "params": {"question": "str"}},
]
ACTION_NAMES = [a["name"] for a in ACTIONS]
_WRITE = {a["name"] for a in ACTIONS if a["write"]}
# Actions that operate on the live DOM and therefore need a page navigated to the URL.
_NEEDS_PAGE = {"DISMISS_OVERLAY", "CLICK", "FILL_FORM"}
# Top-level (non-field) param keys. An edit to one of these sets the param itself;
# any other edit key on a FILL_FORM updates a form field value.
_RESERVED_KEYS = {"submit", "selector", "prefix", "question"}


def requires_approval(action: str) -> bool:
    return action in _WRITE


def needs_page(action: str) -> bool:
    return action in _NEEDS_PAGE


def _coerce(value: str):
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    return value


def apply_edits(params: dict, edit_str: str) -> dict:
    """Parse 'k=v, k2=v2' edits. Reserved keys (submit/selector/prefix/question) set the
    top-level param; everything else updates a FILL_FORM field value (or a top-level param
    when the action has no 'fields')."""
    out = {**params}
    if "fields" in params:
        out["fields"] = dict(params["fields"])
    for pair in edit_str.split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        k, _, v = pair.partition("=")
        k, v = k.strip(), v.strip()
        if k in _RESERVED_KEYS:
            out[k] = _coerce(v)
        elif "fields" in out:
            out["fields"][k] = v
        else:
            out[k] = v
    return out


def _name_selector(name: str) -> str:
    """Build a safe [name="..."] selector. Escapes backslash and double-quote so an
    LLM-supplied field name cannot break out of the attribute selector."""
    escaped = str(name).replace("\\", "\\\\").replace('"', '\\"')
    return f'[name="{escaped}"]'


async def human_review(decision, autonomy: str, prompt_fn):
    """Gate write/escalate actions. autonomy: 'auto' (no gate) | 'review' (gate writes) | 'manual' (gate all).
    Returns (approved: bool, params: dict). prompt_fn is an async (msg, *, kind)->str,
    e.g. PromptChannel.ask — called here with kind="approval"."""
    gated = (autonomy == "manual") or (autonomy == "review" and requires_approval(decision.action))
    if not gated:
        return True, decision.params

    print(f"\n[AGENT PROPOSES] {decision.action}  params={decision.params}")
    print(f"  reasoning: {decision.reasoning}")
    resp = (await prompt_fn("  [Enter]=approve  e <k=v,..>=edit  s=skip  c=cancel crawl: ", kind="approval")).strip()
    if resp == "":
        return True, decision.params
    if resp.lower().startswith("e"):
        edited = apply_edits(decision.params, resp[1:].strip())
        print(f"  edited params: {edited}")
        return True, edited
    if resp.lower().startswith("s"):
        return False, decision.params
    if resp.lower().startswith("c"):
        return False, {"_cancel": True}
    return True, decision.params


async def execute_action(decision, params, page, *, deprioritize_set, stop_flag, allowed_fields=None):
    """Perform an autonomous (non-escalation) action on `page`. Returns a short result string.
    Escalation (ESCALATE_HUMAN) and crawl-mgmt side effects are handled by the master, not here,
    except DEPRIORITIZE_PREFIX/STOP_CRAWL which we apply directly to shared state.

    `allowed_fields` (when provided) restricts FILL_FORM to field names that actually exist on
    the page, so a hallucinated/crafted name can't target an unintended (e.g. password) field."""
    a = decision.action
    try:
        if a == "CONTINUE":
            return "continue"
        if a == "DISMISS_OVERLAY":
            sel = params.get("selector")
            if isinstance(sel, str) and sel and await page.locator(sel).count():
                await page.locator(sel).first.click(timeout=3000, no_wait_after=True)
                return f"dismissed {sel}"
            return "overlay selector not found"
        if a == "CLICK":
            sel = params.get("selector")
            if not isinstance(sel, str) or not sel:
                return "CLICK missing selector"
            await page.locator(sel).first.click(timeout=5000, no_wait_after=True)
            return f"clicked {sel}"
        if a == "FILL_FORM":
            fields = params.get("fields") or {}
            filled, skipped = [], []
            for name, value in fields.items():
                if allowed_fields is not None and name not in allowed_fields:
                    skipped.append(name)
                    continue
                await page.locator(_name_selector(name)).first.fill(str(value), timeout=5000)
                filled.append(name)
            if params.get("submit"):
                await page.keyboard.press("Enter")
            result = f"filled {filled} submit={bool(params.get('submit'))}"
            if skipped:
                result += f" (skipped unknown fields {skipped})"
            return result
        if a == "DEPRIORITIZE_PREFIX":
            p = params.get("prefix")
            if isinstance(p, str) and p:
                deprioritize_set.add(p)
                return f"deprioritized {p}"
            return "DEPRIORITIZE_PREFIX missing prefix"
        if a == "STOP_CRAWL":
            stop_flag.set()
            return "stop requested"
    except Exception as e:
        logger.warning(f"Action {a} failed: {e}")
        return f"action {a} error: {e}"
    return f"unhandled action {a}"
