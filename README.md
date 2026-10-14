# session-divemo

**di**scord **ve**rsion **mo**nitor — a Discord bot that watches, for each
Session platform, the **latest published store release** against the **latest
published GitHub release**, and posts to a Discord channel when they diverge.

| Platform         | GitHub repo       | Store source                                            |
| ---------------- | ----------------- | ------------------------------------------------------- |
| Session Android  | `session-android` | Google Play listing (scraped — no official API exists)  |
| Session iOS      | `session-ios`     | App Store, via the public iTunes lookup API             |
| Session Desktop  | `session-desktop` | `deb.session.foundation` apt repo (`sid` `Packages.gz`) |
| Session Desktop  | `session-desktop` | Flathub `network.loki.Session`: merged manifest + live appstream API |

## What it posts

For each platform, on every poll it compares the store version `S` with the
GitHub version `G`:

- **⚠️ Warning** — an out-of-sync divergence. For app stores this means `S > G`
  (the store is **ahead** of GitHub — a public release whose source hasn't been
  published). For a **`strict_sync`** platform (the APT repo, which we control
  and which must always match GitHub) it means `S ≠ G` in **either** direction —
  behind counts too. The configured users for that platform are tagged.
- **⚠️ Reminder** — while a warning stays unresolved, it is re-posted (re-tagging)
  every `warning_reminder_hours` (default 12), and re-sent immediately if the
  divergence grows.
- **✅ All-clear** — once `S` and `G` are back in sync. Only sent if a warning
  was actually outstanding.
- **ℹ️ GitHub advanced** — GitHub moved forward and is not yet in the store.
  Informational, no tags. (For a `strict_sync` platform this is a warning, not
  an info.)
- **ℹ️ Store advanced** — the store version moved forward without diverging.
  Informational, no tags.

If a platform is already out of sync the first time it is seen, the warning
fires immediately; otherwise the first observation just seeds the baseline
silently. Warning state (and the reminder clock) is latched in SQLite, so
warnings fire once, reminders are paced, and all-clears only follow a real
warning — even across bot restarts.

## Configuration

Copy the sample and edit it. **`config.yaml` is git-ignored and must not be
committed** — it holds the bot token and the per-platform lists of users to
tag. Only `config.sample.yaml` is tracked.

```sh
cp config.sample.yaml config.yaml
$EDITOR config.yaml
```

Key fields (see `config.sample.yaml` for the full, commented example):

- `discord.token` — bot token (or set `$DISCORD_BOT_TOKEN` instead).
- `discord.channel_id` — channel to post into.
- `discord.guild_id` — optional; syncs the `/versions` slash command to that
  server instantly (global sync otherwise takes ~1h).
- `check_interval_seconds` — poll interval (default `300`). Don't go far below
  `60`: the Play Store is scraped and GitHub allows 60 unauthenticated req/h.
- `warning_reminder_hours` — how often to repeat an unresolved warning (default `12`).
- `github.token` — optional; only needed to poll faster than ~60s. Unauthenticated
  is fine at the default interval and avoids GitHub org token policies.
- `state_db` — SQLite path (default `divemo.db`, git-ignored).
- `platforms.<name>.tag_user_ids` — Discord user IDs to tag in that platform's
  warnings. Lists can differ per platform.
- `platforms.<name>.strict_sync` — `true` for the APT repo: warn on any GitHub
  mismatch (behind or ahead), not just when it's ahead. Omit/`false` for stores.

The bot needs permission to send messages (and use application commands) in the
target channel. No privileged intents are required.

## Running

Dependencies are standard packages (see `requirements.txt`); on Python 3.13+
you need `discord.py >= 2.5`. Install them however you manage Python packages,
e.g.:

```sh
pip install -r requirements.txt   # or use your system packages
```

Then:

```sh
python3 bot.py -c config.yaml
```

### Dry run (no Discord connection)

Fetch and evaluate every platform once, printing what it found and which events
*would* be sent, then update the state DB:

```sh
python3 bot.py -c config.yaml --check-once
```

### Slash command

`/versions` posts (visibly, so it can be referenced in the conversation) the
latest-seen versions per platform, labelled with each platform's real source —
**Play Store** / **App Store** / **APT repo** vs **GitHub** — and flags any
platform currently in a warning state with ⚠️.

## Running as a service (systemd)

`divemo.service` is a hardened **system service** template — it runs as a
dedicated unprivileged `divemo` user, autostarts on boot, restarts on failure,
and logs to journald (so it's visible to all admins via `systemctl`). Suggested
FHS layout:

| Path | Purpose |
| --- | --- |
| `/opt/session-divemo` | the code (this repo) |
| `/etc/divemo/config.yaml` | config, mode `640` `root:divemo` (holds the token) |
| `/var/lib/divemo/` | SQLite state (auto-created via `StateDirectory=`) |

```sh
sudo useradd --system --no-create-home --shell /usr/sbin/nologin divemo
sudo git clone git@github.com:session-foundation/session-divemo.git /opt/session-divemo
sudo pip install -r /opt/session-divemo/requirements.txt   # or system packages

# config in /etc, readable only by root + the divemo user
sudo install -d -m 750 -o root -g divemo /etc/divemo
sudo install -m 640 -o root -g divemo /opt/session-divemo/config.sample.yaml /etc/divemo/config.yaml
sudoedit /etc/divemo/config.yaml    # fill in token/channel/guild/tags;
                                    # set state_db: "/var/lib/divemo/divemo.db"

sudo cp /opt/session-divemo/divemo.service /etc/systemd/system/divemo.service
sudo systemctl daemon-reload
sudo systemctl enable --now divemo.service
```

Status and logs:

```sh
systemctl status divemo
journalctl -u divemo -f
```

Because the unit sets `ProtectSystem=strict`, the code and config are mounted
read-only; only `/var/lib/divemo` is writable, so `state_db` **must** point
there (`/var/lib/divemo/divemo.db`).

## Tests

Pure comparison/transition logic and the SQLite round-trip are unit tested:

```sh
python3 -m unittest discover tests
```

## Layout

| File                 | Purpose                                                     |
| -------------------- | ----------------------------------------------------------- |
| `bot.py`             | Discord bot, poll loop, message formatting, CLI entry point |
| `sources.py`         | Version fetchers (GitHub, App Store, Play, deb repo)        |
| `state.py`           | SQLite state store + the pure transition state machine      |
| `version_utils.py`   | Version normalization and comparison                        |
| `config.sample.yaml` | Sample config (copy to the git-ignored `config.yaml`)       |
| `divemo.service`     | systemd user-service template                               |
| `tests/`             | Unit + integration tests                                    |

## License

GPL-3.0-or-later. See [`LICENSE`](LICENSE).
