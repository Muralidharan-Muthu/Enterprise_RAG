import logging
from app.db.connection import get_db
from app.services.embedding_service import embed_passages

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def backfill():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, row_text FROM multi_store_rag_working.table_row_store WHERE embedding IS NULL AND row_text IS NOT NULL")
            rows = cur.fetchall()
            print(f"Found {len(rows)} rows to backfill in table_row_store")
            if not rows:
                return
            ids = [r[0] for r in rows]
            texts = [r[1] for r in rows]
            embeddings = embed_passages(texts)
            for row_id, emb in zip(ids, embeddings):
                emb_str = "[" + ",".join(f"{x:.8f}" for x in emb) + "]"
                cur.execute(
                    "UPDATE multi_store_rag_working.table_row_store SET embedding = %s::vector WHERE id = %s",
                    (emb_str, row_id)
                )
            conn.commit()
            print(f"Successfully backfilled {len(rows)} embeddings into table_row_store!")

if __name__ == "__main__":
    backfill()
