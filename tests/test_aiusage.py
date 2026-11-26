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
            "is_enabled": True, "user_disabled": None, "spend_limit_reached": None,
            "used_credits": 150, "monthly_limit": 1000, "decimal_places": 2})
        models = {w["id"]: w["model"] for w in p["claude"]["windows"]}
        self.assertEqual(models, {"session": None, "weekly_scoped": "Opus"})
        self.assertEqual(p["claude"]["windows"][1]["resetsAt"], "2026-01-05T00:00:00+00:00")
        self.assertEqual(p["codex"]["lastAttempt"]["outcome"], "fetched")


class TestRefresh(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.tmp.name
        self.addCleanup(lambda: os.environ.__setitem__("HOME", self.old_home)
                        if self.old_home is not None else os.environ.pop("HOME", None))
        os.makedirs(os.path.join(self.tmp.name, ".claude"))
        self.path = os.path.join(self.tmp.name, ".claude", ".credentials.json")
        self.requests = []

    def fake_request(self, method, url, headers, body=None):
        self.requests.append((method, url, headers))
        if url == self.m.CL_TOKEN_URL:
            return 200, {"access_token": "NEWTOK", "refresh_token": "NEWREF", "expires_in": 3600}
        return 200, {"limits": []}

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


if __name__ == "__main__":
    unittest.main()
