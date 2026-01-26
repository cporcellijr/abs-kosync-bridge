import sqlite3

def check_status():
    conn = sqlite3.connect('/data/database.db')
    cursor = conn.cursor()
    
    titles = [
        ("Little Heaven", "Little Heaven"),
        ("The Book That Wouldn't Burn", "Wouldn't Burn"),
        ("Eyes of the Void", "Eyes of the Void"),
        ("Fatale: Erotic Horror", "Fatale")
    ]
    
    print("BOOK|SG_MATCH|MATCH_TYPE|PROGRESS")
    for display_name, search_pattern in titles:
        cursor.execute('''
            SELECT 
                s.storygraph_title, 
                s.matched_by,
                (SELECT MAX(percentage) FROM states WHERE abs_id = b.abs_id)
            FROM books b
            LEFT JOIN storygraph_details s ON b.abs_id = s.abs_id
            WHERE b.abs_title LIKE ?
        ''', ('%' + search_pattern + '%',))
        
        row = cursor.fetchone()
        if row:
            sg_title, matched_by, pct = row
            pct_val = (pct * 100) if pct is not None else 0
            print(f"{display_name}|{sg_title}|{matched_by}|{pct_val:.1f}%")
        else:
            print(f"{display_name}|NOT_FOUND|None|0%")

if __name__ == "__main__":
    check_status()
