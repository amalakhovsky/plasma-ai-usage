#!/usr/bin/env python3
"""Fetch Claude Code + Codex usage quotas and emit one normalized JSON blob.

The KDE plasmoid runs this on a timer (via Plasma's executable data source) and
only draws the result. All credential IO, OAuth refresh (with write-back) and
API calls live here, because QML can neither write files nor juggle OAuth well.

Reads the same local credentials the CLIs already use:
  Claude: ~/.claude/.credentials.json   (claudeAiOauth.{accessToken,refreshToken,expiresAt,...})
  Codex : ~/.codex/auth.json            (tokens.{access_token,refresh_token,account_id})
  OpenRouter: ~/.config/ai-usage/openrouter.key  (one line, mode 0600; or $OPENROUTER_KEY_FILE)
Nothing leaves the machine except the two provider API calls the CLIs make too.

Refresh policy — the widget is SECONDARY to the crew agents
-----------------------------------------------------------
The usage endpoints rate-limit hard (HTTP 429) and the budget is shared with
the crew's own allowance checks (agent_coding_template/tools/quota.sh). Both
sides therefore share one cache of raw provider responses in
$USAGE_CACHE_DIR (default ~/.cache/ai-usage):

  <provider>.json   last good raw response  {"fetchedAt", "source", "response"}
  <provider>.429    last rate-limit hit      {"at", "source"}

The widget:
  * displays whatever is in the shared cache when it is younger than
    WIDGET_MAX_AGE — zero network calls while the crew keeps it fresh;
  * only fetches itself when the cache is older than that AND nobody (widget
    or crew) has been 429'd within the current hold (HOLD_MIN, doubling per
    consecutive widget 429 up to HOLD_MAX);
  * writes every successful raw response back to the shared cache so the
    crew's next check can reuse it instead of spending a request;
  * on any failure keeps emitting the cached numbers flagged "stale", so the
    panel never goes blank.

--force (popup Refresh button) ignores WIDGET_MAX_AGE but still honours the
429 hold and a 60 s floor. --pretty indents the output.

Stdlib only, so it runs anywhere python3 does.
"""
import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

# ---- Claude constants (from ClaudeCodeUsage/src/lib/oauth.js) ----
CL_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CL_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CL_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CL_BETA = "oauth-2025-04-20"
CL_API_VERSION = "2023-06-01"

# ---- Codex constants (client_id from the auth.json JWT `client_id` claim) ----
CX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

# ---- OpenRouter: plain API key, two cheap GETs, no OAuth ----
OR_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
OR_KEY_URL = "https://openrouter.ai/api/v1/auth/key"
OR_KEY_FILE = os.environ.get("OPENROUTER_KEY_FILE") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"),
    "ai-usage", "openrouter.key")
# Balance floors in USD (--or-floors WARN,CRIT overrides): the ring turns amber/red
# when the prepaid balance drops this low, whatever the monthly cap says.
OR_FLOORS = [10.0, 3.0]
OR_NOTIFY = True                 # --no-notify disables the desktop notification on crossing a floor

REFRESH_SKEW = 5 * 60            # refresh when the token expires within 5 min
HTTP_TIMEOUT = 15
_SSL = ssl.create_default_context()

# ---- refresh policy ----
CACHE_DIR = os.environ.get("USAGE_CACHE_DIR") or os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache"),
    "ai-usage")
WIDGET_MAX_AGE = 10 * 60         # reuse shared cache younger than this (s)
FORCE_FLOOR = 60                 # even --force won't refetch a cache younger than this
HOLD_MIN = 15 * 60               # stay off the API this long after any 429
HOLD_MAX = 60 * 60               # cap for consecutive widget 429s
WIDGET_STATE = os.path.join(CACHE_DIR, "widget-state.json")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _home_file(*parts):
    return os.path.join(os.path.expanduser("~"), *parts)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _write_json_atomic(path, data):
    """Owner-only (0600) atomic write, so a token refresh never leaves a
    half-written creds file and never widens the file's permissions."""
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _jwt_exp(token):
    """Unix expiry (seconds) from a JWT `exp` claim, or None."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:
        return None


def _request(method, url, headers, body=None):
    """Returns (status, parsed_json_or_text). Never raises for HTTP errors."""
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers = {**headers, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_SSL) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as e:  # network/DNS/timeout
        return 0, str(e)


def _fmt_age(secs):
    secs = max(0, int(secs))
    if secs < 90:
        return f"{secs}s"
    if secs < 90 * 60:
        return f"{(secs + 30) // 60}m"
    return f"{(secs + 1800) // 3600}h"


def _severity_from_percent(pct):
    if pct >= 90:
        return "crit"
    if pct >= 75:
        return "warn"
    return "ok"


def _norm_severity(api_sev, pct):
    """Normalize an API severity string to ok|warn|crit, falling back to pct."""
    if api_sev:
        s = api_sev.lower()
        if s in ("critical", "exceeded", "blocked", "error"):
            return "crit"
        if s in ("warning", "warn", "elevated"):
            return "warn"
        if s in ("normal", "ok", "low"):
            return "ok"
    return _severity_from_percent(pct or 0)


def _humanize_seconds(secs):
    if secs is None:
        return None
    if secs % 604800 == 0 and secs >= 604800:
        n = secs // 604800
        return "Weekly" if n == 1 else f"{n}-week"
    if secs % 86400 == 0 and secs >= 86400:
        n = secs // 86400
        return "Daily" if n == 1 else f"{n}-day"
    if secs % 3600 == 0 and secs >= 3600:
        return f"{secs // 3600}-hour"
    return f"{secs // 60}-min"


def _iso_from_unix(ts):
    if not ts:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(int(ts)))
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #
def _claude_tier(oauth):
    sub = (oauth.get("subscriptionType") or "").lower()
    base = "MAX" if sub == "max" else "PRO" if sub == "pro" else (sub.upper() or "CLAUDE")
    tier = oauth.get("rateLimitTier") or ""
    for tok in tier.replace("-", "_").split("_"):
        if tok.endswith("x") and tok[:-1].isdigit():
            return f"{base} {tok}"
    return base


def _claude_refresh(root):
    oauth = root["claudeAiOauth"]
    status, data = _request("POST", CL_TOKEN_URL, {}, {
        "grant_type": "refresh_token",
        "refresh_token": oauth["refreshToken"],
        "client_id": CL_CLIENT_ID,
    })
    if status < 200 or status >= 300 or not isinstance(data, dict) or not data.get("access_token"):
        raise RuntimeError("session expired; sign in to Claude Code again")
    oauth["accessToken"] = data["access_token"]
    if data.get("refresh_token"):
        oauth["refreshToken"] = data["refresh_token"]
    if data.get("expires_in"):
        oauth["expiresAt"] = int((time.time() + data["expires_in"]) * 1000)
    _write_json_atomic(_home_file(".claude", ".credentials.json"), root)
    return oauth["accessToken"]


def _claude_token(root):
    oauth = root["claudeAiOauth"]
    exp_ms = int(oauth.get("expiresAt") or 0)
    if exp_ms and exp_ms / 1000 - time.time() > REFRESH_SKEW:
        return oauth["accessToken"]
    return _claude_refresh(root)


def _claude_windows(usage):
    """Prefer the self-describing limits[] array; fall back to legacy keys."""
    out = []
    limits = usage.get("limits")
    if isinstance(limits, list) and limits:
        for lim in limits:
            kind = lim.get("kind") or ""
            group = lim.get("group") or ""
            pct = float(lim.get("percent") or 0)
            scope = (lim.get("scope") or {}).get("model") or {}
            model = scope.get("display_name")
            if kind == "session":
                label = "5-hour"
            elif kind == "weekly_all":
                label = "Weekly"
            elif kind == "weekly_scoped":
                label = f"Weekly · {model}" if model else "Weekly (scoped)"
            else:
                label = (model or kind or group or "Limit").replace("_", " ").title()
            out.append({
                "id": kind or label,
                "label": label,
                "group": group or ("session" if kind == "session" else "weekly"),
                "percent": round(pct, 1),
                "severity": _norm_severity(lim.get("severity"), pct),
                "resetsAt": lim.get("resets_at"),
                "active": bool(lim.get("is_active")),
                # session is the shortest window; weekly is long.
                "sortSeconds": 5 * 3600 if (kind == "session" or group == "session") else 7 * 86400,
            })
        return out

    # legacy fallback
    for key, secs, label in (("five_hour", 5 * 3600, "5-hour"),
                             ("seven_day", 7 * 86400, "Weekly")):
        win = usage.get(key)
        if not win:
            continue
        pct = float(win.get("utilization") or 0)
        out.append({
            "id": key, "label": label,
            "group": "session" if key == "five_hour" else "weekly",
            "percent": round(pct, 1), "severity": _severity_from_percent(pct),
            "resetsAt": win.get("resets_at"), "active": True, "sortSeconds": secs,
        })
    return out


def _claude_spend(usage):
    """Extra-usage (paid credits) meter.

    When the monthly spend limit is hit the API flips `enabled`/`is_enabled` to
    false (disabled_reason "org_level_disabled_until", spend_limit_reached
    true) — i.e. exactly when you most want to see the bar. So we show spend
    whenever a limit exists and credits were ever in play, and report the
    limit-reached state instead of hiding it."""
    xu = usage.get("extra_usage") if isinstance(usage.get("extra_usage"), dict) else {}
    sp = usage.get("spend") if isinstance(usage.get("spend"), dict) else {}
    if not sp and not xu:
        return None

    def money(m):
        if not isinstance(m, dict) or "amount_minor" not in m:
            return None
        return f"{m['amount_minor'] / (10 ** (m.get('exponent') or 2)):.2f}"

    enabled = bool(sp.get("enabled") or xu.get("is_enabled"))
    reached = bool(xu.get("spend_limit_reached")) or \
        (sp.get("disabled_reason") or xu.get("disabled_reason") or "").startswith("org_level_disabled")
    ever = bool(xu.get("credits_ever_enabled")) or (xu.get("used_credits") or 0) > 0 \
        or (sp.get("used") or {}).get("amount_minor", 0) > 0
    if not (enabled or reached or ever):
        return None  # extra usage genuinely off: nothing to meter

    if sp:
        pct = float(sp.get("percent") or 0)
        used, limit = money(sp.get("used")), money(sp.get("limit"))
        currency = ((sp.get("used") or {}).get("currency") or (sp.get("limit") or {}).get("currency")
                    or xu.get("currency") or "")
        sev = _norm_severity(sp.get("severity"), pct)
    else:
        pct = float(xu.get("utilization") or 0)
        dp = xu.get("decimal_places", 2)
        used = f"{(xu.get('used_credits') or 0) / (10 ** dp):.2f}"
        limit = f"{(xu.get('monthly_limit') or 0) / (10 ** dp):.2f}"
        currency = xu.get("currency") or ""
        sev = _severity_from_percent(pct)
    if reached:
        sev, pct = "crit", max(pct, 100.0)
    return {
        "percent": round(pct, 1), "severity": sev,
        "used": used, "limit": limit, "currency": currency,
        "enabled": enabled, "limitReached": reached,
        "status": "limit reached" if reached else ("" if enabled else "off"),
    }


def _claude_creds():
    root = _read_json(_home_file(".claude", ".credentials.json"))
    if not root or not root.get("claudeAiOauth", {}).get("accessToken"):
        return None
    return root


def claude_fetch_raw(root):
    """(status, raw_json_or_text) from the live usage endpoint."""
    token = _claude_token(root)
    return _request("GET", CL_USAGE_URL, {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": CL_BETA,
        "anthropic-version": CL_API_VERSION,
        "Accept": "application/json",
    })


def claude_parse(root, usage):
    return {"tier": _claude_tier(root["claudeAiOauth"]),
            "windows": _claude_windows(usage),
            "spend": _claude_spend(usage)}


# --------------------------------------------------------------------------- #
# Codex
# --------------------------------------------------------------------------- #
def _codex_path():
    home = os.environ.get("CODEX_HOME") or _home_file(".codex")
    return os.path.join(home, "auth.json")


def _codex_refresh(auth, path):
    tokens = auth["tokens"]
    status, data = _request("POST", CX_TOKEN_URL, {}, {
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": CX_CLIENT_ID,
        "scope": "openid profile email offline_access",
    })
    if status < 200 or status >= 300 or not isinstance(data, dict) or not data.get("access_token"):
        raise RuntimeError("session expired; run `codex` to sign in again")
    tokens["access_token"] = data["access_token"]
    if data.get("refresh_token"):
        tokens["refresh_token"] = data["refresh_token"]
    if data.get("id_token"):
        tokens["id_token"] = data["id_token"]
    auth["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    _write_json_atomic(path, auth)
    return tokens["access_token"]


def _codex_token(auth, path):
    tok = auth["tokens"]["access_token"]
    exp = _jwt_exp(tok)
    if exp and exp - time.time() > REFRESH_SKEW:
        return tok
    return _codex_refresh(auth, path)


def _codex_plan(auth):
    try:
        idt = auth["tokens"].get("id_token", "")
        payload = idt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims.get("https://api.openai.com/auth", {}).get("chatgpt_plan_type")
    except Exception:
        return None


def _codex_windows(rate_limit):
    out = []
    for key in ("primary_window", "secondary_window"):
        win = rate_limit.get(key)
        if not win:
            continue
        pct = float(win.get("used_percent") or 0)
        secs = win.get("limit_window_seconds")
        out.append({
            "id": key,
            "label": _humanize_seconds(secs) or ("Primary" if key == "primary_window" else "Secondary"),
            "group": "session" if (secs and secs <= 6 * 3600) else "weekly",
            "percent": round(pct, 1),
            "severity": _severity_from_percent(pct),
            "resetsAt": _iso_from_unix(win.get("reset_at")),
            "active": True,
            "sortSeconds": secs or 10 ** 9,
        })
    return out


def _codex_creds():
    path = _codex_path()
    auth = _read_json(path)
    if not auth or not (auth.get("tokens") or {}).get("access_token"):
        return None
    return (auth, path)


def codex_fetch_raw(creds):
    auth, path = creds
    token = _codex_token(auth, path)
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "codex-cli"}
    acct = auth["tokens"].get("account_id")
    if acct:
        headers["ChatGPT-Account-Id"] = acct
    return _request("GET", CX_USAGE_URL, headers)


def codex_parse(creds, data):
    auth, _ = creds
    if not data.get("rate_limit"):
        raise RuntimeError("no data")
    return {"plan": _codex_plan(auth) or data.get("plan_type"),
            "windows": _codex_windows(data["rate_limit"]),
            "spend": None}


# --------------------------------------------------------------------------- #
# OpenRouter
# --------------------------------------------------------------------------- #
def _openrouter_creds():
    """The API key, from the key file. Returns None (provider hidden) when absent."""
    try:
        with open(OR_KEY_FILE, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
    except Exception:
        return None
    return key or None


def openrouter_fetch_raw(key):
    """Account balance + this key's own limit, bundled as one cacheable response."""
    headers = {"Authorization": f"Bearer {key}"}
    status, credits = _request("GET", OR_CREDITS_URL, headers)
    if status != 200 or not isinstance(credits, dict):
        return status, credits
    kstatus, keyinfo = _request("GET", OR_KEY_URL, headers)
    bundle = {"credits": credits.get("data") if isinstance(credits.get("data"), dict) else credits,
              "key": None, "keyStatus": kstatus}
    if kstatus == 200 and isinstance(keyinfo, dict):
        bundle["key"] = keyinfo.get("data") if isinstance(keyinfo.get("data"), dict) else keyinfo
    return 200, bundle


def _month_rollover_iso(now=None):
    """First moment of next month, UTC: when OpenRouter's monthly key limits reset."""
    t = time.gmtime(now or time.time())
    y, m = (t.tm_year + 1, 1) if t.tm_mon == 12 else (t.tm_year, t.tm_mon + 1)
    return f"{y:04d}-{m:02d}-01T00:00:00+00:00"


def _balance_severity(balance):
    warn, crit = OR_FLOORS[0], OR_FLOORS[1]
    if balance <= crit:
        return "crit"
    if balance <= warn:
        return "warn"
    return "ok"


def _worse(a, b):
    rank = {"ok": 0, "warn": 1, "crit": 2}
    return a if rank.get(a, 0) >= rank.get(b, 0) else b


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def openrouter_parse(key, data):
    c = data.get("credits") if isinstance(data.get("credits"), dict) else None
    if c is None or _num(c.get("total_credits")) is None:
        raise RuntimeError("no data")
    total = _num(c.get("total_credits")) or 0.0
    used = _num(c.get("total_usage")) or 0.0
    balance = round(total - used, 2)
    bal_sev = _balance_severity(balance)

    windows = []
    k = data.get("key") if isinstance(data.get("key"), dict) else {}
    limit = _num(k.get("limit"))
    if limit:
        remaining = _num(k.get("limit_remaining"))
        if remaining is not None:
            cap_used = limit - remaining
        else:
            cap_used = _num(k.get("usage_monthly"))
            if cap_used is None:
                cap_used = _num(k.get("usage")) or 0.0
        cap_used = max(0.0, cap_used)
        pct = 100.0 * cap_used / limit
        reset = (k.get("limit_reset") or "").lower()
        label_period = {"daily": "Daily", "weekly": "Weekly"}.get(reset, "Monthly")
        # Newer responses carry the exact reset instant under effective_budget.
        eb = k.get("effective_budget") or {}
        resets_at = eb.get("resets_at") if isinstance(eb, dict) else None
        if not resets_at:
            resets_at = _month_rollover_iso() if reset in ("", "monthly") else None
        windows.append({
            "id": "keycap",
            "label": f"{label_period} key cap  ${cap_used:.2f} / ${limit:.0f}",
            "group": "monthly",
            "percent": round(min(pct, 100.0), 1),
            # The ring follows this window, so fold the balance floors in here too.
            "severity": _worse(_severity_from_percent(pct), bal_sev),
            "resetsAt": resets_at,
            "active": True,
            "sortSeconds": 30 * 86400,
        })
    else:
        # No key cap: the ring shows how much of everything ever bought is gone.
        pct = 100.0 * used / total if total > 0 else 100.0
        windows.append({
            "id": "depletion", "label": f"Credits used  ${used:.2f} / ${total:.2f}",
            "group": "lifetime", "percent": round(min(pct, 100.0), 1),
            "severity": _worse(_severity_from_percent(pct), bal_sev),
            "resetsAt": None, "active": True, "sortSeconds": 365 * 86400,
        })

    spend = {
        "percent": round(min(100.0 * used / total, 100.0), 1) if total > 0 else 100.0,
        "severity": bal_sev,
        "used": f"{used:.2f}", "limit": f"{total:.2f}", "currency": "$",
        "enabled": True, "limitReached": balance <= 0,
        "status": "empty" if balance <= 0 else f"${balance:.2f} left",
    }
    return {"plan": "OpenRouter", "tier": f"${balance:.2f} left",
            "windows": windows, "spend": spend,
            "balance": balance, "balanceSeverity": bal_sev,
            "floors": {"warn": OR_FLOORS[0], "crit": OR_FLOORS[1]}}


def _openrouter_notify(result, wstate):
    """One desktop notification per floor crossing (worsening only), via notify-send."""
    if not OR_NOTIFY or not result.get("available") or result.get("balance") is None:
        return
    sev = result.get("balanceSeverity") or "ok"
    prev = wstate.get("openrouterSeverity") or "ok"
    wstate["openrouterSeverity"] = sev
    rank = {"ok": 0, "warn": 1, "crit": 2}
    if rank.get(sev, 0) <= rank.get(prev, 0):
        return
    bal = result["balance"]
    title = "OpenRouter credits almost gone" if sev == "crit" else "OpenRouter credits running low"
    body = f"${bal:.2f} left on the account."
    try:
        import subprocess
        subprocess.Popen(["notify-send", "-a", "AI Usage", "-i", "speedometer",
                          "-u", "critical" if sev == "crit" else "normal", title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# shared cache + yield policy
# --------------------------------------------------------------------------- #
PROVIDERS = {
    "claude": (_claude_creds, claude_fetch_raw, claude_parse,
               "Session expired — sign in via Claude Code"),
    "codex": (_codex_creds, codex_fetch_raw, codex_parse,
              "Session expired — run `codex`"),
    "openrouter": (_openrouter_creds, openrouter_fetch_raw, openrouter_parse,
                   f"Key rejected — check {OR_KEY_FILE}"),
}


def _cache_paths(name):
    return (os.path.join(CACHE_DIR, f"{name}.json"),
            os.path.join(CACHE_DIR, f"{name}.429"))


def _write_cache(path, data):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        _write_json_atomic(path, data)
    except Exception:
        pass  # cache is best-effort


def _hold_until(hit, wstate):
    """When the API may next be touched, given the latest 429 (from anyone)."""
    if not isinstance(hit, dict) or not hit.get("at"):
        return 0
    hold = HOLD_MIN
    if hit.get("source") == "widget":
        hold = min(HOLD_MAX, int(wstate.get("hold") or HOLD_MIN))
    return float(hit["at"]) + hold


def provider(name, force, wstate, now):
    creds_fn, fetch_raw, parse, expired_msg = PROVIDERS[name]
    creds = creds_fn()
    if creds is None:
        return {"available": False}

    cache_path, hit_path = _cache_paths(name)
    cache = _read_json(cache_path)
    hit = _read_json(hit_path)
    cached_at = float(cache.get("fetchedAt") or 0) if isinstance(cache, dict) else 0
    age = now - cached_at if cached_at else None

    def from_cache(error, stale):
        out = {"available": True, "error": error, "stale": stale,
               "fetchedAt": int(cached_at), "source": cache.get("source") or "?",
               "windows": [], "spend": None, "tier": None, "plan": None}
        try:
            out.update(parse(creds, cache["response"]))
        except Exception as e:
            out["error"] = error or f"bad cached data: {e}"
        return out

    def empty(error):
        return {"available": True, "error": error, "stale": True, "fetchedAt": 0,
                "source": None, "windows": [], "spend": None, "tier": None, "plan": None}

    # 1. Fresh enough: display it, no network.
    max_age = FORCE_FLOOR if force else WIDGET_MAX_AGE
    if age is not None and age < max_age:
        return from_cache(None, False)

    # 2. Somebody was rate-limited recently: yield, show what we have.
    until = _hold_until(hit, wstate)
    if now < until:
        who = "widget" if hit.get("source") == "widget" else "crew"
        msg = f"yielding · {who} rate-limited {_fmt_age(now - float(hit['at']))} ago"
        return from_cache(msg, True) if age is not None else empty(msg)

    # 3. Fetch ourselves and share the answer.
    try:
        status, data = fetch_raw(creds)
    except Exception as e:
        status, data = -1, str(e)

    if status == 429:
        wstate["hold"] = min(HOLD_MAX, max(HOLD_MIN, int(wstate.get("hold") or 0) * 2))
        _write_cache(hit_path, {"at": now, "source": "widget"})
        msg = f"rate-limited · holding {_fmt_age(wstate['hold'])}"
        return from_cache(msg, True) if age is not None else empty(msg)

    if status == 401:
        error = expired_msg
    elif status == -1:
        error = str(data)
    elif status < 200 or status >= 300 or not isinstance(data, dict):
        error = f"HTTP {status}"
    else:
        try:
            parsed = parse(creds, data)
        except Exception as e:
            parsed, error = None, str(e)
        if parsed is not None:
            wstate["hold"] = 0
            _write_cache(cache_path, {"fetchedAt": int(now), "source": "widget", "response": data})
            out = {"available": True, "error": None, "stale": False,
                   "fetchedAt": int(now), "source": "widget"}
            out.update(parsed)
            return out

    return from_cache(error, True) if age is not None else empty(error)


# --------------------------------------------------------------------------- #
def _parse_args(argv):
    global OR_FLOORS, OR_NOTIFY
    if "--no-notify" in argv:
        OR_NOTIFY = False
    if "--or-floors" in argv:
        try:
            raw = argv[argv.index("--or-floors") + 1]
            warn, crit = (float(x) for x in raw.split(",")[:2])
            OR_FLOORS = [max(warn, crit), min(warn, crit)]
        except Exception:
            pass


def main():
    _parse_args(sys.argv)
    force = "--force" in sys.argv
    now = time.time()
    wstate = _read_json(WIDGET_STATE) or {}
    out = {"generatedAt": int(now)}
    for name in PROVIDERS:
        try:
            out[name] = provider(name, force, wstate, now)
        except Exception as e:  # never let one provider take the widget down
            out[name] = {"available": True, "error": str(e), "stale": True,
                         "windows": [], "spend": None}
    if isinstance(out.get("openrouter"), dict):
        _openrouter_notify(out["openrouter"], wstate)
    _write_cache(WIDGET_STATE, wstate)
    pretty = "--pretty" in sys.argv
    json.dump(out, sys.stdout, indent=2 if pretty else None)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
