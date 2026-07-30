# session-divemo — project guide

**di**scord **ve**rsion **mo**nitor. A long-running Discord **gateway bot** that,
for each Session platform, compares the latest *published store release* against
the latest *GitHub release* and posts to a Discord channel when they diverge
(warning + user tags), repeats the warning until resolved, and posts an
all-clear once back in sync. Plus a `/versions` slash command.

- Remote: `git@github.com:session-foundation/session-divemo.git`, default branch `main`.
- Language: Python 3, `discord.py` (gateway). No web server; a single daemon.
- Built entirely in one prior session (originally scaffolded under a different
  repo, `session-shared-scripts`, then moved here — this is the real home).

## Layout

| File | Purpose |
| --- | --- |
| `bot.py` | Discord `Client` + `CommandTree`, poll loop (`tasks.loop`), message/embed formatting, `/versions`, CLI (`--check-once`), config loader |
| `sources.py` | Version fetchers: GitHub, App Store, Play, deb repo, F-Droid; `STORE_DISPLAY_NAMES`; `fetch_store` dispatch |
| `state.py` | SQLite `StateStore` + the **pure** transition state machine `evaluate()` (all decision logic; no IO) |
| `version_utils.py` | Version normalize + compare (uses `packaging.version`) |
| `config.sample.yaml` | Committed template. Real config is `config.yaml` (git-ignored) |
| `divemo.service` | Hardened systemd **system**-service template (generic `/opt` + dedicated user) |
| `tests/test_core.py` | 36 unit/integration tests (pure logic + SQLite round-trip) |

`bot.py`/`sources.py`/`state.py`/`version_utils.py` import each other flat (repo
root is on `sys.path`), so run from the repo dir.

## Version sources (exact identifiers)

- **Android → Play Store**: *scraped* from `https://play.google.com/store/apps/details?id=network.loki.messenger`.
  No API exists; the version is pulled from the page JSON via regex `[[["x.y.z"]]`.
  **This is the fragile source** — if Android starts erroring, Google likely
  changed the page and the regex in `sources.py` needs a tweak.
- **iOS → App Store**: iTunes Lookup API, `bundleId=com.loki-project.loki-messenger` → `results[0].version`. Official/stable.
- **Desktop → APT repo**: parse `https://deb.session.foundation/dists/sid/main/binary-amd64/Packages.gz`,
  package **`session-desktop`** (NOT `session-messenger-desktop`, which is an old
  transitional package). Debian epoch/revision are stripped to the upstream version.
- **Android → F-Droid** (two checks, both vs the **session-android** GitHub release):
  parse `index-v1.json` for app **`network.loki.messenger`**, taking the
  `versionName` of the highest integer `versionCode` (entries are per-ABI splits).
  Same fetcher (`fetch_fdroid`), two endpoints:
    - **merged** — `raw.githubusercontent.com/session-foundation/session-fdroid/master/fdroid/repo/index-v1.json`
      (default branch is **`master`**). This is what's been merged, and must
      *exactly* match GitHub (`strict_sync`). Each session-android release
      auto-opens a `Release X.Y.Z` PR here that **must be merged right after the
      GitHub release** — this check is the "did we forget to merge it" alarm.
    - **live** — `https://fdroid.getsession.org/fdroid/repo/index-v1.json` (the
      served repo F-Droid clients pull). App-store semantics: behind = deploy lag
      (info only), not a warning.
- **GitHub**: `api.github.com/repos/session-foundation/session-{android,ios,desktop}/releases/latest`.
  Tag prefixes differ (desktop is `v1.18.1`; android/ios have no `v`); `normalize_version` strips a leading `v`.
  `session-android` backs three platforms (Play + both F-Droid checks) but is
  fetched **once per cycle** — `check_versions` memoizes GitHub results by repo.

## Alert semantics (state machine in `state.py:evaluate`)

Compare store `S` vs GitHub `G` per platform each poll:

- **⚠️ Warning** — out-of-sync divergence. App stores: only when `S > G` (store
  *ahead*; a public release whose source isn't out). `strict_sync` platforms
  (the APT repo — we control it, it must always match): `S ≠ G` in **either**
  direction (behind counts). Tags the platform's configured users.
- **⚠️ Reminder** — while unresolved, re-posted (re-tagging) every
  `warning_reminder_hours` (default 12); re-sent immediately if divergence grows.
- **✅ All-clear** — once back in sync; only after a real warning.
- **ℹ️ Info** — GitHub advanced (not yet in store) / store advanced without
  diverging. No tags. (For `strict_sync`, a GitHub-ahead move is a warning, not info.)

State (versions, `warning_active`, `last_warned_at`) is latched in SQLite, so
warnings fire once, reminders are paced, and all-clears only follow a warning —
across restarts. First observation of a platform seeds silently unless already
out of sync. `/versions` is public and labels each source (Play Store / App
Store / APT repo / F-Droid repo).

## Config

Copy `config.sample.yaml` → `config.yaml` (git-ignored; holds the bot token and
per-platform tag user IDs). Key fields:

- `discord.token` (or `$DISCORD_BOT_TOKEN`), `discord.channel_id`, `discord.guild_id`.
- `check_interval_seconds` (default **300**). Don't go far below 60 (Play scrape + GitHub 60/h unauth).
- `warning_reminder_hours` (default 12).
- `github.token` — **intentionally blank**; see "no GitHub token" below.
- `state_db` — SQLite path.
- `platforms.<name>`: `display_name`, `github_repo`, `store:` block, `tag_user_ids`,
  and `strict_sync: true` (desktop APT + the **F-Droid *merged*** check).
  Several platforms may share one `github_repo` (Android's Play + both F-Droid
  checks all use `session-android`).

Discord app is **"Session Release Monitor"** (app id `1526269506488504471`). The
bot needs **View Channel + Send Messages + Embed Links** *on the target channel*
(private channels override the server-wide grant — add the bot to the channel).
Slash commands come from the `applications.commands` scope, not a permission bit.
Channel/guild/tag IDs live in `config.yaml`.

## Running & tests

```sh
python3 bot.py -c config.yaml               # run the bot
python3 bot.py -c config.yaml --check-once  # dry run: fetch+evaluate+print, no Discord
python3 -m unittest discover tests          # 36 tests
```

## Deployment (host: angus)

Runs as a **systemd system service** but out of the user's home, per jagerman's
setup (not the `/opt` + dedicated-user layout in the committed template):

- Code: `~jagerman/session-divemo` (this clone), run as user **jagerman**.
- DB: `~jagerman/session-divemo/db/divemo.db` (git-ignored via `*.db`).
- OS: Debian **bookworm** (Python 3.11) with **`python3-discord` 2.2.2** from apt
  — fine, because the `>=2.5` requirement is only needed on Python **3.13+**
  (older discord.py imports stdlib `audioop`, which 3.13 removed).
- Base unit `divemo.service` copied to `/etc/systemd/system/`, with a `systemctl
  edit` **drop-in** overriding: `User/Group=jagerman`, `WorkingDirectory` +
  `ExecStart` → home paths, `ProtectHome=read-only`, `StateDirectory=` cleared,
  and `ReadWritePaths=~jagerman/session-divemo/db` (needed because
  `ProtectSystem=strict` makes the tree read-only). `mkdir -p db` first.

Update workflow: `ssh angus && cd ~/session-divemo && git pull && sudo systemctl restart divemo`.
Logs: `journalctl -u divemo -f`.

## Key decisions & gotchas (hard-won)

- **No GitHub token.** At 300s the unauthenticated limit (60/h) is plenty (36/h
  across the 3 distinct repos). A fine-grained PAT was tried but the
  `session-foundation` org **rejects fine-grained tokens with lifetime > 366 days**
  (403 with a policy message) — so we run unauthenticated. Only add a token if
  polling < ~60s. Note: the per-cycle GitHub memo in `check_versions` is what
  keeps this at 3 repos — without it the two extra `session-android` platforms
  (F-Droid) would push us to 60/h, right at the ceiling.
- **`discord.py` / Python:** `< 2.5` works on Python ≤ 3.12; **≥ 2.5 required on
  3.13+** (audioop). `requirements.txt` encodes this with environment markers.
- **deb package is `session-desktop`**, not `session-messenger-desktop`.
- **iTunes Lookup API is eventually consistent.** Apple's `itunes.apple.com/lookup`
  briefly re-serves the *previous* version (CDN/region caching), so the observed
  App Store version can dip and recover over hours. This once double-fired the
  "App Store updated" `INFO_STORE`. Fix: `state.py` tracks a per-platform
  `store_high` watermark and only announces `INFO_STORE` on a genuinely **new
  high** — a dip-and-recover to an already-announced version is silent. The
  *true* fetched version is still persisted (state never lies), so a genuine
  version pull stays visible in `/versions`; we just don't proactively alert on a
  decrease (indistinguishable from a transient flap for hours). App-store scoped;
  `strict_sync` platforms keep exact behavior.
- **`/versions` works without channel perms** (interaction responses bypass them);
  proactive `channel.send` alerts do **not** — hence the channel-permission need.
- **Bot base class is `discord.Client` + `CommandTree`**, deliberately not
  `commands.Bot` (which warns about the privileged message-content intent we
  don't use). No privileged intents required.

## Conventions / preferences

- **Open-source project: never propose proprietary/vendor-lock-in solutions**
  (e.g. GitHub-Actions-only state stores). Prefer portable tools (SQLite, plain
  config, standard protocols).
- Prefer **system Python packages** over per-project venvs.
- `config.yaml` and `*.db` are git-ignored — never commit secrets or state.
