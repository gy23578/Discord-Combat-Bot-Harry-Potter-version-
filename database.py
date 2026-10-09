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
        db.execute("CREATE TABLE IF NOT EXISTS local_player_points (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, points INTEGER NOT NULL DEFAULT 0 CHECK(points >= 0), PRIMARY KEY(guild_id, user_id))")
        db.execute("CREATE INDEX IF NOT EXISTS local_points_order ON local_player_points(guild_id, points DESC, user_id ASC)")
        db.execute("CREATE INDEX IF NOT EXISTS local_points_user ON local_player_points(user_id, points)")
        db.execute("CREATE TABLE IF NOT EXISTS local_house_points (guild_id INTEGER NOT NULL, house TEXT NOT NULL CHECK(house IN ('gryffindor','slytherin','ravenclaw','hufflepuff')), points INTEGER NOT NULL DEFAULT 0 CHECK(points >= 0), PRIMARY KEY(guild_id, house))")
        db.execute("CREATE TABLE IF NOT EXISTS ranked_duel_results (session_id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, winner_id INTEGER NOT NULL, loser_id INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS guild_settings (guild_id INTEGER PRIMARY KEY, language TEXT NOT NULL DEFAULT 'en' CHECK(language IN ('en', 'fr')))")
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


VALID_HOUSES = ("gryffindor", "slytherin", "ravenclaw", "hufflepuff")


def save_ranked_duel_result(session_id, guild_id, winner, loser):
    """Atomically save ordinary rewards, local points, House points, and a receipt."""
    if not session_id or not guild_id or winner["user_id"] == loser["user_id"]:
        raise ValueError("A ranked result requires a guild and two different players")
    profiles = [winner, loser]
    try:
        with connection() as db:
            db.execute("BEGIN IMMEDIATE")
            inserted = db.execute(
                "INSERT INTO ranked_duel_results VALUES (?, ?, ?, ?) ON CONFLICT(session_id) DO NOTHING",
                (session_id, guild_id, winner["user_id"], loser["user_id"]),
            ).rowcount
            if not inserted:
                for player in profiles:
                    restore_profile_after_failure(player)
                return None
            stored = db.execute("SELECT data FROM players WHERE user_id=?", (winner["user_id"],)).fetchone()
            house = json.loads(stored[0]).get("house") if stored else winner.get("house")
            changes = {}
            for player, delta in ((winner, 10), (loser, -3)):
                uid = player["user_id"]
                row = db.execute("SELECT points FROM local_player_points WHERE guild_id=? AND user_id=?", (guild_id, uid)).fetchone()
                before = row[0] if row else 0
                after = max(0, before + delta)
                db.execute("INSERT INTO local_player_points VALUES (?, ?, ?) ON CONFLICT(guild_id,user_id) DO UPDATE SET points=excluded.points", (guild_id, uid, after))
                changes[uid] = (before, after)
                data = dict(player, learned_spells=sorted(player.get("learned_spells", [])))
                db.execute("INSERT INTO players VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET data=excluded.data", (uid, json.dumps(data)))
            if house in VALID_HOUSES:
                db.execute("INSERT INTO local_house_points VALUES (?, ?, 10) ON CONFLICT(guild_id,house) DO UPDATE SET points=points+10", (guild_id, house))
            else:
                logger.warning("Ranked winner %s has no valid stored House", winner["user_id"])
                house = None
        logger.info("Ranking result saved: session=%s guild=%s winner=%s loser=%s", session_id, guild_id, winner["user_id"], loser["user_id"])
        return {"players": changes, "house": house}
    except (sqlite3.Error, ValueError, TypeError):
        for player in profiles:
            restore_profile_after_failure(player)
        raise


def player_leaderboard(user_id, guild_id=None):
    """Return only ten profiles; aggregation and deterministic ranks stay in SQL."""
    if guild_id is None:
        scores = "SELECT user_id, SUM(points) AS points FROM local_player_points GROUP BY user_id"
        parameters = ()
    else:
        scores = "SELECT user_id, points FROM local_player_points WHERE guild_id=?"
        parameters = (guild_id,)
    prefix = "WITH scores AS (" + scores + "), ranked AS (SELECT user_id, points, ROW_NUMBER() OVER (ORDER BY points DESC, user_id ASC) AS position FROM scores) "
    with connection() as db:
        rows = db.execute(prefix + "SELECT r.user_id, r.points, r.position, p.data FROM ranked r LEFT JOIN players p ON p.user_id=r.user_id ORDER BY r.position LIMIT 10", parameters).fetchall()
        personal = db.execute(prefix + "SELECT position, points FROM ranked WHERE user_id=?", parameters + (user_id,)).fetchone()
    entries = []
    for uid, points, position, data in rows:
        try:
            name = json.loads(data).get("name") if data else None
        except (ValueError, TypeError, AttributeError):
            name = None
        entries.append({"user_id": uid, "points": points, "rank": position, "name": name})
    return entries, personal


def house_leaderboard(guild_id):
    with connection() as db:
        points = dict(db.execute("SELECT house, points FROM local_house_points WHERE guild_id=?", (guild_id,)).fetchall())
    return sorted(((house, points.get(house, 0)) for house in VALID_HOUSES), key=lambda item: (-item[1], item[0]))


def get_guild_language(guild_id):
    with connection() as db:
        row = db.execute("SELECT language FROM guild_settings WHERE guild_id=?", (guild_id,)).fetchone()
    return row[0] if row else "en"


def set_guild_language(guild_id, language):
    from translations import SUPPORTED_LANGUAGES
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError("Unsupported guild language")
    with connection() as db:
        db.execute("INSERT INTO guild_settings(guild_id, language) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET language=excluded.language", (guild_id, language))
