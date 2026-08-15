import os
import sys

# Add backend directory to path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from app.db.connection import get_db

print("Running migration to add is_pinned to chat_messages...")

try:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO multi_store_rag_working, public;")
            cur.execute("ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS is_pinned BOOLEAN DEFAULT FALSE;")
            conn.commit()
    print("Migration successful.")
except Exception as e:
    print(f"Error during migration: {e}")
