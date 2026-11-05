"""Reversible retirement of virtual crypto trades, never a broker operation.

The full original SQLite database is backed up before the atomic active-ledger
change. Existing CFD trade rows, fees, collateral and risk limits are untouched.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import os
from pathlib import Path
import re
import sqlite3

from .storage import dumps

MARKER = 'crypto_retirement_v1'
CRYPTO_CLASSES = {'crypto', 'crypto_cfd', 'cryptocurrency', 'cryptocurrencies', 'cryptocurrency_cfd'}


def is_crypto_trade(trade):
    """Spot was crypto-only; preserve share CFDs such as COIN and MSTR."""
    symbol = trade.get('symbol', '')
    if trade.get('kind', 'spot') != 'cfd' or not isinstance(symbol, str) or not symbol.startswith('CAP:'):
        return True
    detail = trade.get('detail') or {}
    if not isinstance(detail, dict):
        return False
    context = detail.get('context') or {}
    markets = (context.get('markets') or {}) if isinstance(context, dict) else {}
    market = (markets.get(symbol) or {}) if isinstance(markets, dict) else {}
    instrument = (market.get('instrument') or {}) if isinstance(market, dict) else {}
    direct = detail.get('instrument') or {}
    values = [trade.get('asset_class')]
    if isinstance(instrument, dict):
        values.append(instrument.get('asset_class'))
    if isinstance(direct, dict):
        values.append(direct.get('asset_class'))
    return any(isinstance(value, str) and value.lower() in CRYPTO_CLASSES for value in values)


def _number(value, label, *, nonnegative=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or nonnegative and value < 0):
        raise ValueError(f'Retrait crypto impossible : {label} invalide.')
    return float(value)


def _time(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError('Horodatage de trade sans fuseau.')
    return dt.astimezone(timezone.utc)


def _backup(store, instant):
    directory = Path(store.path).parent / 'backups'
    if directory.is_symlink():
        raise ValueError('Dossier de sauvegarde non autorisé.')
    directory.mkdir(mode=0o700, exist_ok=True)
    if not directory.is_dir():
        raise ValueError('Dossier de sauvegarde indisponible.')
    os.chmod(directory, 0o700)
    name = 'avant_retrait_crypto_' + instant.strftime('%Y%m%d_%H%M%S_%f') + '.sqlite3'
    path = directory / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        backup = sqlite3.connect(str(path))
        try:
            store.conn.backup(backup)
            row = backup.execute('PRAGMA quick_check').fetchone()
            if not row or row[0] != 'ok':
                raise ValueError('Sauvegarde SQLite non vérifiable.')
        finally:
            backup.close()
        os.chmod(path, 0o600)
        with path.open('rb') as snapshot:
            os.fsync(snapshot.fileno())
        directory_fd = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return name


def _daily_baseline(store, kept, retired, instant):
    start = instant.replace(hour=0, minute=0, second=0, microsecond=0)
    day = start.strftime('%Y-%m-%d')
    baseline = _number(store.get('initial_capital', 900.), 'capital initial')
    closed_today = []
    carried = False
    activity_today = False
    for trade in kept:
        opened = _time(trade['opened_at'])
        closed = _time(trade['closed_at']) if trade.get('closed_at') else None
        if closed and closed < start:
            baseline += _number(trade['net_pnl'], 'résultat CFD')
        else:
            activity_today = True
            if closed:
                closed_today.append((closed, _number(trade['net_pnl'], 'résultat CFD')))
        if opened < start and (closed is None or closed >= start):
            carried = True
    previous = store.get('day_state')
    same_day = isinstance(previous, dict) and previous.get('day') == day
    if carried and same_day:
        # A precise past mark-to-market cannot be rebuilt from fills alone.
        # Keep the higher known baseline after removing only certain crypto PnL.
        crypto_before = sum(_number(t['net_pnl'], 'résultat crypto') for t in retired
                            if t.get('closed_at') and _time(t['closed_at']) < start)
        baseline = max(baseline, _number(previous.get('start_equity'), 'base journalière') - crypto_before)
    settings = store.get('settings') or {}
    limit = _number(settings.get('daily_loss_pct', 2.), 'limite journalière', nonnegative=True)
    if not 0 < limit <= 2 or baseline <= 0:
        raise ValueError('Base de risque journalière CFD incohérente.')
    current = baseline
    blocked = False
    for _, pnl in sorted(closed_today):
        current += pnl
        blocked |= current <= baseline * (1 - limit / 100)
    # A recorded intraday CFD loss may have recovered before the final fill.
    # Never remove such a latch merely because crypto is being retired.
    preserve_latch = bool(same_day and previous.get('blocked') and (carried or activity_today))
    blocked |= preserve_latch
    return {'day': day, 'start_equity': baseline, 'blocked': blocked,
            'baseline_approximation': carried, 'scope': 'cfd_only',
            'crypto_retirement_adjusted': True,
            'prior_block_preserved_conservatively': preserve_latch}


def retire_crypto(store, *, now=None):
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError('Retrait crypto : horloge UTC requise.')
    instant = instant.astimezone(timezone.utc)
    at = instant.isoformat(timespec='milliseconds')
    with store.lock:
        existing = store.get(MARKER)
        if existing is not None:
            if not isinstance(existing, dict) or existing.get('version') != 1:
                raise ValueError('Marqueur de retrait crypto invalide.')
            return existing
        all_rows = [store.decode_trade(row) for row in store.rows('SELECT * FROM trades ORDER BY id')]
        retired = [trade for trade in all_rows if is_crypto_trade(trade)]
        kept = [trade for trade in all_rows if not is_crypto_trade(trade)]
        closed = [trade for trade in retired if trade.get('closed_at')]
        opened = [trade for trade in retired if not trade.get('closed_at')]
        removed_net = sum(_number(trade['net_pnl'], 'résultat crypto clôturé') for trade in closed)
        restored_cost = 0.
        for trade in opened:
            if trade.get('kind', 'spot') == 'cfd':
                restored_cost += _number(trade['margin'], 'marge crypto', nonnegative=True)
            else:
                entry = _number(trade['entry'], 'entrée crypto', nonnegative=True)
                qty = _number(trade['quantity'], 'quantité crypto', nonnegative=True)
                restored_cost += entry * qty
            restored_cost += _number(trade['entry_fee'], 'frais crypto', nonnegative=True)
        before = _number(store.get('cash'), 'liquidités')
        after = _number(before - removed_net + restored_cost, 'liquidités corrigées')
        day_state = _daily_baseline(store, kept, retired, instant)
        # Backup is outside BEGIN: sqlite3.backup on an active write transaction
        # can block indefinitely. The shared RLock spans backup and mutation.
        backup_name = _backup(store, instant)
        cfd_closed = [trade for trade in kept if trade.get('closed_at')]
        cfd_open = [trade for trade in kept if not trade.get('closed_at')]
        summary = {'version': 1, 'at': at, 'scope': 'cfd_only',
                   'removed_closed': len(closed), 'removed_open': len(opened),
                   'removed_net_pnl': removed_net, 'restored_open_cost': restored_cost,
                   'cash_before': before, 'cash_after': after,
                   'cfd_closed_count': len(cfd_closed),
                   'cfd_closed_net_pnl': sum(_number(t['net_pnl'], 'résultat CFD') for t in cfd_closed),
                   'cfd_open_count': len(cfd_open), 'backup_name': backup_name,
                   'equity_history_restarted': True,
                   'day_baseline_approximation': day_state['baseline_approximation'],
                   'day_blocked': day_state['blocked']}
        with store.transaction():
            for trade in retired:
                store.conn.execute('DELETE FROM trades WHERE id=?', (trade['id'],))
            # CFD decisions remain intact, including their original opening plan.
            # Mixed/global and spot-only diagnostics are in the private backup.
            decisions = store.rows('SELECT id,symbol FROM decisions')
            retired_symbols = {trade['symbol'] for trade in retired}
            for decision in decisions:
                symbol = decision['symbol']
                if not symbol.startswith('CAP:') or symbol in retired_symbols:
                    store.conn.execute('DELETE FROM decisions WHERE id=?', (decision['id'],))
            crypto_names = re.compile(r'\b(?:Binance|crypto(?:monnaie)?s?' +
                                      ''.join('|' + re.escape(s) for s in sorted(retired_symbols)) + r')\b', re.I)
            for event in store.rows('SELECT id,message FROM events'):
                if crypto_names.search(event['message']):
                    store.conn.execute('DELETE FROM events WHERE id=?', (event['id'],))
            store.conn.execute('DELETE FROM equity')
            for key in ('btc_baseline', 'last_equity', 'last_analysis_at', 'market_ready_after'):
                store.conn.execute('DELETE FROM kv WHERE key=?', (key,))
            live_ids = {trade['id'] for trade in cfd_open}
            pending = store.get('reconcile_pending_by_id') or {}
            if not isinstance(pending, dict):
                raise ValueError('État de réconciliation invalide.')
            pending = {key: value for key, value in pending.items()
                       if isinstance(value, dict) and value.get('id') in live_ids}
            legacy = store.get('reconcile_pending')
            if isinstance(legacy, dict) and legacy.get('id') in live_ids:
                pending.setdefault(str(legacy['id']), legacy)
            store.set('reconcile_pending_by_id', pending)
            store.set('reconcile_pending', next(iter(pending.values()), None))
            store.set('cash', after)
            store.set('day_state', day_state)
            store.set('statistics_scope_started_at', at)
            store.set(MARKER, summary)
            store.event(f'Compte démo recentré sur les CFD : {len(closed)} trades crypto clôturés et {len(opened)} positions crypto retirés du calcul ; résultats CFD conservés. Historique de valorisation relancé à partir de ce retrait.')
        return summary
