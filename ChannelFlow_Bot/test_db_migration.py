import database.db as db
db.init_db()
print('Database initialized successfully')
from database.db import get_connection
conn = get_connection()
cur = conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'support_ai%'")
tables = [row[0] for row in cur.fetchall()]
print('Support AI tables:', tables)
conn.close()