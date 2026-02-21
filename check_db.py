import sqlite3

def check_db():
    conn = sqlite3.connect('data/kosync.db')
    c = conn.cursor()
    c.execute("SELECT status, transcript_file FROM books WHERE abs_id='2a9b2633-158e-4106-9fa2-019dfb9abb00'")
    print("Book:", c.fetchone())
    c.execute("SELECT COUNT(*) FROM book_alignments WHERE abs_id='2a9b2633-158e-4106-9fa2-019dfb9abb00'")
    print("Alignments:", c.fetchone())
    c.execute("SELECT status, last_error FROM background_jobs WHERE abs_id='2a9b2633-158e-4106-9fa2-019dfb9abb00'")
    print("Job:", c.fetchone())

if __name__ == '__main__':
    check_db()
