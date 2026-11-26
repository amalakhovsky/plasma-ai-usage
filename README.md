# AI Usage — KDE Plasma widget

A compact Plasma 6 panel widget that shows your **Claude Code** and **Codex** usage
quotas as rings — one per provider — with a detailed pop-up, plus an optional
**OpenRouter** ring for API credit. It reads the same local credentials the
`claude` and `codex` CLIs already use, so there's no extra login, and it
auto-detects which providers are present (a ring only appears when that provider
is set up).

![Claude, Codex and OpenRouter rings in the panel](screenshots/panel.png)

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
- **Auto-detect**: no Claude creds → no Claude ring; same for Codex and OpenRouter.
- **OpenRouter ring** (optional): how much of the key's spending cap is used,
  labelled with the dollars left on the account (or the percentage). The ring turns
  amber/red when the prepaid balance drops below thresholds you set, and can send a
  desktop notification when it crosses one.
- **Gemini ring** (optional): the Antigravity (`agy`) Gemini and Claude/GPT pools, read
  from `~/.cache/ai-usage/agy.json`. The widget never launches `agy`; something else must
  write that file (e.g. a timer running `agy -p /usage`). Dimmed once it is >30 min old.

## Privacy

Everything runs locally. The widget reads:

- `~/.claude/.credentials.json` (Claude Code's OAuth tokens)
- `~/.codex/auth.json` (Codex's OAuth tokens)
- `~/.config/ai-usage/openrouter.key` (only if you add one; see below)

Your tokens never leave the machine except in the API calls the CLIs make
anyway — `api.anthropic.com` and `chatgpt.com` — and, if configured,
`openrouter.ai`. Nothing is sent anywhere else.

## Requirements

- KDE Plasma 6
- `python3` (standard library only — no pip installs)
- Signed in with the `claude` and/or `codex` CLI, and/or an OpenRouter API key

## Install

```sh
git clone https://github.com/amalakhovsky/plasma-ai-usage
cd plasma-ai-usage
./install.sh
```

Then right-click your panel → **Add Widgets…** → **AI Usage**, and drag it in.
To update after `git pull`, run `./install.sh` again.

## OpenRouter

Put your OpenRouter API key on a single line in `~/.config/ai-usage/openrouter.key`
and make it readable only by you:

```sh
mkdir -p ~/.config/ai-usage && chmod 700 ~/.config/ai-usage
printf '%s\n' 'sk-or-...' > ~/.config/ai-usage/openrouter.key
chmod 600 ~/.config/ai-usage/openrouter.key
```

(`$OPENROUTER_KEY_FILE` overrides the path.) The ring appears on the next refresh.
It uses two read-only calls, `/api/v1/credits` (balance) and `/api/v1/auth/key`
(the key's limit and when it resets). If the key has a spending cap (daily, weekly
or monthly), the ring shows how much of it is used; if not, how much of the credit
you have bought is used up.

## Configuration

Right-click the widget → **Configure**:

- **Refresh interval** (default 300 s)
- **Panel ring shows** — shortest window (default), weekly, or most-used
- **Show percentage** next to each ring
- **Ring size**
- **OpenRouter**: warning and critical balance in dollars (default $10 / $3),
  label (dollars left or percent of cap), and notifications on/off

## How it works

QML can't cleanly write files or run an OAuth refresh, so a small stdlib-only
Python helper (`package/contents/tools/aiusage.py`) does all credential IO, token
refresh, API calls and normalization, and prints one JSON blob. The plasmoid runs
it on a timer via Plasma's executable data source and just draws the result.

## License

GPL-3.0-or-later.

## Using it from scripts

`aiusage.py` is the only thing that should call the usage endpoints, so other
tools should run it instead of hitting the APIs. It is safe to run concurrently
(per-provider file locks) and honours per-provider 429 holds.

```sh
python3 aiusage.py --no-notify --max-age 300 --status-file ~/.cache/ai-usage/status.json >/dev/null
```

`--max-age SECONDS` reuses the shared cache if it is younger than that (default
600). `--status-file PATH` atomically writes a 0600 JSON document with
`generatedAt` and, per provider, the same data as stdout plus
`lastAttempt {at, outcome, error}`.
