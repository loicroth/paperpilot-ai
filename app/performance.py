"""Read-only cohorts from the profile recorded at entry, never today's settings."""
from collections import defaultdict
import math

REVIEW_TRADES = 50


def recorded_profile(trade):
    detail = trade.get('detail') or {}
    profile = ((detail.get('context') or {}).get('rules') or {}).get('strategy_profile')
    return profile if isinstance(profile, str) and profile else 'unattributed'


def _summary(trades):
    rows = sorted(trades, key=lambda t: (t['closed_at'], t.get('id', 0)))
    wins = [t['net_pnl'] for t in rows if t['net_pnl'] > 0]
    losses = [t['net_pnl'] for t in rows if t['net_pnl'] < 0]
    rs = [t['net_pnl'] / t['risk_usdt'] for t in rows
          if isinstance(t.get('risk_usdt'), (int, float))
          and math.isfinite(t['risk_usdt']) and t['risk_usdt'] > 0]
    cumulative = peak = drawdown = 0.
    streak = longest = 0
    for trade in rows:
        cumulative += trade['net_pnl']
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
        streak = streak + 1 if trade['net_pnl'] < 0 else 0
        longest = max(longest, streak)
    return {
        'closed_trades': len(rows), 'winners': len(wins), 'losers': len(losses),
        'breakeven': len(rows) - len(wins) - len(losses), 'net_pnl': cumulative,
        'win_rate': len(wins) / len(rows) * 100 if rows else None,
        'avg_win': sum(wins) / len(wins) if wins else None,
        'avg_loss': sum(losses) / len(losses) if losses else None,
        'avg_pnl': cumulative / len(rows) if rows else None,
        'profit_factor': sum(wins) / -sum(losses) if losses else None,
        'avg_r': sum(rs) / len(rs) if rs else None, 'r_observations': len(rs),
        # net_pnl already includes fees/financing: report, never subtract twice.
        'fees': sum(t.get('fees', 0.) for t in rows),
        'financing': sum(t.get('financing_accrued', 0.) for t in rows),
        'max_closed_drawdown_usdt': drawdown, 'max_loss_streak': longest,
        'first_opened_at': min((t['opened_at'] for t in rows), default=None),
        'last_closed_at': rows[-1]['closed_at'] if rows else None,
        'entry_days_utc': len({t['opened_at'][:10] for t in rows}),
    }


def strategy_performance(trades, active_profile, *, cfd_only=False):
    """50 is a review milestone, not a statistical validation or trading gate."""
    closed = [t for t in trades if t.get('closed_at') and
              isinstance(t.get('net_pnl'), (int, float)) and math.isfinite(t['net_pnl'])]
    if cfd_only:
        closed = [t for t in closed if t.get('kind') == 'cfd' or t['symbol'].startswith('CAP:')]
    cohorts = defaultdict(list)
    for trade in closed:
        market = 'cfd' if trade.get('kind') == 'cfd' or trade['symbol'].startswith('CAP:') else 'crypto'
        cohorts[recorded_profile(trade), market].append(trade)
    current = [t for t in closed if recorded_profile(t) == active_profile]
    summary = _summary(current)
    groups = [{'profile': profile, 'market': market, **_summary(rows)}
              for (profile, market), rows in sorted(cohorts.items())]
    current_groups = [{'profile': active_profile, 'market': market,
                       **_summary(cohorts.get((active_profile, market), []))}
                      for market in (('cfd',) if cfd_only else ('crypto', 'cfd'))]
    return {
        'scope': 'cfd_only' if cfd_only else 'all_markets',
        'active_profile': active_profile, 'review_target': REVIEW_TRADES,
        'remaining': max(0, REVIEW_TRADES - len(current)),
        'review_due': len(current) >= REVIEW_TRADES,
        'unattributed_closed_trades': sum(recorded_profile(t) == 'unattributed' for t in closed),
        'current': summary, 'current_groups': current_groups, 'groups': groups,
    }

