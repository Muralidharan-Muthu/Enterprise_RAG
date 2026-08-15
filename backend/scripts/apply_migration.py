"""Apply a .sql migration file through the configured psycopg2 connection."""
import sys
from pathlib import Path

from app.db.connection import get_db


def apply(sql_path: str) -> None:
    sql = Path(sql_path).read_text(encoding="utf-8")
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
    print(f"Applied migration: {sql_path}")


if __name__ == "__main__":
    apply(sys.argv[1])
