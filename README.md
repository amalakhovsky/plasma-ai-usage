# AI Usage — KDE Plasma widget

A compact Plasma 6 panel widget that shows your **Claude Code** and **Codex** usage
quotas as two rings — one per provider — with a detailed pop-up. It reads the same
local credentials the `claude` and `codex` CLIs already use, so there's no extra
login, and it auto-detects which providers are present (a ring only appears when
that provider is signed in).

The KDE counterpart to the GNOME
[ClaudeCodeUsage](https://github.com/amalakhovsky/ClaudeCodeUsage) extension.

## Features

- **Two rings** in the panel — Claude (warm) and Codex (teal) — each shows the
  shortest window's utilization, shifting toward amber/red as you approach the limit.
- **Pop-up** with every active window (session / weekly / per-model), reset
  countdowns, your tier/plan, and Claude's credit/spend bar.
- **Self-describing** windows: labels come from the API (Claude's `limits[]`,
  Codex's `limit_window_seconds`), never hard-coded — so they stay correct as the
  providers change their windows.
- **Refresh + write-back** of both OAuth tokens when they're near expiry, in place,
  0600, atomically — it rides on the CLIs' credentials without fighting them.
- **Auto-detect**: no Claude creds → no Claude ring; same for Codex.

## Privacy

Everything runs locally. The widget reads:

- `~/.claude/.credentials.json` (Claude Code's OAuth tokens)
- `~/.codex/auth.json` (Codex's OAuth tokens)

Your tokens never leave the machine except in the two API calls the CLIs make
anyway — `api.anthropic.com` and `chatgpt.com`. Nothing is sent anywhere else.

## Requirements

- KDE Plasma 6
- `python3` (standard library only — no pip installs)
- Signed in with the `claude` and/or `codex` CLI

## Install

```sh
git clone https://github.com/amalakhovsky/plasma-ai-usage
cd plasma-ai-usage
./install.sh
```

Then right-click your panel → **Add Widgets…** → **AI Usage**, and drag it in.
To update after `git pull`, run `./install.sh` again.

## Configuration

Right-click the widget → **Configure**:

- **Refresh interval** (default 120 s)
- **Panel ring shows** — shortest window (default), weekly, or most-used
- **Show percentage** next to each ring
- **Ring size**

## How it works

QML can't cleanly write files or run an OAuth refresh, so a small stdlib-only
Python helper (`package/contents/tools/aiusage.py`) does all credential IO, token
refresh, API calls and normalization, and prints one JSON blob. The plasmoid runs
it on a timer via Plasma's executable data source and just draws the result.

## License

GPL-3.0-or-later.
