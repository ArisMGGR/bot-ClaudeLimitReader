# ClaudeLimitReader

**Install the service** (run `python run.py setup` first if the bot isn't configured yet):

```bash
python run.py service install
```

**Uninstall the service** (stops the bot and removes the service):

```bash
sudo python3 run.py service uninstall --system
```

On Windows, run `python run.py service uninstall` instead, from an elevated
terminal if the service was installed from one. See
[Running as a service](#running-as-a-service) for details.

A Discord and Telegram bot that reports how much of your Claude subscription
quota is left, and warns when you are burning it faster than the week can
absorb.

Runs on a host that does **not** need Claude Code installed.

```
🟢 5-hour session
███████░░░░░░░░░░░░░   34.0%   resets in 4h 5m

🟢 Weekly (all models)
███████░░░░░░░░░░░░░   33.0%   resets in 5d 14h

🔴 Overspending badly — 1.66x the even pace
Spent 33% of the weekly cap with 20% of the week elapsed (+13 points).
At this rate you hit 100% on Sun 06 Sep 15:13 UTC, 2d 18h before the window resets.
```

## How it works

Subscription limits have no public API. The numbers Claude Code's `/usage`
shows come from `GET https://api.anthropic.com/api/oauth/usage`, authenticated
with an OAuth token. This bot performs its own login to that same endpoint and
formats the result.

The pace tracker needs no stored history: each window reports `resets_at`, and
subtracting the window length gives its start, so the fraction of the window
elapsed can be compared directly against the fraction of quota spent.

## Requirements

- Python 3.11+ (3.14 tested)
- `discord.py` and/or `python-telegram-bot` — install only what you enable

`python run.py setup` creates a virtualenv and installs these for you. To do it
by hand instead:

```bash
pip install -r requirements.txt
```

## Setup

### Quick start

```bash
python run.py setup
```

One interactive pass, in five steps: creates a virtualenv and installs the
dependencies, logs in, asks which platforms you want and for their tokens and
user IDs, writes `~/.claudelimitreader/config.toml`, reads it back and fetches
your usage once so a wrong answer surfaces now rather than at `run` time, then
offers to install the background service.

Run it with any Python 3.11+; if you are not already in a virtualenv it offers
to create `./venv`, installs `requirements.txt` there, and continues under that
interpreter — which is the one the service then records. That makes the whole
setup, on a fresh host:

```bash
python3 run.py setup
```

Bot tokens are typed hidden and the file is written `0600`; an
existing config is backed up to `config.toml.bak` (also `0600`) before being
replaced.

You still need the Discord app or the BotFather token in hand — setup prompts
for them, it cannot create them. See [Discord app setup](#discord-app-setup)
below for that part.

The manual route below does the same thing step by step, and is the only option
on a host with no interactive terminal.

### 1. Log in

```bash
python run.py login
```

Prints an authorization URL. Open it in any browser — it does not have to be on
the bot host — approve, and paste the code back. Tokens are written to
`~/.claudelimitreader/tokens.json` with owner-only permissions.

This is a full authorization-code + PKCE login, so the bot gets its **own**
refresh-token lineage. That matters: refresh tokens rotate on every use, so a
copied credential would fight with your Claude Code install and log one of them
out every time the other refreshed. A separate login avoids that entirely.

<details>
<summary>Fallback: adopt local Claude Code credentials</summary>

```bash
python run.py login --import
```

Reads `~/.claude/.credentials.json`. Only for a host that already has Claude
Code. **This takes over that token's lineage** — the local CLI will need
`claude /login` again after the bot's next refresh. Prefer the browser login.

</details>

### 2. Configure

```bash
mkdir -p ~/.claudelimitreader
cp config.example.toml ~/.claudelimitreader/config.toml
```

Fill in the platforms you want:

- **Discord** — see [Discord app setup](#discord-app-setup) below.
- **Telegram** — talk to [@BotFather](https://t.me/BotFather), `/newbot`, copy
  the token. Get your numeric user ID from [@userinfobot](https://t.me/userinfobot).

Tokens can also come from `CLR_DISCORD_TOKEN` / `CLR_TELEGRAM_TOKEN`, which
override the file — preferable if you would rather not keep secrets on disk.

`allowed_user_ids` must be non-empty for any enabled platform; the bot refuses
to start otherwise rather than exposing your account usage to everyone.

### Discord app setup

The bot needs **no permissions and no role**. Slash command replies are
authorized by the interaction token, not by channel permissions, and every
reply is ephemeral. It never reads messages, posts unprompted, or touches
members or channels.

**1. Create the application.** Go to
<https://discord.com/developers/applications> → **New Application**, name it,
and accept the terms.

**2. Copy the Application ID.** On the **General Information** tab. You need it
for the invite link in step 6.

**3. Create the bot user.** **Bot** tab → **Reset Token** → copy the token into
`discord.token` in your config. It is shown only once.

**4. Lock the app down.** Still on the **Bot** tab:

- Turn **Public Bot** *off* — otherwise anyone with the Application ID can
  invite your bot to their own server.
- Leave **Presence**, **Server Members**, and **Message Content** intents *off*.
  All three are privileged and none are used; the bot runs on
  `Intents.default()`.

**5. Skip the Installation tab.** Its "Install Link" is for user installs. For
a server you use the URL below instead.

**6. Invite it with zero permissions.** Replace `YOUR_APP_ID` and open:

```
https://discord.com/api/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot%20applications.commands&permissions=0
```

Pick your server and authorize. Discord creates a managed role named after the
bot automatically — **grant it nothing**. An empty role is correct; the bot does
not need Send Messages, Embed Links, or Read Message History, because ephemeral
interaction responses bypass channel permissions.

**7. Get the IDs.** Discord Settings → Advanced → enable **Developer Mode**.
Then right-click your own name → **Copy User ID** (into `allowed_user_ids`) and
right-click the server icon → **Copy Server ID** (into `guild_id`).

**8. Set the config.**

```toml
[discord]
enabled = true
token = "your-bot-token"
allowed_user_ids = [your-user-id]
guild_id = your-server-id
```

Setting `guild_id` registers `/usage` in that one server, which takes effect as
soon as the bot starts. Leaving it out registers globally, which can take up to
an hour to appear.

**9. Start it.** `python run.py run`. The bot shows as online and `/usage` is
available immediately.

Anyone in the server can *see* the command, but only IDs in `allowed_user_ids`
get a reply; everyone else gets "Not authorized". Replies are ephemeral, so
nobody sees your numbers either way. To hide the command itself, use
**Server Settings → Integrations → ClaudeLimitReader → Manage** and restrict
`/usage` to a role or a single channel.

<details>
<summary>Alternative: install to your account instead of a server</summary>

No server, no role, no invite. **Installation** tab → tick **User Install**,
set User Install scopes to `applications.commands`, then open the Install Link
and choose **"Add to my apps"**. Leave `guild_id` unset. You get `/usage` in any
DM or server you are in, and nobody else ever sees the command exists.

</details>

### 3. Run

```bash
python run.py run
```

Then `/usage` in Discord (replies ephemerally, only you see it) or `/usage` in
Telegram. Unauthorized Discord users get "Not authorized"; unauthorized
Telegram users get silence, so the bot stays invisible to strangers.

### Discord commands

| Command | Purpose |
|---|---|
| `/usage` | The quota report, as an ephemeral embed |
| `/status` | Credential and bot health — the CLI's `status`, from Discord |
| `/authenticate` | Re-authorize the bot without touching the host |

`/status` shows which account is authorized, how long the access token is good
for (and that an expired one renews itself), the enabled platforms and how many
users each allows, the bot's uptime and gateway latency, and where the token and
config files live. It never prints the tokens themselves. When nothing is
authorized it says so and points at `/authenticate`.

`/authenticate` runs the same authorization-code + PKCE login as
`python run.py login`, driven from Discord: it replies with the approval link,
you press **Enter code** and paste the code into a modal, and the bot exchanges
it and rewrites `tokens.json`. Useful when the refresh token lapses on a host
you cannot easily get a shell on — `/usage` picks up the new token straight
away, with no restart.

The attempt expires after 10 minutes, only `allowed_user_ids` may start one,
only the user who started it can submit the code, and every reply is ephemeral.
The code itself goes through a modal and is never logged.

## Logs

Every command also writes its log records to `logs/` in the project folder,
next to `run.py`: one file per calendar day named `YYYY-MM-DD.log` and starting
with a `# YYYY-MM-DD` header line. A long-running `python run.py run` rolls over to the next day's
file by itself at local midnight. Console output is unchanged; `-v` raises both
to debug level. If the directory cannot be written, the command warns once and
carries on without file logging.

While `python run.py run` is up, it also writes an `All good — no errors in the
last 30 minutes.` line every 30 minutes that passed without an error, so a
quiet log still shows the bot was alive. A window with an error gets no such line.

## Commands

| Command | Purpose |
|---|---|
| `python run.py setup` | Interactive first-run setup (login + config + check) |
| `python run.py login` | Authorize this host (add `--import` for the fallback) |
| `python run.py status` | Show token state and config path |
| `python run.py usage` | Print the report to stdout — useful for testing |
| `python run.py run` | Start the enabled bots |
| `python run.py service install` | Install the background service (see below) |

## Running as a service

`setup` offers this as its last step, or install it on its own at any time:

```bash
python run.py service install
```

`install` first checks what `run` would check (a platform enabled, its token
and user IDs, a login, and the platform's library for this Python) and refuses
rather than install a service that would exit at once.

| Command | What it does |
|---|---|
| `python run.py service install` | Install and start the service |
| `python run.py service status` | Is it installed, is it running, where are the logs |
| `python run.py service uninstall` | Stop it and remove it |
| `python run.py service show` | Print the unit or launcher without installing anything |

### Linux (systemd)

Installs `claude-limit-reader-bot.service`. By default it is a **user** unit in
`~/.config/systemd/user/`, which needs no root:

```bash
python run.py service install
systemctl --user status claude-limit-reader-bot.service
journalctl --user -u claude-limit-reader-bot.service -f
```

A user unit stops when your last session ends. To keep it running while you are
logged out:

```bash
sudo loginctl enable-linger $USER
```

For a system unit in `/etc/systemd/system/` that starts at boot instead, say yes
when setup asks to install system-wide — it re-runs the install under `sudo` for
you. Run setup itself without `sudo`: under `sudo` its login and config belong
to root, so it refuses to install a unit for `$SUDO_USER`. Or do it directly:

```bash
sudo -E venv/bin/python run.py service install --system
```

`-E` and the explicit interpreter matter: `sudo` otherwise resets the
environment and records a Python without the dependencies. The unit gets
`User=$SUDO_USER`, so it still runs as the user who owns the tokens.

Switching an already-installed user unit over:

```bash
python run.py service uninstall
```

```bash
sudo -E venv/bin/python run.py service install --system
```

A system unit is managed without `--user`:

```bash
systemctl status claude-limit-reader-bot.service
```

The unit restarts on failure and is confined: `NoNewPrivileges`, `PrivateTmp`,
`ProtectSystem=strict`, with `ReadWritePaths` limited to the app directory
(`~/.claudelimitreader`, or `CLR_HOME`) and the project's `logs/` folder.
`python run.py service show` prints it if you would rather install it yourself.

A service does not inherit your shell's environment, so `install` carries over
what the bot reads from it: `CLR_HOME` (always; under `sudo` it defaults to
`$SUDO_USER`'s home, not root's) and `CLR_CONFIG` (or a `-c` path) go into the
unit as `Environment=` lines, and `CLR_DISCORD_TOKEN` / `CLR_TELEGRAM_TOKEN` go
into `service.env` in the app directory, written `0600` and referenced with
`EnvironmentFile=` — the unit file itself is world-readable. Re-run
`service install` after changing any of them; it restarts the running bot.

### Windows

There is no systemd, so `install` uses the nearest equivalent under the same
name, `claude-limit-reader-bot`:

- **Elevated** — a Task Scheduler task triggered at logon. Creating a
  logon-triggered task requires Administrator, so run it from an elevated
  terminal to get this. The task runs a `claude-limit-reader-bot.cmd` launcher
  kept in the app directory, so a console window flashes briefly at logon.
- **Not elevated** — a `claude-limit-reader-bot.cmd` launcher in your Startup
  folder, which needs no privileges and starts at the same point.

The two are alternatives. An elevated install removes a Startup-folder launcher
left by an earlier unelevated one; an unelevated install refuses while the
scheduled task exists, since only an elevated terminal can remove it.

Either way the bot runs under `pythonw.exe`, so it has no console window, and
`install` also starts it immediately rather than waiting for the next logon.
The launcher sets `CLR_HOME`, `CLR_CONFIG` (or the `-c` path) and any
`CLR_*_TOKEN` from the installing shell, since a logon task does not inherit
it; re-run `service install` after changing them, which stops the running bot
and starts a new one. With no console, a bot that
fails to start says why in the day's file under `logs/`.

Windows has no equivalent of the unit's `Restart=on-failure`: if the bot exits,
it stays down until the next logon. A network that is not up yet at logon does
not make it exit: on both platforms the bot keeps retrying its first connection
(backing off to once every 5 minutes) and logs each failed attempt. A rejected
bot token still stops it.

## Configuration reference

| Key | Default | Meaning |
|---|---|---|
| `discord.guild_id` | unset | Register `/usage` in one server (instant) instead of globally (~1h) |
| `pace.tolerance_points` | `5.0` | Percentage points of overspend tolerated before flagging |
| `pace.critical_ratio` | `1.5` | Multiple of even pace at which the verdict escalates |

Verdicts: `under`, `on_pace`, `ahead`, `critical`, plus `unknown` in the first
~2% of a window (too little elapsed time to judge) and `exhausted` at 100%.

## Tests

```bash
python -m unittest discover -s tests
```

Covers the parser (including the fallback path and unknown window types), the
pace math against a captured real response, and the config the setup wizard
writes — rendered, then read back through the real loader.

## Notes and caveats

- **The usage endpoint is undocumented.** It can change without warning. The
  parser prefers the normalized `limits[]` array and falls back to the named
  window objects, rendering whatever windows are present — so a new window type
  appears automatically and a removed one does not crash it.
- **Windows shown depend on your plan.** On Pro you get the 5-hour session and
  the weekly cap; `seven_day_opus` and friends are `null` and are skipped. On
  Max they appear on their own.
- **The token can spend, not just read.** Its `user:inference` scope means
  anyone holding `tokens.json` can consume your quota. It is stored `0600`, but
  treat the host as you would treat a machine holding a password.
- **Re-login roughly monthly.** Refresh tokens last ~25–30 days. When one
  lapses, `/usage` reports the auth error. Fix it with `/authenticate` in
  Discord, or `python run.py login` on the host.

## License

MIT — see [LICENSE](LICENSE).
