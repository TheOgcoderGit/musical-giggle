import sqlite3

# Check if support tables exist
conn = sqlite3.connect('/home/user/project/channelflow/extracted/ChannelFlowAI5_monetization/channelflow.db')
cur = conn.cursor()

# List all tables
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [row[0] for row in cur.fetchall()]
print("Tables:", tables)

# Check for support-related tables
for table in ['support_tickets', 'support_messages', 'knowledge_articles', 'ai_usage']:
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    if cur.fetchone():
        print(f"Table {table}: EXISTS")
        cur.execute(f"PRAGMA table_info({table})")
        for col in cur.fetchall():
            print(f"  {col}")
    else:
        print(f"Table {table}: MISSING")

conn.close()