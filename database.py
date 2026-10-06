import sqlite3
import json
from contextlib import closing
from pathlib import Path


DATABASE_FILE = str(Path(__file__).with_name("players.db"))


def init_database():
    connection = sqlite3.connect(DATABASE_FILE)
    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS players (
            user_id INTEGER PRIMARY KEY,
            data TEXT NOT NULL
        )
    """)

    connection.commit()
    connection.close()


def save_player(user_id, player):
    connection = sqlite3.connect(DATABASE_FILE)
    cursor = connection.cursor()

    # JSON ne peut pas sauvegarder un set directement
    player_copy = player.copy()
    player_copy["learned_spells"] = list(
        player["learned_spells"]
    )

    data = json.dumps(player_copy)

    cursor.execute("""
        INSERT INTO players (user_id, data)
        VALUES (?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET data = excluded.data
    """, (
        user_id,
        data
    ))

    connection.commit()
    connection.close()


def load_player(user_id):
    connection = sqlite3.connect(DATABASE_FILE)
    cursor = connection.cursor()

    cursor.execute(
        "SELECT data FROM players WHERE user_id = ?",
        (user_id,)
    )

    result = cursor.fetchone()

    connection.close()

    if result is None:
        return None

    player = json.loads(
        result[0]
    )

    # On reconvertit la liste en set
    player["learned_spells"] = set(
        player["learned_spells"]
    )

    return player

def save_runtime(state):
    """Store resumable sessions separately from permanent player progression."""
    with closing(sqlite3.connect(DATABASE_FILE)) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS runtime (id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
        connection.execute("INSERT INTO runtime VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", (json.dumps(state),))
        connection.commit()


def load_runtime():
    with closing(sqlite3.connect(DATABASE_FILE)) as connection:
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name='runtime'").fetchone()
        if not exists:
            return None
        row = connection.execute("SELECT data FROM runtime WHERE id=1").fetchone()
    return json.loads(row[0]) if row else None
