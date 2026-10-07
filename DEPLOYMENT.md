# Private beta deployment

The project is prepared for hosting, but has not been deployed or connected during verification.

## Local setup

Use Python 3.9+ (prefer a supported Python release on the host). From the project directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
.venv/bin/python bot.py
```

Fill `DISCORD_TOKEN` and `BOT_OWNER_ID` in `.env`; `BOT_OWNER_ID` is your positive numeric Discord user ID. `LOG_LEVEL` is optional and defaults to INFO. Real environment variables take precedence over `.env`. Both required settings are checked before database/network startup. Keep the token private and regenerate it if exposed. Run the bot as an unprivileged account with write access to its application directory. No Administrator permission is needed.

Runtime dependencies are only discord.py and python-dotenv; SQLite and operational helpers use the standard library. Requirements constrain major versions; install in a fresh virtual environment and retain the resolved dependency versions for your beta host. Tests have been run using the existing project environment, not a freshly downloaded dependency installation.

## Discord setup

Invite with the **bot** OAuth scope. Minimum channel permissions for the present text-only commands: **View Channels** and **Send Messages**. Enable the privileged **Message Content Intent** in the Developer Portal; prefix commands require it. Default nonprivileged guild/message intents are enabled. Read Message History is reasonable for support but the bot does not fetch message history. Embed Links, Attach Files, Manage Messages, application-command scope, and Administrator are not required by the current code.

The privileged Members intent is not enabled. Automatic cleanup on member departure only runs when Discord delivers the event; offline status and client disconnects cannot reliably end a session. Use `!canceltraining` voluntarily. Normal duels have the five-minute inactivity fallback, measured with a monotonic clock so host clock adjustments cannot change the deadline. No inactivity XP penalty applies to training.

## SQLite and existing data

Use the existing `players.db` in the application directory. The path is resolved relative to `database.py`, never the launch working directory. Do not initialize a fresh application folder and forget to transfer your existing database. Stop the old process before moving data. Keep SQLite on a local persistent disk, not an ephemeral container layer or network filesystem. The service account must be able to write the database and adjacent WAL/SHM files.

The active database is deliberately **not ignored** in `.gitignore`. Keep it local on the host; do not commit private player records to a public repository. Git is not a database backup system. Transfer a verified SQLite backup for deployment/restoration, or copy the closed database after a clean shutdown. Do not blindly copy a running WAL database.

## Backups and restore

Startup (before clearing legacy live snapshots) and every 24 hours create a consistent snapshot with SQLite's online backup API in `backups/`. Keep the latest **10** completed timestamped `players-*.sqlite3` files. Backup creation runs off the Discord event loop for scheduled/manual backups, and is serialized. `!backupdb` creates one on demand, owner only. Startup backup failure prevents connecting; a later backup failure is logged while the bot continues.

Backups are private files (0600) in a dedicated directory (0700 when created). Local backups protect against accidental data damage, not host/disk loss. Copy periodic backups to a separate secure machine/storage location manually. Monitor free disk space.

To restore: stop the service, retain the damaged DB for investigation, verify the selected backup using `PRAGMA integrity_check`, replace `players.db` with that backup, and remove stale `players.db-wal` / `players.db-shm` **only while all bot/SQLite processes are stopped**. Start and inspect `!botstatus`. Live sessions are never resumed from a backup.

## Lifecycle and diagnostics

Console and `logs/bot.log` receive startup, ready/database, combat, training, command/task error, and backup events. The log rotates at 2 MB with three older files. Configured token strings are redacted, including exception formatting. Logs include operational paths/player IDs; restrict access and avoid DEBUG in normal hosting.

Owner commands:

- `!botstatus`: uptime, loaded/saved profiles, normal/training duel counts, teaching requests, trials, tracked combat tasks/timers, database path and health. It can be used in DMs or another channel while dueling.
- `!backupdb`: safe snapshot confirmation without revealing backup paths.

On restart, all live combat, statuses, requests, and memory-only questions expire without penalties or completion rewards. Permanent progression and completed practical objectives persist. Training HP is saved as pre-session HP. Graceful SIGTERM/SIGINT restores combat HP and cancels tasks. After a hard kill, saved normal-duel HP may still appear in profiles, but the next duel heals normally. Gateway reconnects keep current sessions and timers.

## Optional Linux systemd hosting

`deploy/harry-potter-bot.service` is a template, not an installed service. Create the `harrybot` user/group, place the project and virtual environment at `/opt/harry-potter-bot`, configure a private `.env`, transfer existing data, and give the account directory ownership. Adjust all paths/user names if using another location. After reviewing the unit, an administrator can install and enable it with systemd. It restarts on failure with a ten-second delay, rate-limits repeated startup failures, and grants writes only to the application directory. `journalctl -u harry-potter-bot` supplies service logs. This preparation has not run any installation or enable command.

## Remaining beta limits / operator checks

Run only one bot process/database copy. SQLite calls are synchronous with a five-second busy bound; they fit a small private beta, not heavy traffic. Completed combat actions, individual XP changes, and result transactions are not one global transaction across Discord and SQLite. A hard crash between separate action writes can leave partial action progression; completed duel reward pairs and inactivity penalties are atomic. SQLite cannot protect against full disks, faulty hardware, or unsupported filesystem locking. Monitor logs/backups, host storage, and Discord outages; keep independent backups.

Temporary request entries expire lazily when inspected/used; they never resume after restart. Training has no automatic inactivity end. Future scaling needs stronger storage orchestration, not multiple instances of this bot.

Before opening beta, use a private Discord server to smoke-test actual mentions, channel permission failures, both teaching directions, reactions (including Impero), simultaneous duels, disconnect/reconnect, graceful restart, and a backup restore. Automated tests mock Discord and cannot verify host networking, real tokens, Developer Portal configuration, or gateway delivery. No live Discord or Linux service test was performed here.

## Verification

```sh
.venv/bin/python -B -m unittest discover -s tests -v
```

Tests use temporary SQLite databases, mocked Discord objects, and controlled timers. The production `players.db` is not modified by tests.
