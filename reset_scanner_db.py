"""
reset_scanner_db.py – Počisti scanner.db po downloadu podatkov.
Poženi na VPS-u: python3 reset_scanner_db.py
"""
import sqlite3
import os

DB_PATH = "scanner.db"

if not os.path.exists(DB_PATH):
    print(f"DB ne obstaja: {DB_PATH}")
    exit(0)

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

cur.execute("SELECT COUNT(*) FROM summary")
summary_count = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM book")
book_count = cur.fetchone()[0]

print(f"Brišem: {summary_count} summary vrstic, {book_count} book vrstic...")

conn.execute("DELETE FROM summary")
conn.execute("DELETE FROM book")
conn.execute("VACUUM")
conn.commit()
conn.close()

print("DB resetiran. Scanner bo nadaljeval z novimi podatki.")
