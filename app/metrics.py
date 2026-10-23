from __future__ import annotations
from collections import defaultdict

def statistics(store,quotes):
    p=store.position(); cash=store.get('cash',900.); initial=store.get('initial_capital',900.)
    trades=store.trades(); realized=sum(t['net_pnl'] for t in trades)
    market=None; unrealized=0.; equity=cash
    if p:
        q=quotes.get(p['symbol'])
        # A stale valuation is labelled in health, never used to open trades.
        market=q.bid if q else p['entry']
        net_exit=market*(1-p['slippage_bps']/10000)*(1-p['fee_rate'])
        equity+=p['quantity']*net_exit
        unrealized=p['quantity']*(net_exit-p['entry'])-p['entry_fee']
        p.update(current_price=market,unrealized_pnl=unrealized)
    wins=[t['net_pnl'] for t in trades if t['net_pnl']>0]
    losses=[t['net_pnl'] for t in trades if t['net_pnl']<0]
    series=store.rows('SELECT * FROM equity ORDER BY id')
    peak=initial; maxdd=0.
    for row in series+[{'equity':equity}]:
        peak=max(peak,row['equity']); maxdd=max(maxdd,(peak-row['equity'])/peak*100 if peak else 0)
    by=defaultdict(lambda:{'trades':0,'net_pnl':0.,'winners':0})
    daily=defaultdict(lambda:{'trades':0,'net_pnl':0.})
    for t in reversed(trades):
        item=by[t['symbol']]; item['trades']+=1; item['net_pnl']+=t['net_pnl']; item['winners']+=int(t['net_pnl']>0)
        d=daily[t['closed_at'][:10]]; d['trades']+=1; d['net_pnl']+=t['net_pnl']
    btc=quotes.get('BTCUSDT'); base=store.get('btc_baseline')
    benchmark=(base['quantity']*btc.bid*(1-base['slippage_bps']/10000)*(1-base['fee_rate']) if isinstance(base,dict) else initial*btc.bid/base) if btc and base else None
    stats=dict(initial_capital=initial,equity=equity,cash=cash,realized_pnl=realized,unrealized_pnl=unrealized,
        pnl_pct=(equity/initial-1)*100,closed_trades=len(trades),win_rate=len(wins)/len(trades)*100 if trades else 0,
        winners=len(wins),losers=len(losses),avg_win=sum(wins)/len(wins) if wins else 0,avg_loss=sum(losses)/len(losses) if losses else 0,
        profit_factor=sum(wins)/abs(sum(losses)) if losses else None,max_drawdown_pct=maxdd,
        realized_rr=sum(t['net_pnl']/t['risk_usdt'] for t in trades)/len(trades) if trades else 0,
        total_fees=sum(t['fees'] for t in trades)+(p['entry_fee'] if p else 0),by_symbol=dict(by),
        daily=[{'date':d,**v} for d,v in sorted(daily.items())],btc_benchmark_equity=benchmark)
    return stats,p
