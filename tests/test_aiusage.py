"""Offline tests for aiusage.py (stdlib only, no network)."""
import base64
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "package", "contents", "tools", "aiusage.py")


def load():
    spec = importlib.util.spec_from_file_location("aiusage_under_test", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def jwt(exp):
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b({'alg': 'none'})}.{b({'exp': exp})}.sig"


CLAUDE_RAW = {
    "limits": [
        {"kind": "session", "group": "session", "percent": 10, "resets_at": "2026-01-01T00:00:00+00:00"},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 20,
         "scope": {"model": {"display_name": "Opus"}}, "resets_at": "2026-01-05T00:00:00+00:00"},
    ],
    "extra_usage": {"is_enabled": True, "used_credits": 150, "monthly_limit": 1000, "decimal_places": 2},
}
CODEX_RAW = {"plan_type": "plus", "rate_limit": {
    "primary_window": {"used_percent": 5, "limit_window_seconds": 18000, "reset_at": 1800000000}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = os.path.join(self.tmp.name, "cache")
        self.m.CACHE_DIR = d
        self.m.WIDGET_STATE = os.path.join(d, "widget-state.json")
        self.m.LOCK_WAIT = 3
        self.calls = {"claude": 0, "codex": 0}
        self.raw = {"claude": (200, CLAUDE_RAW), "codex": (200, CODEX_RAW)}
        self.patch_provider("claude")
        self.patch_provider("codex")

    def patch_provider(self, name):
        def fetch(creds):
            self.calls[name] += 1
            return self.raw[name]
        _, _, parse, msg = self.m.PROVIDERS[name]
        creds = ({"claudeAiOauth": {"accessToken": "x", "subscriptionType": "max"}}
                 if name == "claude" else ({"tokens": {}}, "/nonexistent"))
        self.m.PROVIDERS[name] = (lambda: creds, fetch, parse, msg)

    def state(self):
        return {"holds": {}}


class TestHolds(Base):
    def test_claude_hold_not_shortened_by_codex_success(self):
        st = self.state()
        self.raw["claude"] = (429, {})
        out = self.m.provider("claude", False, st, time.time())
        self.assertTrue(out["stale"])
        self.assertEqual(st["holds"]["claude"], self.m.HOLD_MIN)
        out = self.m.provider("codex", False, st, time.time())
        self.assertIsNone(out["error"])
        self.assertEqual(st["holds"]["claude"], self.m.HOLD_MIN)
        # persisted too, and legacy scalar is dropped on load
        with open(self.m.WIDGET_STATE, "w") as fh:
            json.dump({"hold": 99, "holds": {"claude": 900}}, fh)
        loaded = self.m._load_state()
        self.assertNotIn("hold", loaded)
        self.assertEqual(loaded["holds"], {"claude": 900})
        # and the next claude call yields instead of fetching
        n = self.calls["claude"]
        self.m.provider("claude", False, st, time.time())
        self.assertEqual(self.calls["claude"], n)


class TestValidation(Base):
    def test_empty_windows_is_error_not_cached(self):
        for name, empty in (("claude", {"limits": [], "extra_usage": {}}),
                            ("codex", {"rate_limit": {"primary_window": None}})):
            self.raw[name] = (200, empty)
            st = {"holds": {name: 900}}
            att = {}
            out = self.m.provider(name, False, st, time.time(), att)
            self.assertIn("no usage windows in response", out["error"])
            self.assertEqual(att["outcome"], "error")
            self.assertFalse(os.path.exists(self.m._cache_paths(name)[0]))
            self.assertEqual(st["holds"][name], 900)


class TestLocking(Base):
    def test_second_caller_reuses_fresh_cache(self):
        lock = os.path.join(self.m.CACHE_DIR, "claude.lock")
        fd = self.m._lock_acquire(lock)
        res = {}
        att = {}
        t = threading.Thread(target=lambda: res.update(
            out=self.m.provider("claude", False, self.state(), time.time(), att)))
        t.start()
        time.sleep(0.4)
        # the lock holder "fetches" and writes the cache
        self.m._write_cache(self.m._cache_paths("claude")[0],
                            {"fetchedAt": int(time.time()), "source": "widget", "response": CLAUDE_RAW})
        self.m._lock_release(fd)
        t.join(10)
        self.assertEqual(self.calls["claude"], 0)
        self.assertIsNone(res["out"]["error"])
        self.assertEqual(att["outcome"], "cache")

    def test_busy_serves_stale(self):
        self.m.LOCK_WAIT = 0.3
        fd = self.m._lock_acquire(os.path.join(self.m.CACHE_DIR, "claude.lock"))
        try:
            att = {}
            out = self.m.provider("claude", False, self.state(), time.time(), att)
        finally:
            self.m._lock_release(fd)
        self.assertEqual(out["error"], "busy")
        self.assertTrue(out["stale"])
        self.assertEqual(att["outcome"], "busy")
        self.assertEqual(self.calls["claude"], 0)


class TestStatusFile(Base):
    def test_status_file_shape(self):
        sf = os.path.join(self.tmp.name, "status.json")
        self.m.PROVIDERS["openrouter"] = (lambda: None,) + self.m.PROVIDERS["openrouter"][1:]
        argv = sys.argv
        sys.argv = ["aiusage.py", "--no-notify", "--max-age", "5", "--status-file", sf]
        try:
            import io
            import contextlib
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                self.m.main()
        finally:
            sys.argv = argv
        self.assertEqual(self.m.WIDGET_MAX_AGE, 5.0)
        stdout = json.loads(buf.getvalue())
        self.assertNotIn("lastAttempt", stdout["claude"])
        self.assertEqual(oct(os.stat(sf).st_mode & 0o777), "0o600")
        with open(sf) as fh:
            doc = json.load(fh)
        self.assertEqual(set(doc), {"generatedAt", "providers"})
        p = doc["providers"]
        self.assertEqual(set(p), {"claude", "codex", "openrouter"})
        self.assertEqual(p["claude"]["lastAttempt"]["outcome"], "fetched")
        self.assertIsNotNone(p["claude"]["lastAttempt"]["at"])
        self.assertIsNone(p["claude"]["lastAttempt"]["error"])
        self.assertEqual(p["openrouter"]["lastAttempt"],
                         {"at": None, "outcome": "unavailable", "error": None})
        self.assertEqual(p["claude"]["extraUsage"], {
            "is_enabled": True, "user_disabled": None, "spend_limit_reached": False,
            "used_credits": 150, "monthly_limit": 1000, "decimal_places": 2})
        models = {w["id"]: w["model"] for w in p["claude"]["windows"]}
        self.assertEqual(models, {"session": None, "weekly_scoped": "Opus"})
        self.assertEqual(p["claude"]["windows"][1]["resetsAt"], "2026-01-05T00:00:00+00:00")
        self.assertEqual(p["codex"]["lastAttempt"]["outcome"], "fetched")


class TestRefresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # HOME and the cache dir must be set BEFORE load(): the module binds
        # CACHE_DIR/WIDGET_STATE/OR_KEY_FILE from the environment at import.
        for var, val in (("HOME", self.tmp.name),
                         ("USAGE_CACHE_DIR", os.path.join(self.tmp.name, "cache")),
                         ("XDG_CONFIG_HOME", os.path.join(self.tmp.name, "cfg")),
                         ("CODEX_HOME", os.path.join(self.tmp.name, "codex"))):
            old = os.environ.get(var)
            os.environ[var] = val
            self.addCleanup(lambda v=var, o=old: os.environ.__setitem__(v, o)
                            if o is not None else os.environ.pop(v, None))
        self.m = load()
        self.assertTrue(self.m.CACHE_DIR.startswith(self.tmp.name))
        self.m.LOCK_WAIT = 3
        self.hook = None          # called when the token endpoint is hit
        self.token_status = 200
        os.makedirs(os.path.join(self.tmp.name, ".claude"))
        self.path = os.path.join(self.tmp.name, ".claude", ".credentials.json")
        self.requests = []

    def fake_request(self, method, url, headers, body=None):
        self.requests.append((method, url, headers))
        if url in (self.m.CL_TOKEN_URL, self.m.CX_TOKEN_URL):
            if self.hook:
                self.hook()
            if self.token_status != 200:
                return self.token_status, {"error": "x"}
            return 200, {"access_token": "NEWTOK", "refresh_token": "NEWREF", "expires_in": 3600}
        if url == self.m.CX_USAGE_URL:
            return self.usage_status, CODEX_RAW
        return self.usage_status, CLAUDE_RAW

    usage_status = 200

    def write_claude(self, tok, ref, exp_ms, **extra):
        with open(self.path, "w") as fh:
            json.dump({"claudeAiOauth": {"accessToken": tok, "refreshToken": ref,
                                         "expiresAt": exp_ms}, **extra}, fh)

    def read_claude(self):
        with open(self.path) as fh:
            return json.load(fh)["claudeAiOauth"]

    def expired_root(self):
        return {"claudeAiOauth": {"accessToken": "old", "refreshToken": "r1",
                                  "expiresAt": int(time.time() * 1000) - 1000}}

    def token_posts(self):
        return [u for _, u, _ in self.requests if u in (self.m.CL_TOKEN_URL, self.m.CX_TOKEN_URL)]

    def test_refresh_skipped_when_disk_token_changed(self):
        now_ms = int(time.time() * 1000)
        stale = {"claudeAiOauth": {"accessToken": "old", "refreshToken": "r1", "expiresAt": now_ms - 1000}}
        disk = {"claudeAiOauth": {"accessToken": "rotated", "refreshToken": "r2",
                                  "expiresAt": now_ms + 3600_000}, "unknownKey": 1}
        with open(self.path, "w") as fh:
            json.dump(disk, fh)
        self.m._request = self.fake_request
        self.m.claude_fetch_raw(stale)
        urls = [u for _, u, _ in self.requests]
        self.assertNotIn(self.m.CL_TOKEN_URL, urls)
        self.assertEqual(self.requests[-1][2]["Authorization"], "Bearer rotated")

    def test_refresh_merges_and_keeps_unknown_keys(self):
        now_ms = int(time.time() * 1000)
        root = {"claudeAiOauth": {"accessToken": "old", "refreshToken": "r1", "expiresAt": now_ms - 1000}}
        with open(self.path, "w") as fh:
            json.dump({"claudeAiOauth": {"accessToken": "old", "refreshToken": "r1",
                                         "expiresAt": now_ms - 1000, "scopes": ["a"]},
                       "mcpOAuth": {"k": 1}}, fh)
        self.m._request = self.fake_request
        self.m.claude_fetch_raw(root)
        with open(self.path) as fh:
            saved = json.load(fh)
        self.assertEqual(saved["claudeAiOauth"]["accessToken"], "NEWTOK")
        self.assertEqual(saved["claudeAiOauth"]["scopes"], ["a"])
        self.assertEqual(saved["mcpOAuth"], {"k": 1})
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o600")

    def test_codex_refresh_skipped_when_disk_token_changed(self):
        path = os.path.join(self.tmp.name, "auth.json")
        good = jwt(int(time.time()) + 3600)
        with open(path, "w") as fh:
            json.dump({"tokens": {"access_token": good, "refresh_token": "r2"}}, fh)
        auth = {"tokens": {"access_token": jwt(int(time.time()) - 10), "refresh_token": "r1"}}
        self.m._request = self.fake_request
        self.m._codex_creds = lambda: (auth, path)
        self.m.codex_fetch_raw((auth, path))
        self.assertNotIn(self.m.CX_TOKEN_URL, [u for _, u, _ in self.requests])
        self.assertEqual(self.requests[-1][2]["Authorization"], f"Bearer {good}")

    # ---- finding 1 + second reviewer: mid-POST rotation ----
    def test_mid_post_rotation_keeps_disk_token(self):
        now_ms = int(time.time() * 1000)
        self.write_claude("old", "r1", now_ms - 1000)
        self.hook = lambda: self.write_claude("cli", "rcli", now_ms + 7200_000, keep=1)
        self.m._request = self.fake_request
        self.m.claude_fetch_raw(self.expired_root())
        saved = self.read_claude()
        self.assertEqual((saved["accessToken"], saved["refreshToken"]), ("cli", "rcli"))
        self.assertEqual(self.requests[-1][2]["Authorization"], "Bearer cli")

    def test_invalid_grant_adopts_rotated_disk_token(self):
        now_ms = int(time.time() * 1000)
        self.write_claude("old", "r1", now_ms - 1000)
        self.token_status = 400
        self.hook = lambda: self.write_claude("cli", "rcli", now_ms + 7200_000)
        self.m._request = self.fake_request
        status, _ = self.m.claude_fetch_raw(self.expired_root())
        self.assertEqual(status, 200)
        self.assertEqual(self.requests[-1][2]["Authorization"], "Bearer cli")
        self.assertEqual(self.read_claude()["accessToken"], "cli")

    def test_codex_refresh_merge_adopt_and_account_id(self):
        path = os.path.join(self.tmp.name, "auth.json")
        exp = int(time.time())
        tok_old = jwt(exp - 10)
        with open(path, "w") as fh:
            json.dump({"tokens": {"access_token": tok_old, "refresh_token": "r1", "account_id": "A"},
                       "extra": 1}, fh)
        self.m._request = self.fake_request
        # 401-style failure with rotation by the CLI during the POST
        newer = jwt(exp + 7200)
        def rotate():
            with open(path, "w") as fh:
                json.dump({"tokens": {"access_token": newer, "refresh_token": "r2", "account_id": "B"}}, fh)
        self.hook, self.token_status = rotate, 400
        auth = {"tokens": {"access_token": tok_old, "refresh_token": "r1", "account_id": "A"}}
        self.m.codex_fetch_raw((auth, path))
        self.assertEqual(auth["tokens"]["account_id"], "B")
        hdr = self.requests[-1][2]
        self.assertEqual((hdr["Authorization"], hdr["ChatGPT-Account-Id"]), (f"Bearer {newer}", "B"))

    def test_codex_refresh_writes_and_keeps_unknown_keys(self):
        path = os.path.join(self.tmp.name, "auth.json")
        tok_old = jwt(int(time.time()) - 10)
        with open(path, "w") as fh:
            json.dump({"tokens": {"access_token": tok_old, "refresh_token": "r1", "account_id": "A"},
                       "OPENAI_API_KEY": "k"}, fh)
        self.m._request = self.fake_request
        auth = {"tokens": {"access_token": tok_old, "refresh_token": "r1", "account_id": "A"}}
        self.m.codex_fetch_raw((auth, path))
        with open(path) as fh:
            saved = json.load(fh)
        self.assertEqual(saved["tokens"]["access_token"], "NEWTOK")
        self.assertEqual(saved["tokens"]["account_id"], "A")
        self.assertEqual(saved["OPENAI_API_KEY"], "k")

    def test_token_status_classification(self):
        now_ms = int(time.time() * 1000)
        self.m._request = self.fake_request
        for st, exc, text in ((400, self.m.SessionExpired, "session expired"),
                              (403, self.m.SessionExpired, "session expired"),
                              (429, self.m.RateLimited, "429"),
                              (500, RuntimeError, "HTTP 500"),
                              (0, RuntimeError, "token refresh failed")):
            self.write_claude("old", "r1", now_ms - 1000)
            self.token_status = st
            with self.assertRaises(exc) as cm:
                self.m.claude_fetch_raw(self.expired_root())
            self.assertIn(text, str(cm.exception))
            if exc is RuntimeError:
                self.assertNotIsInstance(cm.exception, self.m.SessionExpired)

    def test_token_429_feeds_provider_hold(self):
        self.write_claude("old", "r1", int(time.time() * 1000) - 1000)
        self.token_status = 429
        self.m._request = self.fake_request
        st, att = {"holds": {}}, {}
        out = self.m.provider("claude", False, st, time.time(), att)
        self.assertEqual(att["outcome"], "rate-limited")
        self.assertEqual(st["holds"]["claude"], self.m.HOLD_MIN)
        self.assertTrue(os.path.exists(self.m._cache_paths("claude")[1]))
        self.assertTrue(out["stale"])

    def test_token_network_error_is_plain_error(self):
        self.write_claude("old", "r1", int(time.time() * 1000) - 1000)
        self.token_status = 503
        self.m._request = self.fake_request
        st, att = {"holds": {}}, {}
        out = self.m.provider("claude", False, st, time.time(), att)
        self.assertEqual(att["outcome"], "error")
        self.assertNotIn("sign in", out["error"])
        self.assertEqual(st["holds"], {})

    def test_no_second_refresh_after_401(self):
        self.write_claude("old", "r1", int(time.time() * 1000) - 1000)
        self.usage_status = 401
        self.m._request = self.fake_request
        status, _ = self.m.claude_fetch_raw(self.expired_root())
        self.assertEqual(status, 401)
        self.assertEqual(len(self.token_posts()), 1)

    def test_corrupt_creds_is_error_with_stale_cache(self):
        with open(self.path, "w") as fh:
            fh.write('{"claudeAiOauth": {"acc')
        with self.assertRaises(Exception):
            self.m._claude_creds()
        self.m._write_cache(self.m._cache_paths("claude")[0],
                            {"fetchedAt": int(time.time()) - 5000, "source": "crew", "response": CLAUDE_RAW})
        att = {}
        out = self.m.provider("claude", False, {"holds": {}}, time.time(), att)
        self.assertTrue(out["available"])
        self.assertTrue(out["stale"])
        self.assertIn("credentials unreadable", out["error"])
        self.assertEqual(att["outcome"], "error")
        self.assertTrue(out["windows"])

    def test_missing_creds_is_unavailable(self):
        att = {}
        self.assertEqual(self.m.provider("claude", False, {"holds": {}}, time.time(), att),
                         {"available": False})
        self.assertEqual(att["outcome"], "unavailable")

    def test_atomic_write_through_symlink_and_mode(self):
        real = os.path.join(self.tmp.name, "real.json")
        link = os.path.join(self.tmp.name, "link.json")
        with open(real, "w") as fh:
            fh.write("{}")
        os.chmod(real, 0o644)
        os.symlink(real, link)
        self.m._write_json_atomic(link, {"a": 1})
        self.assertTrue(os.path.islink(link))
        with open(real) as fh:
            self.assertEqual(json.load(fh), {"a": 1})
        self.assertEqual(oct(os.stat(real).st_mode & 0o777), "0o600")
        self.assertEqual([f for f in os.listdir(self.tmp.name) if f.startswith(".tmp.")], [])

    def test_openrouter_key_429_is_429(self):
        def req(method, url, headers, body=None):
            return (200, {"data": {"total_credits": 10, "total_usage": 1}}) if url == self.m.OR_CREDITS_URL \
                else (429, {})
        self.m._request = req
        self.assertEqual(self.m.openrouter_fetch_raw("k")[0], 429)


class TestParsing(Base):
    def test_null_utilization_skipped(self):
        w = self.m._claude_windows({"limits": [
            {"kind": "session", "group": "session", "percent": None},
            {"kind": "weekly_all", "group": "weekly"},
            {"kind": "weekly_all", "group": "weekly", "percent": 0}]})
        self.assertEqual([x["percent"] for x in w], [0.0])  # a real 0 is kept
        self.assertEqual(self.m._claude_windows({"five_hour": {"utilization": None}, "seven_day": {}}), [])
        self.assertEqual(self.m._codex_windows({"primary_window": {"used_percent": None}}), [])

    def test_all_null_is_no_windows_error(self):
        for name, raw in (("claude", {"limits": [{"foo": "bar"}]}),
                          ("codex", {"rate_limit": {"primary_window": {"used_percent": None}}})):
            self.raw[name] = (200, raw)
            att = {}
            out = self.m.provider(name, False, self.state(), time.time(), att)
            self.assertIn("no usage windows in response", out["error"])
            self.assertFalse(os.path.exists(self.m._cache_paths(name)[0]))

    def test_legacy_seven_day_model(self):
        w = self.m._claude_windows({
            "five_hour": {"utilization": 3}, "seven_day": {"utilization": 4},
            "seven_day_opus": {"utilization": 50, "resets_at": "x"},
            "seven_day_sonnet": {"utilization": None}})
        self.assertEqual([x["id"] for x in w], ["five_hour", "seven_day", "weekly_scoped"])
        self.assertEqual((w[2]["group"], w[2]["model"], w[2]["percent"]), ("weekly", "Opus", 50.0))

    def test_extra_usage_from_spend_schema(self):
        usage = {"limits": [], "spend": {
            "enabled": True, "disabled_reason": None, "percent": 40,
            "used": {"amount_minor": 400, "exponent": 2, "currency": "USD"},
            "limit": {"amount_minor": 1000, "exponent": 2, "currency": "USD"}}}
        e = self.m._claude_extra(usage)
        self.assertIs(e["is_enabled"], True)
        self.assertIs(e["spend_limit_reached"], False)
        self.assertEqual((e["used_credits"], e["monthly_limit"], e["decimal_places"]), (400, 1000, 2))
        usage["spend"].update(enabled=False, disabled_reason="org_level_disabled_until")
        e = self.m._claude_extra(usage)
        self.assertIs(e["is_enabled"], False)
        self.assertIs(e["spend_limit_reached"], True)
        self.assertEqual(self.m._claude_extra({"limits": []})["is_enabled"], None)


class TestCacheAndStatus(Base):
    def test_corrupt_cache_falls_through_to_fetch(self):
        self.m._write_cache(self.m._cache_paths("claude")[0],
                            {"fetchedAt": int(time.time()), "source": "widget", "response": {"limits": []}})
        att = {}
        out = self.m.provider("claude", False, self.state(), time.time(), att)
        self.assertEqual(self.calls["claude"], 1)
        self.assertEqual(att["outcome"], "fetched")
        self.assertFalse(out["stale"])

    def test_bad_cache_and_failed_fetch_is_stale(self):
        self.m._write_cache(self.m._cache_paths("claude")[0],
                            {"fetchedAt": int(time.time()), "source": "widget", "response": {"limits": []}})
        self.raw["claude"] = (500, "boom")
        out = self.m.provider("claude", False, self.state(), time.time(), {})
        self.assertTrue(out["stale"])
        self.assertTrue(out["error"])

    def test_future_fetchedat_is_not_a_hit(self):
        self.m._write_cache(self.m._cache_paths("claude")[0],
                            {"fetchedAt": int(time.time()) + 500, "source": "widget", "response": CLAUDE_RAW})
        self.m.provider("claude", False, self.state(), time.time(), {})
        self.assertEqual(self.calls["claude"], 1)

    def test_now_is_taken_after_lock(self):
        # caller's `now` is hours old; the cache is fresh by the real clock
        self.m._write_cache(self.m._cache_paths("claude")[0],
                            {"fetchedAt": int(time.time()), "source": "crew", "response": CLAUDE_RAW})
        att = {}
        self.m.provider("claude", False, self.state(), time.time() - 7200, att)
        self.assertEqual(att["outcome"], "cache")
        self.assertEqual(self.calls["claude"], 0)

    def test_status_merge_never_regresses_fetchedat(self):
        sf = os.path.join(self.tmp.name, "s", "status.json")
        mk = lambda ts, pct: {"available": True, "fetchedAt": ts, "windows": [{"percent": pct}],
                              "lastAttempt": {"at": ts, "outcome": "cache", "error": None}}
        self.m._write_status_file(sf, {"claude": mk(2000, 96), "codex": mk(1000, 1)})
        self.m._write_status_file(sf, {"claude": mk(1500, 80), "codex": mk(1100, 2)})
        with open(sf) as fh:
            p = json.load(fh)["providers"]
        self.assertEqual(p["claude"]["fetchedAt"], 2000)
        self.assertEqual(p["claude"]["windows"][0]["percent"], 96)
        self.assertEqual(p["codex"]["fetchedAt"], 1100)
        self.assertEqual(oct(os.stat(sf).st_mode & 0o777), "0o600")

    def test_status_write_failure_exits_nonzero(self):
        import io
        import contextlib
        blocker = os.path.join(self.tmp.name, "file")
        open(blocker, "w").close()
        argv = sys.argv
        sys.argv = ["aiusage.py", "--no-notify", "--status-file", os.path.join(blocker, "sub", "s.json")]
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    self.m.main()
        finally:
            sys.argv = argv
        self.assertEqual(cm.exception.code, 1)


class TestAgy(Base):
    def write(self, fetched, pools, credits=0.0):
        self.m.AGY_FILE = os.path.join(self.tmp.name, "agy.json")
        with open(self.m.AGY_FILE, "w") as f:
            json.dump({"fetchedAt": fetched, "source": "crew agy-usage",
                       "response": {"pools": pools, "credits": credits}}, f)

    def test_missing_file_is_unavailable(self):
        self.m.AGY_FILE = os.path.join(self.tmp.name, "nope.json")
        self.assertFalse(self.m.agy_from_file()["available"])

    def test_windows_rollover_and_order(self):
        now = 2_000_000_000
        self.write(now - 60, [
            {"pool": "claude_gpt", "window": "5h", "pct_used": 0.0, "resetsAt": None},
            {"pool": "gemini", "window": "weekly", "pct_used": 13.0, "resetsAt": "2033-06-01T00:00:00Z"},
            {"pool": "gemini", "window": "5h", "pct_used": 50.0, "resetsAt": "2020-01-01T00:00:00Z"},
        ])
        d = self.m.agy_from_file(now)
        self.assertFalse(d["stale"])
        self.assertEqual([w["id"] for w in d["windows"]], ["gemini_5h", "gemini_weekly", "claude_gpt_5h"])
        g5 = d["windows"][0]
        self.assertEqual((g5["percent"], g5["resetsAt"]), (0.0, None))  # reset already passed
        self.assertEqual(d["windows"][1]["percent"], 13.0)
        self.assertFalse(d["windows"][2]["active"])                       # unused pool dimmed
        self.assertIsNone(d["plan"])

    def test_stale(self):
        now = 2_000_000_000
        self.write(now - 3 * 3600, [{"pool": "gemini", "window": "5h", "pct_used": 5, "resetsAt": None}], 7)
        d = self.m.agy_from_file(now)
        self.assertTrue(d["stale"])
        self.assertIn("3h", d["error"])
        self.assertEqual(d["plan"], "7 credits")


class TestArgs(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def parse(self, *a):
        return self.m._parse_args(list(a))

    def test_equals_forms_and_flags(self):
        a = self.parse("--max-age=300", "--status-file=/x/y.json", "--force", "--pretty",
                       "--no-notify", "--or-floors=20,5")
        self.assertEqual((a.status_file, a.force, a.pretty), ("/x/y.json", True, True))
        self.assertEqual((self.m.WIDGET_MAX_AGE, self.m.OR_NOTIFY, self.m.OR_FLOORS), (300.0, False, [20.0, 5.0]))

    def test_rejects_bad_values(self):
        import io
        import contextlib
        for bad in (["--max-age", "inf"], ["--max-age", "nan"], ["--max-age", "-1"],
                    ["--max-age", "abc"], ["--status-file", "--no-notify"], ["--status-file"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                self.parse(*bad)
            self.assertNotEqual(cm.exception.code, 0, bad)


if __name__ == "__main__":
    unittest.main()
