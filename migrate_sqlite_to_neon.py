"""Importe une ancienne base galaxy.db dans Neon sans écraser les lignes existantes.

Usage : DATABASE_URL='postgresql://...' python migrate_sqlite_to_neon.py /chemin/galaxy.db
Les images présentes dans static/uploads sont également copiées dans PostgreSQL.
"""
import mimetypes
import os
import sqlite3
import sys
from datetime import datetime

if not os.environ.get("DATABASE_URL"):
    raise SystemExit("Erreur : définissez DATABASE_URL avec la chaîne de connexion Neon.")

from app import app, db, Player, PlaySession, Advertisement, AdEvent
from sqlalchemy import text


def parse_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def rows(connection, table):
    names = {row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')}
    if not names:
        return []
    return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"').fetchall()]


def main():
    source_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "galaxy.db")
    if not os.path.isfile(source_path):
        raise SystemExit(f"Base SQLite introuvable : {source_path}")

    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row
    counts = {"players": 0, "sessions": 0, "ads": 0, "events": 0, "images": 0}

    with app.app_context():
        # Les joueurs doivent être importés avant les sessions et événements.
        for row in rows(source, "player"):
            player = db.session.get(Player, str(row["id"]))
            first_seen = parse_datetime(row.get("first_seen"))
            last_seen = parse_datetime(row.get("last_seen"))
            if player is None:
                player = Player(
                    id=str(row["id"]),
                    first_seen=first_seen,
                    last_seen=last_seen,
                    best_score=int(row.get("best_score") or 0),
                )
                db.session.add(player)
                counts["players"] += 1
            else:
                if first_seen and (not player.first_seen or first_seen < player.first_seen):
                    player.first_seen = first_seen
                if last_seen and (not player.last_seen or last_seen > player.last_seen):
                    player.last_seen = last_seen
                player.best_score = max(player.best_score or 0, int(row.get("best_score") or 0))
        db.session.flush()

        for row in rows(source, "play_session"):
            session_id = str(row["id"])
            if db.session.get(PlaySession, session_id):
                continue
            db.session.add(PlaySession(
                id=session_id,
                player_id=str(row["player_id"]),
                started_at=parse_datetime(row.get("started_at")),
                ended_at=parse_datetime(row.get("ended_at")),
            ))
            counts["sessions"] += 1
        db.session.flush()

        for row in rows(source, "advertisement"):
            ad_id = int(row["id"])
            if db.session.get(Advertisement, ad_id):
                continue
            image_path = str(row.get("image_path") or "")
            image_data = row.get("image_data")
            image_mime = row.get("image_mime")
            if not image_data and image_path:
                candidate = os.path.abspath(os.path.join(app.static_folder, image_path))
                static_root = os.path.abspath(app.static_folder) + os.sep
                if candidate.startswith(static_root) and os.path.isfile(candidate):
                    with open(candidate, "rb") as image_file:
                        image_data = image_file.read()
                    image_mime = image_mime or mimetypes.guess_type(candidate)[0]
            if image_data:
                image_path = image_path or f"database/import-{ad_id}"
                counts["images"] += 1
            db.session.add(Advertisement(
                id=ad_id,
                title=row["title"],
                target_url=row["target_url"],
                image_path=image_path or f"legacy/missing-{ad_id}",
                image_data=image_data,
                image_mime=image_mime,
                created_at=parse_datetime(row.get("created_at")),
                ends_at=parse_datetime(row.get("ends_at")),
                active=bool(row.get("active", True)),
            ))
            counts["ads"] += 1
        db.session.flush()

        for row in rows(source, "ad_event"):
            event_id = int(row["id"])
            if db.session.get(AdEvent, event_id):
                continue
            if db.session.get(Advertisement, int(row["ad_id"])) is None:
                continue
            db.session.add(AdEvent(
                id=event_id,
                ad_id=int(row["ad_id"]),
                player_id=str(row["player_id"]) if row.get("player_id") else None,
                kind=row["kind"],
                created_at=parse_datetime(row.get("created_at")),
            ))
            counts["events"] += 1

        db.session.commit()
        if db.engine.dialect.name == "postgresql":
            for table in ("advertisement", "ad_event"):
                db.session.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f"GREATEST(COALESCE((SELECT MAX(id) FROM {table}), 1), 1), true)"
                ))
            db.session.commit()

    source.close()
    print("Import terminé :")
    for key, value in counts.items():
        print(f"- {key} : {value}")
    print("Les lignes déjà présentes dans Neon ont été conservées.")


if __name__ == "__main__":
    main()
