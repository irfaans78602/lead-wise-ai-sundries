"""
resolve_gaps.py
Marks all 'notified' and 'unanswered' questions as 'resolved'.
Run this AFTER you have updated your JSON files and re-run ingest_rag.py.
"""
from db import get_conn
from env import load_env

load_env()

def main():
    conn = get_conn()
    cur = conn.cursor()

    # Update all unresolved statuses to 'resolved'
    cur.execute("""
        UPDATE ask_ai_log
        SET status = 'resolved'
        WHERE status IN ('unanswered', 'no_context', 'notified')
    """)
    
    updated_count = cur.rowcount
    conn.commit()
    
    cur.close()
    conn.close()
    
    print(f"Success! Marked {updated_count} gap logs as 'resolved'.")

if __name__ == "__main__":
    main()