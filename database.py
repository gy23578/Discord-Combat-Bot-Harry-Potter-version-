"""Compatible JSON profiles, transactional writes, and online SQLite backups."""
import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

APPLICATION_DIR = Path(__file__).resolve().parent
DATABASE_FILE = str(APPLICATION_DIR / "players.db")
BACKUP_DIR = APPLICATION_DIR / "backups"
logger = logging.getLogger(__name__)
backup_lock = threading.Lock()


@contextmanager
def connection():
    db = sqlite3.connect(DATABASE_FILE, timeout=5)
    try:
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA synchronous=FULL")
        with db:
            yield db
    except Exception:
        logger.exception("SQLite operation failed")
        raise
    finally:
        db.close()


def init_database():
    with connection() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS players (user_id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
    logger.info("Database initialized: %s", DATABASE_FILE)


def save_players(profiles):
    rows = []
    for player in profiles:
        data = dict(player)
        data["learned_spells"] = sorted(player.get("learned_spells", []))
        rows.append((player["user_id"], json.dumps(data)))
    try:
        with connection() as db:
            db.executemany("INSERT INTO players VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET data=excluded.data", rows)
    except sqlite3.Error:
        for player in profiles:
            restore_profile_after_failure(player)
        raise


def save_player(user_id, player):
    if player.get("user_id") != user_id:
        raise ValueError("Profile ID does not match save target")
    save_players([player])


def restore_profile_after_failure(player):
    try:
        saved = load_player(player["user_id"])
        if saved is not None:
            player.clear()
            player.update(saved)
    except Exception:
        logger.exception("Unable to reload profile after failed write")


def load_player(user_id):
    with connection() as db:
        row = db.execute("SELECT data FROM players WHERE user_id=?", (user_id,)).fetchone()
    if row is None:
        return None
    try:
        player = json.loads(row[0])
        player.setdefault("user_id", user_id)
        player["learned_spells"] = set(player.get("learned_spells", []))
        return player
    except (ValueError, TypeError, AttributeError):
        logger.exception("Invalid saved profile for player %s; refusing to overwrite", user_id)
        raise


def save_runtime(state):
    """Legacy compatibility API; live sessions are no longer restored by the bot."""
    with connection() as db:
        db.execute("CREATE TABLE IF NOT EXISTS runtime (id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
        db.execute("INSERT INTO runtime VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", (json.dumps(state),))


def load_runtime():
    with connection() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='runtime'").fetchone():
            return None
        row = db.execute("SELECT data FROM runtime WHERE id=1").fetchone()
    return json.loads(row[0]) if row else None


def clear_runtime():
    with connection() as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='runtime'").fetchone():
            db.execute("DELETE FROM runtime")


def database_health():
    with connection() as db:
        healthy = db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        count = db.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    return healthy, count


def backup_database():
    with backup_lock:
        return _backup_database()


def _backup_database():
    """Online consistent snapshot; retain ten completed backups."""
    directory = Path(BACKUP_DIR)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = datetime.now(timezone.utc).strftime("players-%Y%m%dT%H%M%S%fZ.sqlite3")
    target = directory / name
    temporary = target.with_suffix(".tmp")
    try:
        with connection() as source:
            destination = sqlite3.connect(str(temporary))
            try:
                source.backup(destination)
            finally:
                destination.close()
        temporary.chmod(0o600)
        temporary.replace(target)
        for old in sorted(directory.glob("players-*.sqlite3"), reverse=True)[10:]:
            old.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()
    logger.info("Database backup completed: %s", target.name)
    return target
