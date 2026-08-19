import os
import sys
from pathlib import Path

# Ensure backend root is on python path
backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

import psycopg2
from app.config import settings

def truncate_all_tables():
    print("=" * 60)
    print(" SUPABASE DATABASE TRUNCATION")
    print("=" * 60)
    print(f"Target Host:   {settings.SUPABASE_HOST}")
    print(f"Target DB:     {settings.SUPABASE_DB}")
    print(f"Target Schema: {settings.SUPABASE_SCHEMA}")
    print("=" * 60)

    try:
        conn = psycopg2.connect(
            host=settings.SUPABASE_HOST,
            port=settings.SUPABASE_PORT,
            dbname=settings.SUPABASE_DB,
            user=settings.SUPABASE_USER,
            password=settings.SUPABASE_PASSWORD,
            sslmode=settings.DB_SSLMODE,
        )
        conn.autocommit = True
        cur = conn.cursor()

        schema = settings.SUPABASE_SCHEMA or "multi_store_rag_working"

        tables = [
            "chat_messages",
            "chat_sessions",
            "table_cell_store",
            "table_row_store",
            "table_chunk_store",
            "table_store",
            "clause_store",
            "document_store",
            "image_store",
            "vector_store",
            "parse_staging",
            "ingestion_jobs",
            "pipeline_runs",
            "document_registry",
        ]

        cur.execute(
            """
            SELECT table_name FROM information_schema.tables 
            WHERE table_schema = %s AND table_type = 'BASE TABLE';
            """,
            (schema,)
        )
        existing_tables = {row[0] for row in cur.fetchall()}
        valid_tables = [t for t in tables if t in existing_tables]

        if not valid_tables:
            print(f"No matching tables found in schema '{schema}'.")
            return

        print(f"Found {len(valid_tables)} tables to truncate:")
        for t in valid_tables:
            print(f"  - {schema}.{t}")

        table_list_sql = ", ".join(f'\"{schema}\".\"{t}\"' for t in valid_tables)
        truncate_sql = f"TRUNCATE TABLE {table_list_sql} RESTART IDENTITY CASCADE;"
        
        print("\nExecuting TRUNCATE CASCADE...")
        cur.execute(truncate_sql)
        print("\n Successfully deleted all records from all Supabase tables!")

        cur.close()
        conn.close()

    except Exception as e:
        print(f"\n Error truncating Supabase database: {e}")
        sys.exit(1)

if __name__ == "__main__":
    truncate_all_tables()
