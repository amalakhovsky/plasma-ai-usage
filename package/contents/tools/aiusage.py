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

Concurrency, holds and scripting
--------------------------------
This script is the only program that calls the usage endpoints, and may be
run from the widget, a systemd timer and on-demand callers at once.
  * Locking: each provider takes an exclusive flock on <cache>/<provider>.lock
    (non-blocking retries for up to 20 s). After acquiring it the cache is
    re-read and freshness re-evaluated, so queued callers reuse what the lock
    holder just wrote. If the lock can't be had the cache is served stale with
    error "busy".
  * Per-provider 429 backoff: widget-state.json keeps holds{<provider>: secs};
    one provider's success never shortens another's hold. The legacy scalar
    "hold" is ignored.
  * --max-age SECONDS overrides WIDGET_MAX_AGE (600) for this invocation.
  * --status-file PATH atomically writes (0600) {"generatedAt", "providers":
    {name: <provider dict> + "lastAttempt": {at, outcome, error}}} for
    machine consumers. outcome is one of fetched|cache|stale|rate-limited|
    error|busy|unavailable.
  * A 2xx response with zero usage windows is an error, never cached.
  * Windows with a null/missing utilization are skipped, never shown as 0%.
  * --status-file writes are serialized under <PATH>.lock and merged per
    provider (an older fetchedAt never replaces a newer one); exit status is 1
    if the file can't be written. Unreadable/corrupt credentials are an error
    (cached data served stale), not "unavailable".
  * Token-endpoint 429 feeds the provider's 429 hold; only 400/401/403 mean
    "session expired". A token rotated on disk mid-refresh is adopted.
  * OAuth: the existing access token is used until it is within 30 s of expiry
    (or the API answers 401), then the creds file is re-read (a live CLI may
    have rotated it) before refreshing, and only token fields are merged back.

Stdlib only, so it runs anywhere python3 does.
"""
import argparse
import base64
import datetime
import fcntl
import json
import math
import os
import ssl
import sys
import tempfile
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
LOCK_WAIT = 20                   # max seconds to wait for a provider lock


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


def _read_json_strict(path, retries=3):
    """Parsed JSON, or None when the file does not exist. Any other failure
    (unreadable, truncated/corrupt after a few short retries) raises, so a
    broken credentials file is never mistaken for a missing one."""
    for attempt in range(retries):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return None
        except json.JSONDecodeError:
            # a CLI may be mid-write (non-atomic); give it a moment
            if attempt == retries - 1:
                raise
            time.sleep(0.05)


def _write_json_atomic(path, data, keep_mode=False):
    """Atomic write, owner-only (0600) by default so a token refresh never
    leaves a half-written creds file and never widens its permissions. With
    keep_mode an existing target keeps its current mode (a new file is 0600).
    Writes through symlinks (to the real file) with an exclusive random temp."""
    real = os.path.realpath(path)
    mode = 0o600
    if keep_mode:
        try:
            mode = os.stat(real).st_mode & 0o7777
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(prefix=".tmp.", dir=os.path.dirname(real) or ".")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, real)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class RateLimited(RuntimeError):
    """A token endpoint answered 429: feeds the provider's 429 hold."""


class SessionExpired(RuntimeError):
    """The token endpoint rejected the refresh token (400/401/403)."""


def _pct(v):
    """A finite float percentage, or None when missing/null/garbage (never 0)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


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


CL_TOKEN_FIELDS = ("accessToken", "refreshToken", "expiresAt")
CL_CREDS = lambda: _home_file(".claude", ".credentials.json")


def _claude_expired(oauth):
    exp_ms = int(oauth.get("expiresAt") or 0)
    return bool(exp_ms) and exp_ms / 1000 <= time.time()


def _claude_adopt(oauth, disk):
    for k in CL_TOKEN_FIELDS:
        if k in disk:
            oauth[k] = disk[k]


def _claude_refresh(root, force=False):
    """Return (access_token, posted): a usable token, refreshing only if really
    necessary. `posted` is True when the token endpoint was called. With force
    (the server just rejected our token with 401) the token endpoint is hit
    even when the token is not locally expired, unless the file on disk already
    holds a different token, which is adopted instead."""
    path = CL_CREDS()
    oauth = root["claudeAiOauth"]
    held = oauth.get("accessToken")
    held_ref = oauth.get("refreshToken")
    # A live CLI may already have rotated the tokens: the disk copy is the truth.
    disk = ((_read_json(path) or {}).get("claudeAiOauth")) or {}
    if disk.get("accessToken"):
        _claude_adopt(oauth, disk)
        if ((disk["accessToken"] != held or disk.get("refreshToken") != held_ref)
                and not _claude_expired(disk)):
            return oauth["accessToken"], False
    if not force and not _claude_expired(oauth):
        return oauth["accessToken"], False  # only step in when nobody else has
    pre_tok = oauth.get("accessToken")
    pre_ref = oauth.get("refreshToken")
    status, data = _request("POST", CL_TOKEN_URL, {}, {
        "grant_type": "refresh_token",
        "refresh_token": oauth["refreshToken"],
        "client_id": CL_CLIENT_ID,
    })
    if status < 200 or status >= 300 or not isinstance(data, dict) or not data.get("access_token"):
        # The CLI may have rotated the single-use refresh token while we were
        # in flight: adopt what it wrote instead of reporting a false expiry.
        disk = ((_read_json(path) or {}).get("claudeAiOauth")) or {}
        if disk.get("accessToken") and disk["accessToken"] != pre_tok and not _claude_expired(disk):
            _claude_adopt(oauth, disk)
            return oauth["accessToken"], True
        if status == 429:
            raise RateLimited("token endpoint rate-limited (HTTP 429)")
        if status in (400, 401, 403):
            raise SessionExpired("session expired; sign in to Claude Code again")
        raise RuntimeError(f"token refresh failed: HTTP {status}" if status else
                           f"token refresh failed: {data}")
    oauth["accessToken"] = data["access_token"]
    if data.get("refresh_token"):
        oauth["refreshToken"] = data["refresh_token"]
    if data.get("expires_in"):
        oauth["expiresAt"] = int((time.time() + data["expires_in"]) * 1000)
    # Re-read immediately before writing; never fall back to an older snapshot.
    fresh = _read_json_strict(path)
    if not isinstance(fresh, dict) or not isinstance(fresh.get("claudeAiOauth"), dict):
        raise RuntimeError("credentials file unreadable before write; not overwriting")
    cur = fresh["claudeAiOauth"]
    if cur.get("accessToken") != pre_tok or cur.get("refreshToken") != pre_ref:
        # Someone else rotated the tokens during our POST: theirs win, no write.
        _claude_adopt(oauth, cur)
        return oauth["accessToken"], True
    for k in CL_TOKEN_FIELDS:
        if k in oauth:
            cur[k] = oauth[k]
    _write_json_atomic(path, fresh, keep_mode=True)
    return oauth["accessToken"], True


def _claude_token(root):
    oauth = root["claudeAiOauth"]
    if not _claude_expired(oauth):
        return oauth["accessToken"], False
    return _claude_refresh(root)


def _claude_windows(usage):
    """Prefer the self-describing limits[] array; fall back to legacy keys.
    Windows without a usable utilization are skipped, never shown as 0%."""
    out = []
    limits = usage.get("limits")
    if isinstance(limits, list) and limits:
        for lim in limits:
            if not isinstance(lim, dict):
                continue
            pct = _pct(lim.get("percent"))
            if pct is None:
                continue
            kind = lim.get("kind") or ""
            group = lim.get("group") or ""
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
                "model": model or None,
            })
        return out

    # legacy fallback
    for key, secs, label in (("five_hour", 5 * 3600, "5-hour"),
                             ("seven_day", 7 * 86400, "Weekly")):
        win = usage.get(key)
        if not isinstance(win, dict):
            continue
        pct = _pct(win.get("utilization"))
        if pct is None:
            continue
        out.append({
            "id": key, "label": label,
            "group": "session" if key == "five_hour" else "weekly",
            "percent": round(pct, 1), "severity": _severity_from_percent(pct),
            "resetsAt": win.get("resets_at"), "active": True, "sortSeconds": secs,
            "model": None,
        })
    for key in sorted(k for k in usage if isinstance(k, str) and k.startswith("seven_day_")):
        win = usage.get(key)
        model = key[len("seven_day_"):].replace("_", " ").title()
        if not isinstance(win, dict) or not model:
            continue
        pct = _pct(win.get("utilization"))
        if pct is None:
            continue
        out.append({
            "id": "weekly_scoped", "label": f"Weekly · {model}", "group": "weekly",
            "percent": round(pct, 1), "severity": _severity_from_percent(pct),
            "resetsAt": win.get("resets_at"), "active": True, "sortSeconds": 7 * 86400,
            "model": model,
        })
    return out


def _claude_spend_state(usage):
    """Normalized extra-usage state shared by _claude_spend and _claude_extra:
    (xu, sp, enabled, limit_reached, credits_ever_in_play)."""
    xu = usage.get("extra_usage") if isinstance(usage.get("extra_usage"), dict) else {}
    sp = usage.get("spend") if isinstance(usage.get("spend"), dict) else {}
    enabled = bool(sp.get("enabled") or xu.get("is_enabled"))
    reached = bool(xu.get("spend_limit_reached")) or \
        str(sp.get("disabled_reason") or xu.get("disabled_reason") or "").startswith("org_level_disabled")
    used_minor = (sp.get("used") or {}).get("amount_minor", 0) if isinstance(sp.get("used"), dict) else 0
    ever = bool(xu.get("credits_ever_enabled")) or (xu.get("used_credits") or 0) > 0 \
        or (used_minor or 0) > 0
    return xu, sp, enabled, reached, ever


def _claude_spend(usage):
    """Extra-usage (paid credits) meter.

    When the monthly spend limit is hit the API flips `enabled`/`is_enabled` to
    false (disabled_reason "org_level_disabled_until", spend_limit_reached
    true) — i.e. exactly when you most want to see the bar. So we show spend
    whenever a limit exists and credits were ever in play, and report the
    limit-reached state instead of hiding it."""
    xu, sp, enabled, reached, ever = _claude_spend_state(usage)
    if not sp and not xu:
        return None

    def money(m):
        if not isinstance(m, dict) or "amount_minor" not in m:
            return None
        return f"{m['amount_minor'] / (10 ** (m.get('exponent') or 2)):.2f}"

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
    """Creds dict; None when the file is absent or holds no login. A corrupt or
    unreadable file raises (an error, not 'unavailable')."""
    root = _read_json_strict(_home_file(".claude", ".credentials.json"))
    if root is None:
        return None
    if not isinstance(root, dict) or not isinstance(root.get("claudeAiOauth", {}), dict):
        raise RuntimeError("Claude credentials file is malformed")
    if not root.get("claudeAiOauth", {}).get("accessToken"):
        return None
    return root


def claude_fetch_raw(root):
    """(status, raw_json_or_text) from the live usage endpoint."""
    def call(token):
        return _request("GET", CL_USAGE_URL, {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": CL_BETA,
            "anthropic-version": CL_API_VERSION,
            "Accept": "application/json",
        })
    token, posted = _claude_token(root)
    status, data = call(token)
    if status == 401 and not posted:  # refresh once (never twice per call), retry once
        token, _ = _claude_refresh(root, force=True)
        status, data = call(token)
    return status, data


_EXTRA_KEYS = ("is_enabled", "user_disabled", "spend_limit_reached",
               "used_credits", "monthly_limit", "decimal_places")


def _claude_extra(usage):
    xu, sp, enabled, reached, _ = _claude_spend_state(usage)
    out = {k: xu.get(k) for k in _EXTRA_KEYS}
    if not xu and not sp:
        return out
    out["is_enabled"] = enabled
    out["spend_limit_reached"] = reached
    if sp:
        used = sp.get("used") if isinstance(sp.get("used"), dict) else {}
        lim = sp.get("limit") if isinstance(sp.get("limit"), dict) else {}
        if out["used_credits"] is None and "amount_minor" in used:
            out["used_credits"] = used["amount_minor"]
        if out["monthly_limit"] is None and "amount_minor" in lim:
            out["monthly_limit"] = lim["amount_minor"]
        if out["decimal_places"] is None and (used or lim):
            out["decimal_places"] = (used.get("exponent") or lim.get("exponent") or 2)
    return out


def claude_parse(root, usage):
    windows = _claude_windows(usage)
    if not windows:
        raise RuntimeError("no usage windows in response")
    return {"tier": _claude_tier(((root or {}).get("claudeAiOauth")) or {}),
            "windows": windows,
            "spend": _claude_spend(usage),
            "extraUsage": _claude_extra(usage)}


# --------------------------------------------------------------------------- #
# Codex
# --------------------------------------------------------------------------- #
def _codex_path():
    home = os.environ.get("CODEX_HOME") or _home_file(".codex")
    return os.path.join(home, "auth.json")


CX_TOKEN_FIELDS = ("access_token", "refresh_token", "id_token")


def _codex_expired(tok):
    exp = _jwt_exp(tok)
    return bool(exp) and exp <= time.time()


def _codex_adopt(tokens, disk):
    for k in CX_TOKEN_FIELDS + ("account_id",):
        if k in disk:
            tokens[k] = disk[k]


def _codex_refresh(auth, path, force=False):
    """Return (access_token, posted); see _claude_refresh (incl. force)."""
    tokens = auth["tokens"]
    held = tokens.get("access_token")
    held_ref = tokens.get("refresh_token")
    disk = ((_read_json(path) or {}).get("tokens")) or {}
    if disk.get("access_token"):
        _codex_adopt(tokens, disk)
        if ((disk["access_token"] != held or disk.get("refresh_token") != held_ref)
                and not _codex_expired(disk["access_token"])):
            return tokens["access_token"], False
    if not force and not _codex_expired(tokens.get("access_token")):
        return tokens["access_token"], False  # only step in when nobody else has
    pre_tok = tokens.get("access_token")
    pre_ref = tokens.get("refresh_token")
    status, data = _request("POST", CX_TOKEN_URL, {}, {
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": CX_CLIENT_ID,
        "scope": "openid profile email offline_access",
    })
    if status < 200 or status >= 300 or not isinstance(data, dict) or not data.get("access_token"):
        disk = ((_read_json(path) or {}).get("tokens")) or {}
        if (disk.get("access_token") and disk["access_token"] != pre_tok
                and not _codex_expired(disk["access_token"])):
            _codex_adopt(tokens, disk)
            return tokens["access_token"], True
        if status == 429:
            raise RateLimited("token endpoint rate-limited (HTTP 429)")
        if status in (400, 401, 403):
            raise SessionExpired("session expired; run `codex` to sign in again")
        raise RuntimeError(f"token refresh failed: HTTP {status}" if status else
                           f"token refresh failed: {data}")
    tokens["access_token"] = data["access_token"]
    if data.get("refresh_token"):
        tokens["refresh_token"] = data["refresh_token"]
    if data.get("id_token"):
        tokens["id_token"] = data["id_token"]
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    auth["last_refresh"] = stamp
    # Re-read immediately before writing; never fall back to an older snapshot.
    fresh = _read_json_strict(path)
    if not isinstance(fresh, dict) or not isinstance(fresh.get("tokens"), dict):
        raise RuntimeError("credentials file unreadable before write; not overwriting")
    cur = fresh["tokens"]
    if cur.get("access_token") != pre_tok or cur.get("refresh_token") != pre_ref:
        # Someone else rotated the tokens during our POST: theirs win, no write.
        _codex_adopt(tokens, cur)
        return tokens["access_token"], True
    for k in CX_TOKEN_FIELDS:
        if k in tokens:
            cur[k] = tokens[k]
    fresh["last_refresh"] = stamp
    _write_json_atomic(path, fresh, keep_mode=True)
    return tokens["access_token"], True


def _codex_token(auth, path):
    tok = auth["tokens"]["access_token"]
    if not _codex_expired(tok):
        return tok, False
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
        if not isinstance(win, dict):
            continue
        pct = _pct(win.get("used_percent"))
        if pct is None:
            continue
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
    auth = _read_json_strict(path)
    if auth is None:
        return None
    if not isinstance(auth, dict) or not isinstance(auth.get("tokens") or {}, dict):
        raise RuntimeError("Codex auth file is malformed")
    if not (auth.get("tokens") or {}).get("access_token"):
        return None
    return (auth, path)


def codex_fetch_raw(creds):
    auth, path = creds
    def call(token):
        headers = {"Authorization": f"Bearer {token}", "User-Agent": "codex-cli"}
        acct = auth["tokens"].get("account_id")
        if acct:
            headers["ChatGPT-Account-Id"] = acct
        return _request("GET", CX_USAGE_URL, headers)
    token, posted = _codex_token(auth, path)
    status, data = call(token)
    if status == 401 and not posted:  # refresh once (never twice per call), retry once
        token, _ = _codex_refresh(auth, path, force=True)
        status, data = call(token)
    return status, data


def codex_parse(creds, data):
    auth = creds[0] if creds else {"tokens": {}}
    if not isinstance(data, dict) or not data.get("rate_limit"):
        raise RuntimeError("no data")
    windows = _codex_windows(data["rate_limit"])
    if not windows:
        raise RuntimeError("no usage windows in response")
    return {"plan": _codex_plan(auth) or data.get("plan_type"),
            "windows": windows,
            "spend": None}


# --------------------------------------------------------------------------- #
# OpenRouter
# --------------------------------------------------------------------------- #
def _openrouter_creds():
    """The API key, from the key file. Returns None (provider hidden) when absent."""
    try:
        with open(OR_KEY_FILE, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
    except FileNotFoundError:
        return None
    return key or None  # other OSErrors (permissions...) propagate: an error, not 'unavailable'


def openrouter_fetch_raw(key):
    """Account balance + this key's own limit, bundled as one cacheable response."""
    headers = {"Authorization": f"Bearer {key}"}
    status, credits = _request("GET", OR_CREDITS_URL, headers)
    if status != 200 or not isinstance(credits, dict):
        return status, credits
    kstatus, keyinfo = _request("GET", OR_KEY_URL, headers)
    if kstatus == 429 or kstatus == 0 or kstatus >= 500:
        # Degrading to "no key cap" would understate usage: surface the failure.
        return kstatus, keyinfo
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
# Gemini / agy — read-only: agy.json is written by `crew agy-usage` (agy -p /usage);
# this helper never launches agy itself.
# --------------------------------------------------------------------------- #
AGY_FILE = os.path.join(CACHE_DIR, "agy.json")
AGY_STALE_SECS = 30 * 60
_AGY_POOLS = (("gemini", "Gemini", 0), ("claude_gpt", "Claude/GPT", 1))


def agy_from_file(now=None):
    now = time.time() if now is None else now
    doc = _read_json(AGY_FILE)
    if not isinstance(doc, dict) or not isinstance(doc.get("response"), dict):
        return {"available": False, "windows": [], "spend": None}
    fetched = _fetched_at(doc)
    windows = []
    for p in doc["response"].get("pools") or []:
        if not isinstance(p, dict):
            continue
        meta = next((m for m in _AGY_POOLS if m[0] == p.get("pool")), None)
        pct = _pct(p.get("pct_used"))
        if meta is None or pct is None:
            continue
        secs = 18000 if p.get("window") == "5h" else 604800
        reset = p.get("resetsAt")
        try:
            passed = bool(reset) and datetime.datetime.fromisoformat(
                str(reset).replace("Z", "+00:00")).timestamp() <= now
        except ValueError:
            passed = False
        if passed:  # the window rolled over since the last check: it is fresh
            pct, reset = 0.0, None
        windows.append({
            "id": f"{meta[0]}_{p.get('window')}",
            "label": f"{meta[1]} {_humanize_seconds(secs)}",
            "group": "session" if secs <= 6 * 3600 else "weekly",
            "percent": round(pct, 1),
            "severity": _severity_from_percent(pct),
            "resetsAt": reset,
            # an unused pool (agy reports reset 1970) is shown dimmed
            "active": bool(reset) or pct > 0,
            # Gemini sorts ahead of Claude/GPT so the panel ring tracks the Gemini pool
            "sortSeconds": secs + meta[2],
        })
    windows.sort(key=lambda w: (not w["id"].startswith("gemini"), w["sortSeconds"]))
    stale = now - fetched > AGY_STALE_SECS
    credits = doc["response"].get("credits")
    return {"available": True, "fetchedAt": fetched, "source": doc.get("source"),
            "stale": stale,
            "error": (f"last checked {_fmt_age(now - fetched)} ago" if stale else None),
            "windows": windows, "spend": None, "tier": None,
            "plan": (f"{credits:g} credits" if isinstance(credits, (int, float)) and credits > 0 else None)}


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


def _lock_acquire(path, timeout=None):
    """Exclusive flock with a non-blocking retry loop. Returns an fd, -1 when
    locking is impossible (cache dir unwritable: carry on unlocked), or None
    when the lock stayed busy for the whole timeout."""
    timeout = LOCK_WAIT if timeout is None else timeout
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return -1
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.1)


def _lock_release(fd):
    if fd is not None and fd >= 0:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _load_state():
    st = _read_json(WIDGET_STATE)
    st = st if isinstance(st, dict) else {}
    st.pop("hold", None)  # legacy shared scalar: ignored
    if not isinstance(st.get("holds"), dict):
        st["holds"] = {}
    return st


def _persist_hold(name, value):
    """Read-modify-write one provider's hold so concurrent runs don't clobber
    others. Without the lock the write is skipped (the in-memory hold stands)."""
    fd = _lock_acquire(os.path.join(CACHE_DIR, "widget-state.lock"), 5)
    if fd is None or fd < 0:
        sys.stderr.write(f"aiusage: widget-state lock unavailable; hold for {name} not persisted\n")
        return
    try:
        st = _load_state()
        st["holds"][name] = value
        _write_cache(WIDGET_STATE, st)
    finally:
        _lock_release(fd)


def _hold_until(hit, wstate, name):
    """When the API may next be touched, given the latest 429 (from anyone)."""
    if not isinstance(hit, dict) or not hit.get("at"):
        return 0
    hold = HOLD_MIN
    if hit.get("source") == "widget":
        hold = min(HOLD_MAX, int((wstate.get("holds") or {}).get(name) or HOLD_MIN))
    return float(hit["at"]) + hold


def _creds_or_error(creds_fn):
    """(creds, error): error is set when the credentials exist but can't be read."""
    try:
        return creds_fn(), None
    except Exception as e:
        return None, f"credentials unreadable: {e}"


def _refresh_wstate(wstate):
    """Reload shared widget state under the provider lock (a peer may have
    changed holds while we queued); in-memory-only keys are kept."""
    disk = _load_state()
    wstate.setdefault("holds", {})
    if not isinstance(wstate["holds"], dict):
        wstate["holds"] = {}
    wstate["holds"].update(disk["holds"])
    if "openrouterSeverity" in disk:
        wstate["openrouterSeverity"] = disk["openrouterSeverity"]
    wstate.pop("hold", None)


def provider(name, force, wstate, now, attempt=None):
    """Compute one provider's dict. `attempt`, if given, is filled with
    {"at", "outcome", "error"} describing what this run did. `now` is only the
    caller's clock: freshness is judged with a clock read after taking the lock."""
    attempt = attempt if attempt is not None else {}
    creds_fn = PROVIDERS[name][0]
    creds, cerr = _creds_or_error(creds_fn)
    if creds is None and cerr is None:
        attempt.update(at=None, outcome="unavailable", error=None)
        return {"available": False}
    wstate.setdefault("holds", {})
    if not isinstance(wstate["holds"], dict):
        wstate["holds"] = {}
    wstate.pop("hold", None)

    fd = _lock_acquire(os.path.join(CACHE_DIR, f"{name}.lock"))
    try:
        if fd is None:
            return _serve_busy(name, creds, attempt, time.time())
        if fd < 0:  # cannot lock: never fetch unlocked, serve the cache
            return _serve_busy(name, creds, attempt, time.time(),
                               error="lock unavailable", outcome="error")
        _refresh_wstate(wstate)
        return _provider_locked(name, force, wstate, time.time(), attempt)
    finally:
        _lock_release(fd)


def _serve_busy(name, creds, attempt, now, error="busy", outcome="busy"):
    _, _, parse, _ = PROVIDERS[name]
    cache = _read_json(_cache_paths(name)[0])
    attempt.update(at=now, outcome=outcome, error=error)
    if isinstance(cache, dict) and cache.get("response") is not None:
        out = {"available": True, "error": error, "stale": True,
               "fetchedAt": int(cache.get("fetchedAt") or 0), "source": cache.get("source") or "?",
               "windows": [], "spend": None, "tier": None, "plan": None}
        try:
            out.update(parse(creds, cache["response"]))
        except Exception:
            pass
        return out
    return {"available": True, "error": error, "stale": True, "fetchedAt": 0,
            "source": None, "windows": [], "spend": None, "tier": None, "plan": None}


def _provider_locked(name, force, wstate, now, attempt):
    creds_fn, fetch_raw, parse, expired_msg = PROVIDERS[name]
    creds, cerr = _creds_or_error(creds_fn)  # re-read under the lock: a peer may have rotated tokens
    if creds is None and cerr is None:
        attempt.update(at=None, outcome="unavailable", error=None)
        return {"available": False}

    cache_path, hit_path = _cache_paths(name)
    cache = _read_json(cache_path)  # RE-READ after taking the lock
    hit = _read_json(hit_path)
    try:
        cached_at = float(cache.get("fetchedAt") or 0) if isinstance(cache, dict) else 0
    except (TypeError, ValueError):
        cached_at = 0
    age = now - cached_at if cached_at else None
    have_cache = age is not None and isinstance(cache, dict)

    def from_cache(error, stale, outcome, strict=False):
        out = {"available": True, "error": error, "stale": stale,
               "fetchedAt": int(cached_at), "source": cache.get("source") or "?",
               "windows": [], "spend": None, "tier": None, "plan": None}
        try:
            out.update(parse(creds, cache["response"]))
        except Exception as e:
            if strict:
                return None
            out["error"] = error or f"bad cached data: {e}"
            out["stale"] = True
            if not error:
                outcome = "stale"
        attempt.update(at=time.time(), outcome=outcome, error=out["error"])
        return out

    def empty(error, outcome):
        attempt.update(at=time.time(), outcome=outcome, error=error)
        return {"available": True, "error": error, "stale": True, "fetchedAt": 0,
                "source": None, "windows": [], "spend": None, "tier": None, "plan": None}

    def fail(error, outcome):
        return from_cache(error, True, outcome) if have_cache else empty(error, outcome)

    if cerr is not None:
        return fail(cerr, "error")

    # 1. Fresh enough: display it, no network. A cache that doesn't parse
    #    falls through to a fetch.
    max_age = FORCE_FLOOR if force else WIDGET_MAX_AGE
    if have_cache and 0 <= age < max_age:
        out = from_cache(None, False, "cache", strict=True)
        if out is not None:
            return out

    # 2. Somebody was rate-limited recently: yield, show what we have.
    until = _hold_until(hit, wstate, name)
    if now < until:
        who = "widget" if hit.get("source") == "widget" else "crew"
        msg = f"yielding · {who} rate-limited {_fmt_age(now - float(hit['at']))} ago"
        return fail(msg, "rate-limited")

    # 3. Fetch ourselves and share the answer.
    try:
        status, data = fetch_raw(creds)
    except RateLimited as e:
        status, data = 429, str(e)
    except Exception as e:
        status, data = -1, str(e)
    done = time.time()  # stamp everything with the post-fetch clock

    if status == 429:
        hold = min(HOLD_MAX, max(HOLD_MIN, int(wstate["holds"].get(name) or 0) * 2))
        wstate["holds"][name] = hold
        _persist_hold(name, hold)
        _write_cache(hit_path, {"at": done, "source": "widget"})
        return fail(f"rate-limited · holding {_fmt_age(hold)}", "rate-limited")

    if status == 401:
        error = expired_msg
    elif status == -1 or status == 0:
        error = str(data)
    elif status < 200 or status >= 300 or not isinstance(data, dict):
        error = f"HTTP {status}"
    else:
        try:
            parsed = parse(creds, data)
        except Exception as e:
            parsed, error = None, str(e)
        if parsed is not None:
            if wstate["holds"].get(name):
                wstate["holds"][name] = 0
                _persist_hold(name, 0)
            _write_cache(cache_path, {"fetchedAt": int(done), "source": "widget", "response": data})
            out = {"available": True, "error": None, "stale": False,
                   "fetchedAt": int(done), "source": "widget"}
            out.update(parsed)
            attempt.update(at=done, outcome="fetched", error=None)
            return out

    return fail(error, "error")


# --------------------------------------------------------------------------- #
def _max_age_arg(v):
    try:
        f = float(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {v!r}")
    if not math.isfinite(f) or f < 0:
        raise argparse.ArgumentTypeError("must be a finite, non-negative number of seconds")
    return f


def _build_parser():
    ap = argparse.ArgumentParser(prog="aiusage.py", allow_abbrev=False,
                                 description="Claude/Codex/OpenRouter usage as normalized JSON.")
    ap.add_argument("--force", action="store_true",
                    help="ignore --max-age/WIDGET_MAX_AGE (60 s floor and 429 holds still apply)")
    ap.add_argument("--pretty", action="store_true", help="indent the JSON output")
    ap.add_argument("--no-notify", action="store_true", help="no desktop notification on a floor crossing")
    ap.add_argument("--or-floors", metavar="WARN,CRIT", help="OpenRouter balance floors in USD")
    ap.add_argument("--max-age", type=_max_age_arg, metavar="SECONDS",
                    help="reuse cache younger than this (default %d)" % WIDGET_MAX_AGE)
    ap.add_argument("--status-file", metavar="PATH", help="also write the machine-readable status file here")
    return ap


def _parse_args(argv):
    """Sets module options; returns the parsed namespace (argv excludes the program name)."""
    global OR_FLOORS, OR_NOTIFY, WIDGET_MAX_AGE
    args = _build_parser().parse_args(argv)
    if args.no_notify:
        OR_NOTIFY = False
    if args.or_floors is not None:
        try:
            warn, crit = (float(x) for x in args.or_floors.split(",")[:2])
            OR_FLOORS = [max(warn, crit), min(warn, crit)]
        except Exception:
            pass
    if args.max_age is not None:
        WIDGET_MAX_AGE = args.max_age
    return args


def _fetched_at(d):
    try:
        return float(d.get("fetchedAt") or 0) if isinstance(d, dict) else 0.0
    except (TypeError, ValueError):
        return 0.0


STATUS_FUTURE_SKEW = 60  # a previous entry dated further ahead than this is never kept


def _write_status_file(status_file, providers):
    """Serialize writers under <status_file>.lock and merge per provider so an
    older fetchedAt never replaces a newer one already on disk. Raises on failure."""
    real = os.path.realpath(status_file)
    os.makedirs(os.path.dirname(real), exist_ok=True)
    fd = _lock_acquire(real + ".lock", 10)
    if fd is None:
        raise RuntimeError("timed out waiting for the status-file lock")
    if fd < 0:
        raise RuntimeError("cannot lock the status file")
    try:
        existing = _read_json(real)
        old = existing.get("providers") if isinstance(existing, dict) else None
        merged = {}
        now = time.time()
        for name, d in providers.items():
            prev = old.get(name) if isinstance(old, dict) else None
            prev_at = _fetched_at(prev)
            if (isinstance(prev, dict) and d.get("available") is not False
                    and prev_at > _fetched_at(d)
                    and prev_at <= now + STATUS_FUTURE_SKEW):  # future-dated: clock stepped back
                # Keep the newer windows, but say what this run saw.
                keep = dict(prev)
                if "lastAttempt" in d:
                    keep["lastAttempt"] = d["lastAttempt"]
                if d.get("error") or d.get("stale"):
                    keep["error"] = d.get("error") or keep.get("error")
                    keep["stale"] = True
                merged[name] = keep
            else:
                merged[name] = d
        _write_json_atomic(real, {"generatedAt": int(time.time()), "providers": merged})
    finally:
        _lock_release(fd)


def main():
    args = _parse_args(sys.argv[1:])
    status_file = args.status_file
    force = args.force
    now = time.time()
    wstate = _load_state()
    out = {"generatedAt": int(now)}
    attempts = {}
    for name in PROVIDERS:
        attempts[name] = {}
        try:
            out[name] = provider(name, force, wstate, now, attempts[name])
        except Exception as e:  # never let one provider take the widget down
            out[name] = {"available": True, "error": str(e), "stale": True,
                         "windows": [], "spend": None}
            attempts[name].update(at=time.time(), outcome="error", error=str(e))
    try:
        out["agy"] = agy_from_file()
    except Exception as e:
        out["agy"] = {"available": True, "error": str(e), "stale": True,
                      "windows": [], "spend": None}
    out["generatedAt"] = int(time.time())
    if isinstance(out.get("openrouter"), dict):
        prev = wstate.get("openrouterSeverity")
        _openrouter_notify(out["openrouter"], wstate)
        if wstate.get("openrouterSeverity") != prev:
            fd = _lock_acquire(os.path.join(CACHE_DIR, "widget-state.lock"), 5)
            if fd is None or fd < 0:
                sys.stderr.write("aiusage: widget-state lock unavailable; severity not persisted\n")
            else:
                try:
                    st = _load_state()
                    st["openrouterSeverity"] = wstate["openrouterSeverity"]
                    _write_cache(WIDGET_STATE, st)
                finally:
                    _lock_release(fd)
    json.dump(out, sys.stdout, indent=2 if args.pretty else None)
    sys.stdout.write("\n")
    sys.stdout.flush()
    if status_file:
        providers = {}
        for name in PROVIDERS:
            d = dict(out[name])
            a = attempts.get(name) or {}
            d["lastAttempt"] = {"at": int(a["at"]) if a.get("at") else None,
                                "outcome": a.get("outcome") or "error",
                                "error": a.get("error")}
            providers[name] = d
        try:
            _write_status_file(status_file, providers)
        except Exception as e:
            sys.stderr.write(f"aiusage: cannot write status file: {e}\n")
            sys.exit(1)


if __name__ == "__main__":
    main()
