from __future__ import annotations
from collections import defaultdict
import math
from .risk import portfolio_remaining_risk
from .execution import portfolio_valuations
from .performance import strategy_performance

def statistics(store,quotes,*,cfd_only=None):
    # Cash, open collateral and closed results must come from one DB snapshot,
    # including callers running concurrently with a virtual fill.
    with store.lock:
        return _statistics_locked(store,quotes,cfd_only=cfd_only)


def _statistics_locked(store,quotes,*,cfd_only=None):
    positions=portfolio_valuations(store,quotes)
    cash=store.get('cash',900.); initial=store.get('initial_capital',900.)
    trades=store.trades(); realized=sum(t['net_pnl'] for t in trades)
    equity=cash+sum(p['equity_add'] for p in positions)
    unrealized=sum(p['unrealized_pnl'] for p in positions)
    # Compatibility for callers displaying only a single position. The engine
    # and current dashboard use the aggregate positions list below.
    p=positions[0] if positions else None
    for row in positions: row.pop('equity_add',None)
    settings=store.get('settings',{})
    cfd_only=bool(settings.get('cfd_only',False)) if cfd_only is None else cfd_only
    initial_risk=sum(row['risk_usdt'] for row in positions)
    remaining_risk=portfolio_remaining_risk(positions,quotes)
    risk_used=remaining_risk if math.isfinite(remaining_risk) else None
    risk_cap=max(0.,equity)*settings.get('max_portfolio_risk_pct',2.)/100
    wins=[t['net_pnl'] for t in trades if t['net_pnl']>0]
    losses=[t['net_pnl'] for t in trades if t['net_pnl']<0]
    series=store.rows('SELECT * FROM equity ORDER BY id')
    # Mixed-market equity cannot be reconstructed from the remaining CFD
    # closes. The migration starts a new curve; its drawdown starts there too.
    peak=(series[0]['equity'] if series else equity) if cfd_only else initial
    maxdd=0.
    for row in series+[{'equity':equity}]:
        peak=max(peak,row['equity']); maxdd=max(maxdd,(peak-row['equity'])/peak*100 if peak else 0)
    by=defaultdict(lambda:{'trades':0,'net_pnl':0.,'winners':0})
    daily=defaultdict(lambda:{'trades':0,'net_pnl':0.})
    for t in reversed(trades):
        item=by[t['symbol']]; item['trades']+=1; item['net_pnl']+=t['net_pnl']; item['winners']+=int(t['net_pnl']>0)
        d=daily[t['closed_at'][:10]]; d['trades']+=1; d['net_pnl']+=t['net_pnl']
    btc=quotes.get('BTCUSDT') if not cfd_only else None; base=store.get('btc_baseline') if not cfd_only else None
    benchmark=(base['quantity']*btc.bid*(1-base['slippage_bps']/10000)*(1-base['fee_rate']) if isinstance(base,dict) else initial*btc.bid/base) if btc and base else None
    cleanup=store.get('crypto_retirement_v1',{}) if cfd_only else {}
    cleanup_summary={key:cleanup[key] for key in ('at','removed_closed','removed_open','removed_net_pnl') if key in cleanup}
    stats=dict(initial_capital=initial,equity=equity,cash=cash,realized_pnl=realized,unrealized_pnl=unrealized,
        pnl_pct=(equity/initial-1)*100,closed_trades=len(trades),win_rate=len(wins)/len(trades)*100 if trades else 0,
        winners=len(wins),losers=len(losses),avg_win=sum(wins)/len(wins) if wins else 0,avg_loss=sum(losses)/len(losses) if losses else 0,
        profit_factor=sum(wins)/abs(sum(losses)) if losses else None,max_drawdown_pct=maxdd,
        realized_rr=sum(t['net_pnl']/t['risk_usdt'] for t in trades)/len(trades) if trades else 0,
        positions=positions,open_positions_count=len(positions),max_positions=settings.get('max_positions',3),
        portfolio_risk_used=risk_used,open_risk_usdt=initial_risk,portfolio_risk_cap=risk_cap,
        portfolio_risk_known=risk_used is not None,
        portfolio_risk_pct=risk_used/equity*100 if risk_used is not None and equity>0 else None,
        margin_used=sum(row.get('margin',0.) for row in positions),
        financing_total=sum(t.get('financing_accrued',0.) for t in trades)+sum(row.get('financing_usdt',0.) for row in positions),
        total_fees=sum(t['fees'] for t in trades)+sum(row['entry_fee'] for row in positions),by_symbol=dict(by),
        daily=[{'date':d,**v} for d,v in sorted(daily.items())],btc_benchmark_equity=benchmark,
        history_scope='cfd_only' if cfd_only else 'all_markets',
        equity_history_started_at=store.get('statistics_scope_started_at') if cfd_only else None,
        max_drawdown_scope='since_cfd_curve_start' if cfd_only else 'since_reset',
        crypto_cleanup=cleanup_summary or None,
        strategy_performance=strategy_performance(trades,settings.get('strategy_profile','unattributed'),cfd_only=cfd_only))
    return stats,p

