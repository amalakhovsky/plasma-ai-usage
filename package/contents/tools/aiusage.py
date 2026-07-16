#!/usr/bin/env python3
"""Fetch Claude Code + Codex usage quotas and emit one normalized JSON blob.

The KDE plasmoid runs this on a timer (via Plasma's executable data source) and
only draws the result. All credential IO, OAuth refresh (with write-back) and
API calls live here, because QML can neither write files nor juggle OAuth well.

Reads the same local credentials the CLIs already use:
  Claude: ~/.claude/.credentials.json   (claudeAiOauth.{accessToken,refreshToken,expiresAt,...})
  Codex : ~/.codex/auth.json            (tokens.{access_token,refresh_token,account_id})
Nothing leaves the machine except the two provider API calls the CLIs make too.

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

REFRESH_SKEW = 5 * 60            # refresh when the token expires within 5 min
HTTP_TIMEOUT = 15
_SSL = ssl.create_default_context()


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
    sp = usage.get("spend")
    if isinstance(sp, dict) and sp.get("enabled"):
        pct = float(sp.get("percent") or 0)
        used = sp.get("used") or {}
        limit = sp.get("limit") or {}

        def money(m):
            if not m or "amount_minor" not in m:
                return None
            return f"{m['amount_minor'] / (10 ** (m.get('exponent') or 2)):.2f}"
        return {
            "percent": round(pct, 1),
            "severity": _norm_severity(sp.get("severity"), pct),
            "used": money(used), "limit": money(limit),
            "currency": (used.get("currency") or limit.get("currency") or ""),
        }
    xu = usage.get("extra_usage")  # legacy
    if isinstance(xu, dict) and xu.get("is_enabled"):
        pct = float(xu.get("utilization") or 0)
        return {
            "percent": round(pct, 1), "severity": _severity_from_percent(pct),
            "used": f"{(xu.get('used_credits') or 0) / 100:.2f}",
            "limit": f"{(xu.get('monthly_limit') or 0) / 100:.2f}",
            "currency": xu.get("currency") or "",
        }
    return None


def fetch_claude():
    root = _read_json(_home_file(".claude", ".credentials.json"))
    if not root or not root.get("claudeAiOauth", {}).get("accessToken"):
        return {"available": False}
    result = {"available": True, "error": None, "tier": _claude_tier(root["claudeAiOauth"]),
              "windows": [], "spend": None}
    try:
        token = _claude_token(root)
        status, usage = _request("GET", CL_USAGE_URL, {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": CL_BETA,
            "anthropic-version": CL_API_VERSION,
            "Accept": "application/json",
        })
        if status == 401:
            result["error"] = "Session expired — sign in via Claude Code"
        elif status == 429:
            result["error"] = "rate-limited"
        elif status < 200 or status >= 300 or not isinstance(usage, dict):
            result["error"] = f"HTTP {status}"
        else:
            result["windows"] = _claude_windows(usage)
            result["spend"] = _claude_spend(usage)
    except Exception as e:
        result["error"] = str(e)
    return result


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


def fetch_codex():
    path = _codex_path()
    auth = _read_json(path)
    if not auth or not (auth.get("tokens") or {}).get("access_token"):
        return {"available": False}
    result = {"available": True, "error": None, "plan": _codex_plan(auth), "windows": []}
    try:
        token = _codex_token(auth, path)
        headers = {"Authorization": f"Bearer {token}", "User-Agent": "codex-cli"}
        acct = auth["tokens"].get("account_id")
        if acct:
            headers["ChatGPT-Account-Id"] = acct
        status, data = _request("GET", CX_USAGE_URL, headers)
        if status == 401:
            result["error"] = "Session expired — run `codex`"
        elif status == 429:
            result["error"] = "rate-limited"
        elif status < 200 or status >= 300 or not isinstance(data, dict):
            result["error"] = f"HTTP {status}"
        elif not data.get("rate_limit"):
            result["error"] = "no data"
        else:
            result["plan"] = result["plan"] or data.get("plan_type")
            result["windows"] = _codex_windows(data["rate_limit"])
    except Exception as e:
        result["error"] = str(e)
    return result


# --------------------------------------------------------------------------- #
def main():
    out = {
        "generatedAt": int(time.time()),
        "claude": fetch_claude(),
        "codex": fetch_codex(),
    }
    pretty = "--pretty" in sys.argv
    json.dump(out, sys.stdout, indent=2 if pretty else None)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
