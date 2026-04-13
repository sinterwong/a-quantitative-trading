import sqlite3, os
db = 'C:/Users/sinte/.openclaw/workspace/quant_repo/backend/services/portfolio.db'
conn = sqlite3.connect(db)
print('Tables:', conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
for t in ['positions', 'trades', 'signals']:
    try:
        rows = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
        print(f'{t}: {rows} rows')
    except Exception as e:
        print(f'{t}: error - {e}')
print('\nAll positions:')
for row in conn.execute('SELECT symbol, shares, entry_price FROM positions').fetchall():
    print(f'  {row}')
print('\nAll trades:')
for row in conn.execute('SELECT symbol, direction, price, shares FROM trades ORDER BY id').fetchall():
    print(f'  {row}')
