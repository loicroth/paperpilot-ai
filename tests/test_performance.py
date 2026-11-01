"""Synthetic accounting cases, not evidence of trading profitability."""
import copy
import json
import pytest
from app.performance import strategy_performance


def trade(identifier, pnl, profile='expanded_v1', market='crypto', risk=2., **changes):
    result = dict(id=identifier, symbol='CAP:US500' if market == 'cfd' else 'BTCUSDT',
                  kind='cfd' if market == 'cfd' else 'spot', opened_at='2026-10-31T08:00:00+00:00',
                  closed_at=f'2026-10-31T10:{identifier % 60:02d}:00+00:00',
                  net_pnl=pnl, risk_usdt=risk, fees=.4, financing_accrued=.1,
                  detail={'context': {'rules': {'strategy_profile': profile}}})
    result.update(changes)
    return result


def test_profile_and_market_cohorts_preserve_historical_attribution_and_net_costs():
    trades = [trade(1, -2), trade(2, 6), trade(3, -3, market='cfd'),
              trade(4, 200, profile='classic'), trade(5, 100, detail={}),
              trade(6, None, closed_at=None)]
    before = copy.deepcopy(trades)
    result = strategy_performance(trades, 'expanded_v1')
    current = result['current']
    assert current['closed_trades'] == 3 and current['net_pnl'] == 1
    assert current['avg_pnl'] == pytest.approx(1/3)
    assert current['fees'] == pytest.approx(1.2)  # Already included in net_pnl.
    assert current['financing'] == pytest.approx(.3)
    assert current['profit_factor'] == pytest.approx(6/5)
    assert current['avg_r'] == pytest.approx(1/6)
    assert result['remaining'] == 47 and not result['review_due']
    assert result['unattributed_closed_trades'] == 1
    assert [g['net_pnl'] for g in result['current_groups']] == [4, -3]
    assert strategy_performance(trades, 'classic')['current']['net_pnl'] == 200
    assert trades == before


def test_review_milestone_uses_only_closed_trades_of_the_current_profile():
    trades = [trade(i, -1) for i in range(1, 50)]
    trades += [trade(51, 9, profile='classic'), trade(52, None, closed_at=None)]
    assert not strategy_performance(trades, 'expanded_v1')['review_due']
    trades.append(trade(50, -1))
    result = strategy_performance(trades, 'expanded_v1')
    assert result['review_due'] and result['remaining'] == 0
    assert result['current']['net_pnl'] == -50
    assert result['current']['max_loss_streak'] == 50
    assert result['current']['max_closed_drawdown_usdt'] == 50
    assert result['current']['entry_days_utc'] == 1  # 50 trades != 50 independent sessions.


def test_chronological_drawdown_and_r_denominator_do_not_use_newest_first_order():
    rows = [trade(3, -3, risk=0), trade(2, 4), trade(1, -2)]
    result = strategy_performance(rows, 'expanded_v1')['current']
    assert result['max_closed_drawdown_usdt'] == 3
    assert result['max_loss_streak'] == 1
    assert result['r_observations'] == 2 and result['avg_r'] == .5


def test_empty_and_all_winning_groups_are_finite_json():
    empty = strategy_performance([], 'expanded_v1')
    assert empty['current']['closed_trades'] == 0 and empty['remaining'] == 50
    assert empty['current']['avg_pnl'] is None
    result = strategy_performance([trade(1, 2)], 'expanded_v1')
    assert result['current']['profit_factor'] is None
    assert result['current_groups'][1]['closed_trades'] == 0
    json.dumps(result, allow_nan=False)
