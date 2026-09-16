from __future__ import annotations

"""Migrate account/community data from the old local SQLite DB to PostgreSQL.

Security notes:
- Never commit the SQLite DB or TARGET_DATABASE_URL to GitHub.
- Passwords are NOT decrypted. The existing PBKDF2 password_hash is copied as-is,
  so the same password keeps working after migration.
- Place catalogue data is intentionally not migrated; production TourAPI sync owns it.

Examples:
  python scripts/migrate_sqlite_accounts_to_postgres.py \
      --sqlite ./data/gyeongju_hanjeok.db --dry-run

  $env:TARGET_DATABASE_URL="postgresql+psycopg://..."
  python scripts/migrate_sqlite_accounts_to_postgres.py \
      --sqlite ./data/gyeongju_hanjeok.db --apply
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, MetaData, Table, create_engine, select
from sqlalchemy.exc import IntegrityError

# Dependency order. `places` is deliberately omitted: production data is synced separately.
TABLES = [
    "users",
    "user_consents",
    "user_public_profiles",
    "friendships",
    "friend_invites",
    "shared_routes",
    "shared_route_members",
    "shared_route_invites",
    "community_posts",
    "community_comments",
    "community_recommendations",
    "community_saved_courses",
    "journeys",
]

USER_FK_COLUMNS = {
    "user_id",
    "requester_user_id",
    "addressee_user_id",
    "inviter_user_id",
    "claimed_by_user_id",
    "owner_user_id",
    "added_by_user_id",
    "author_user_id",
}


def _sqlite_rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return []
    cur = conn.execute(f'SELECT * FROM "{table}"')
    columns = [item[0] for item in cur.description]
    return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def _parse_datetime(value: Any) -> Any:
    if value in (None, "") or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    return value


def _json_value(value: Any) -> Any:
    if value in (None, ""):
        return None if value is None else value
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _coerce_for_target(table: Table, row: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for col in table.columns:
        if col.name not in row:
            continue
        value = row[col.name]
        if isinstance(col.type, JSON):
            value = _json_value(value)
        elif isinstance(col.type, DateTime):
            value = _parse_datetime(value)
        elif isinstance(col.type, Boolean) and value is not None:
            value = bool(value)
        result[col.name] = value
    return result


def _pk_filter(table: Table, row: dict[str, Any]):
    clauses = []
    for col in table.primary_key.columns:
        clauses.append(col == row[col.name])
    if not clauses:
        raise RuntimeError(f"{table.name}: primary key not found")
    expr = clauses[0]
    for clause in clauses[1:]:
        expr = expr & clause
    return expr


def _upsert_row(conn, table: Table, row: dict[str, Any]) -> str:
    existing = conn.execute(select(table).where(_pk_filter(table, row))).mappings().first()
    if existing:
        conn.execute(table.update().where(_pk_filter(table, row)).values(**row))
        return "updated"
    conn.execute(table.insert().values(**row))
    return "inserted"


def _normalize_target_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://") and "+psycopg" not in url:
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", required=True, help="Path to old gyeongju_hanjeok.db")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    sqlite_path = Path(args.sqlite).expanduser().resolve()
    if not sqlite_path.exists():
        print(f"[ERROR] SQLite file not found: {sqlite_path}", file=sys.stderr)
        return 2

    src = sqlite3.connect(sqlite_path)
    try:
        counts = {table: len(_sqlite_rows(src, table)) for table in TABLES}
        print("[SOURCE]", sqlite_path)
        for table, count in counts.items():
            print(f"  {table:32s} {count}")

        if args.dry_run:
            print("[DRY-RUN] No PostgreSQL data was changed.")
            return 0

        target_url = os.getenv("TARGET_DATABASE_URL", "").strip()
        if not target_url:
            print("[ERROR] TARGET_DATABASE_URL is not set.", file=sys.stderr)
            return 2
        target_url = _normalize_target_url(target_url)

        engine = create_engine(target_url, pool_pre_ping=True)
        metadata = MetaData()
        metadata.reflect(bind=engine, only=TABLES)

        missing = [table for table in TABLES if table not in metadata.tables]
        if missing:
            print(f"[ERROR] Target PostgreSQL is missing tables: {missing}", file=sys.stderr)
            return 3

        user_map: dict[str, str] = {}
        stats: dict[str, dict[str, int]] = {
            t: {"inserted": 0, "updated": 0, "skipped": 0} for t in TABLES
        }

        with engine.begin() as dst:
            users = metadata.tables["users"]
            for source_row in _sqlite_rows(src, "users"):
                source_id = str(source_row["user_id"])
                email = str(source_row["email"]).strip().lower()
                existing_email = dst.execute(
                    select(users).where(users.c.email == email)
                ).mappings().first()

                if existing_email:
                    target_id = str(existing_email["user_id"])
                    user_map[source_id] = target_id
                    row = dict(source_row)
                    row["user_id"] = target_id
                    row = _coerce_for_target(users, row)
                    dst.execute(
                        users.update().where(users.c.user_id == target_id).values(**row)
                    )
                    stats["users"]["updated"] += 1
                else:
                    user_map[source_id] = source_id
                    row = _coerce_for_target(users, source_row)
                    action = _upsert_row(dst, users, row)
                    stats["users"][action] += 1

            for table_name in TABLES[1:]:
                table = metadata.tables[table_name]
                for source_row in _sqlite_rows(src, table_name):
                    row = dict(source_row)
                    for key in list(row):
                        if key in USER_FK_COLUMNS and row[key] is not None:
                            row[key] = user_map.get(str(row[key]), str(row[key]))

                    # Unique helper keys may embed user IDs. Rebuild the known ones after remap.
                    if table_name == "friendships":
                        a = str(row.get("requester_user_id") or "")
                        b = str(row.get("addressee_user_id") or "")
                        row["pair_key"] = "::".join(sorted([a, b]))
                    elif table_name == "shared_route_members":
                        route_id = str(row.get("shared_route_id") or "")
                        user_id = str(row.get("user_id") or "")
                        row["route_user_key"] = f"{route_id}::{user_id}"
                    elif table_name == "community_recommendations":
                        post_id = str(row.get("post_id") or "")
                        user_id = str(row.get("user_id") or "")
                        row["recommendation_key"] = f"{post_id}::{user_id}"
                    elif table_name == "community_saved_courses":
                        post_id = str(row.get("source_post_id") or "")
                        user_id = str(row.get("user_id") or "")
                        row["save_key"] = f"{user_id}::{post_id}"

                    row = _coerce_for_target(table, row)
                    try:
                        with dst.begin_nested():
                            action = _upsert_row(dst, table, row)
                        stats[table_name][action] += 1
                    except IntegrityError as exc:
                        stats[table_name]["skipped"] += 1
                        print(
                            f"[WARN] {table_name}: skipped one conflicting row ({exc.orig})"
                        )

        print("[DONE] Migration committed to PostgreSQL.")
        for table_name in TABLES:
            row = stats[table_name]
            if sum(row.values()):
                print(
                    f"  {table_name:32s} inserted={row['inserted']} "
                    f"updated={row['updated']} skipped={row['skipped']}"
                )
        print(
            "[LOGIN] Existing password_hash values were preserved, so the same passwords should work."
        )
        return 0
    finally:
        src.close()


if __name__ == "__main__":
    raise SystemExit(main())
