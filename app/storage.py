from __future__ import annotations
import json, sqlite3, threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from .config import Settings

def utcnow(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds')
def dumps(value): return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',',':'))

class Store:
    def __init__(self, path):
        had_file=Path(path).exists() and Path(path).stat().st_size>0
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=FULL')
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.conn.executescript('''
        CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS trades(
          id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, opened_at TEXT NOT NULL, closed_at TEXT,
          entry REAL NOT NULL, exit REAL, quantity REAL NOT NULL, stop_loss REAL NOT NULL,
          take_profit REAL NOT NULL, entry_fee REAL NOT NULL, exit_fee REAL NOT NULL DEFAULT 0,
          fees REAL NOT NULL, net_pnl REAL, risk_usdt REAL NOT NULL, entry_reason TEXT NOT NULL,
          exit_reason TEXT, detail TEXT NOT NULL, last_market_at INTEGER NOT NULL,
          entry_bid REAL NOT NULL, entry_ask REAL NOT NULL, exit_bid REAL, exit_ask REAL,
          signal TEXT UNIQUE NOT NULL, fee_rate REAL NOT NULL, slippage_bps REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS signals(id TEXT PRIMARY KEY, at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY, at TEXT NOT NULL, symbol TEXT NOT NULL,
          action TEXT NOT NULL, reason TEXT NOT NULL, rejection TEXT, detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS equity(id INTEGER PRIMARY KEY, at TEXT NOT NULL, equity REAL NOT NULL,
          cash REAL NOT NULL, btc_equity REAL);
        CREATE TABLE IF NOT EXISTS usage(day TEXT PRIMARY KEY, calls INTEGER NOT NULL DEFAULT 0,
          tokens INTEGER NOT NULL DEFAULT 0);
        ''')
        previous_version=self.conn.execute('PRAGMA user_version').fetchone()[0]
        if had_file and previous_version<3:
            backups=Path(path).parent/'backups';backups.mkdir(exist_ok=True)
            name='avant_multi_positions_'+__import__('datetime').datetime.now(__import__('datetime').timezone.utc).strftime('%Y%m%d_%H%M%S_%f')+'.sqlite3'
            with sqlite3.connect(str(backups/name)) as backup: self.conn.backup(backup)
        # Additive, idempotent migration: existing spot positions/history retain defaults.
        columns={row[1] for row in self.conn.execute('PRAGMA table_info(trades)')}
        additions={
            'kind': "TEXT NOT NULL DEFAULT 'spot'", 'direction': "TEXT NOT NULL DEFAULT 'LONG'",
            'margin': 'REAL NOT NULL DEFAULT 0', 'leverage': 'REAL NOT NULL DEFAULT 1',
            'multiplier': 'REAL NOT NULL DEFAULT 1', 'currency': "TEXT NOT NULL DEFAULT 'USDT'",
            'fx_open': 'REAL NOT NULL DEFAULT 1', 'financing_daily': 'REAL NOT NULL DEFAULT 0',
            'financing_accrued': 'REAL NOT NULL DEFAULT 0', 'financing_at': 'INTEGER NOT NULL DEFAULT 0',
            'maintenance_margin_pct': 'REAL NOT NULL DEFAULT 50',
            'last_bid': 'REAL', 'last_ask': 'REAL', 'last_fx_bid': 'REAL', 'last_fx_ask': 'REAL'}
        with self.transaction():
            for name, declaration in additions.items():
                if name not in columns: self.conn.execute(f'ALTER TABLE trades ADD COLUMN {name} {declaration}')
            self.conn.execute('DROP INDEX IF EXISTS one_position')
            self.conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS one_position_per_symbol ON trades(symbol) WHERE closed_at IS NULL')
            if self.get('cash') is None:
                self.set('cash',900.0); self.set('initial_capital',900.0)
                self.set('settings',Settings().model_dump()); self.set('mode','stopped')
                self.set('session_started',utcnow())
            if previous_version<3: self.conn.execute("PRAGMA user_version=3")
    @contextmanager
    def transaction(self):
        with self.lock:
            self.conn.execute('BEGIN IMMEDIATE')
            try:
                yield self
                self.conn.execute('COMMIT')
            except BaseException:
                self.conn.execute('ROLLBACK'); raise
    def get(self,key,default=None):
        with self.lock:
            row=self.conn.execute('SELECT value FROM kv WHERE key=?',(key,)).fetchone()
            return json.loads(row['value']) if row else default
    def set(self,key,value):
        with self.lock: self.conn.execute('INSERT INTO kv VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,dumps(value)))
    def rows(self,sql,args=()):
        with self.lock: return [dict(r) for r in self.conn.execute(sql,args).fetchall()]
    def positions(self):
        return [self.decode_trade(row) for row in self.rows('SELECT * FROM trades WHERE closed_at IS NULL ORDER BY id')]
    def position(self,symbol=None):
        if symbol is None:
            rows=self.rows('SELECT * FROM trades WHERE closed_at IS NULL ORDER BY id LIMIT 1')
        else:
            rows=self.rows('SELECT * FROM trades WHERE closed_at IS NULL AND symbol=? ORDER BY id LIMIT 1',(symbol,))
        return self.decode_trade(rows[0]) if rows else None
    @staticmethod
    def decode_trade(row):
        row['detail']=json.loads(row['detail']) if isinstance(row['detail'],str) else row['detail']
        return row
    def trades(self): return [self.decode_trade(r) for r in self.rows('SELECT * FROM trades WHERE closed_at IS NOT NULL ORDER BY id DESC')]
    def event(self,message,level='info'):
        with self.lock: self.conn.execute('INSERT INTO events(at,level,message) VALUES(?,?,?)',(utcnow(),level,str(message)[:3000]))
    def decision(self,decision,rejection,detail):
        with self.lock:
            self.conn.execute('INSERT INTO decisions(at,symbol,action,reason,rejection,detail) VALUES(?,?,?,?,?,?)',
                (utcnow(),decision.symbol,decision.action,decision.reason,rejection,dumps(detail)))
            self.set('last_analysis_at',utcnow())
    def claim_signal(self,signal):
        with self.lock:
            return self.conn.execute('INSERT OR IGNORE INTO signals VALUES(?,?)',(signal,utcnow())).rowcount==1
    def reserve_usage(self,day,max_calls,max_tokens,reserve):
        with self.transaction():
            self.conn.execute('INSERT OR IGNORE INTO usage(day) VALUES(?)',(day,))
            row=self.rows('SELECT * FROM usage WHERE day=?',(day,))[0]
            if row['calls']>=max_calls or row['tokens']+reserve>max_tokens: return False
            self.conn.execute('UPDATE usage SET calls=calls+1,tokens=tokens+? WHERE day=?',(reserve,day))
            return True
    def settle_usage(self,day,reserve,actual):
        with self.lock:
            # Unknown usage (timeout) keeps the full conservative reservation.
            if actual>0: self.conn.execute('UPDATE usage SET tokens=MAX(0,tokens+?) WHERE day=?',(actual-reserve,day))
    def reset(self, force=False):
        with self.transaction():
            if self.position() and not force: raise ValueError('Fermez toutes les positions avant de réinitialiser le portefeuille.')
            self.set('mode','stopped')
            for table in ('trades','signals','decisions','equity','events'): self.conn.execute('DELETE FROM '+table)
            for key in ('day_state','btc_baseline','last_equity','last_analysis_at','last_cycle','market_ready_after',
                        'reconcile_pending','reconcile_pending_by_id','last_managed_position_id'):
                self.conn.execute('DELETE FROM kv WHERE key=?',(key,))
            self.set('cash',900.0); self.set('initial_capital',900.0); self.set('session_started',utcnow())
            self.event('Portefeuille réinitialisé à 900 USDT. Le budget API du jour est conservé.')
    def close(self):
        with self.lock: self.conn.close()
