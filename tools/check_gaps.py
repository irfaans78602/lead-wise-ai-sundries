"""
check_gaps.py
Finds unanswered questions, emails them to the admin, and marks them as notified.
Run this via a cron job (e.g., GitHub Actions or Render Cron).
"""
import os
import json
import urllib.request
from db import get_conn
from env import load_env

load_env()

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
CONTACT_TO = os.environ.get("CONTACT_TO", "contact@lead-wise-ai.com")
CONTACT_FROM = os.environ.get("CONTACT_FROM", "LeadWise AI <noreply@lead-wise-ai.com>")
THRESHOLD = 10

def send_email(questions):
    if not RESEND_API_KEY:
        print("No RESEND_API_KEY found. Skipping email.")
        return False

    rows_html = ""
    for q in questions:
        rows_html += f"<tr><td style='padding: 8px; border-bottom: 1px solid #ddd;'>{q['question']}</td>"
        rows_html += f"<td style='padding: 8px; border-bottom: 1px solid #ddd;'>{q['times_asked']} times</td>"
        rows_html += f"<td style='padding: 8px; border-bottom: 1px solid #ddd;'>{q['status']}</td></tr>"

    html_body = f"""
    <h2>LeadWise AI Gap Analysis</h2>
    <p>We have found {len(questions)} questions that the AI could not answer. Please update the JSON files in the website repo to fix these.</p>
    <table style="border-collapse: collapse; width: 100%; text-align: left;">
        <thead>
            <tr>
                <th style="padding: 8px; border-bottom: 2px solid #333;">Question</th>
                <th style="padding: 8px; border-bottom: 2px solid #333;">Frequency</th>
                <th style="padding: 8px; border-bottom: 2px solid #333;">Type</th>
            </tr>
        </thead>
        <tbody>
            {rows_html}
        </tbody>
    </table>
    <p>Once you have added the answers to your JSON files and run <code>ingest_rag.py</code>, run <code>python tools/resolve_gaps.py</code> to clear this list.</p>
    """

    url = "https://api.resend.com/emails"
    data = json.dumps({
        "from": CONTACT_FROM,
        "to": [CONTACT_TO],
        "subject": f"LeadWise AI: {len(questions)} Unanswered Questions",
        "html": html_body
    }).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json"
    })

    try:
        with urllib.request.urlopen(req) as resp:
            print(f"Email sent successfully: {resp.status}")
            return True
    except Exception as e:
        print(f"Failed to send email: {e}")
        return False

def main():
    conn = get_conn()
    cur = conn.cursor()

    # 1. Get unique questions that are unanswered and haven't been notified yet
    cur.execute("""
        SELECT question, count(*) as times_asked, min(status) as status
        FROM ask_ai_log
        WHERE status IN ('unanswered', 'no_context')
        GROUP BY question
        ORDER BY times_asked DESC
        LIMIT %s
    """, (THRESHOLD,))

    questions = cur.fetchall()

    if len(questions) < THRESHOLD:
        print(f"Only {len(questions)} unresolved gaps found. Waiting for {THRESHOLD} before emailing.")
        return

    print(f"Found {len(questions)} unresolved gaps. Sending email...")
    if send_email(questions):
        # 2. Mark these specific questions as 'notified' so we don't email them again
        question_texts = tuple(q['question'] for q in questions)
        
        # Note: Using ANY(%s) for a tuple requires PostgreSQL array binding
        cur.execute("""
            UPDATE ask_ai_log
            SET status = 'notified'
            WHERE question = ANY(%s) AND status IN ('unanswered', 'no_context')
        """, (list(question_texts),))
        
        conn.commit()
        print("Marked questions as 'notified' in the database.")

    cur.close()
    conn.close()

if __name__ == "__main__":
    main()