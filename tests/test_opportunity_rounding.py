"""Tick-boundary regressions using real structure and final risk admission."""
from dataclasses import replace
from decimal import Decimal

import pytest

from app.opportunity import market_feasibility
from test_opportunity import fixture, plan_at_bound


def scaled(price, scale):
    return float(Decimal(str(price)) * Decimal(str(scale)))


def observed_levels(data, *, support, resistance):
    """Keep explicit confirmed pivots without float-derived reference prices."""
    for tf in data.values():
        atr = tf['atr14']
        for index, bar in enumerate(tf['candles']):
            entry = bar['close']
            bar['low'] = support if index == 2 else min(entry, support + .4 * atr)
            bar['high'] = resistance if index == 3 else max(entry, resistance - .4 * atr)


@pytest.mark.parametrize('scale', [1., .000001])
def test_long_exact_market_margin_does_not_spend_an_extra_tick_and_reject_two_r(scale):
    data, quote, rule, cfg = fixture()
    quote = replace(quote, bid=scaled(100., scale), ask=scaled(100.02, scale))
    rule = replace(rule, tick_size=scaled(.01, scale))
    for tf in data.values():
        tf['atr14'] = 1. * scale
        for bar in tf['candles']:
            bar['open'] = bar['close'] = quote.ask
    observed_levels(data, support=scaled(98.3, scale), resistance=scaled(104.78, scale))

    result = market_feasibility(data, quote, rule, cfg)

    expected_stop = float(Decimal('98.15') * Decimal(str(scale)))
    expected_target = float(Decimal('104.68') * Decimal(str(scale)))
    assert result['status'] == 'feasible'
    assert result['reference_stop'] == expected_stop
    assert result['reference_target'] == expected_target
    plan = plan_at_bound(data, quote, rule, cfg, result)
    assert plan.rr >= 2.
    assert result['max_net_rr'] == pytest.approx(plan.rr, rel=1e-12)
    assert plan.stop_loss == expected_stop
    assert plan.take_profit == expected_target


@pytest.mark.parametrize('scale', [1., .000001])
def test_short_exact_market_margin_does_not_round_up_one_extra_tick(scale):
    data, quote, rule, cfg = fixture(cfd=True, short=True, financing=0.)
    quote = replace(quote, bid=scaled(100., scale), ask=scaled(100.02, scale))
    rule = replace(rule, tick_size=scaled(.01, scale))
    for tf in data.values():
        tf['atr14'] = 1. * scale
        for bar in tf['candles']:
            bar['open'] = bar['close'] = quote.bid
    observed_levels(data, support=scaled(95.22, scale), resistance=scaled(101.65, scale))

    result = market_feasibility(data, quote, rule, cfg, short=True)

    expected_stop = float(Decimal('101.80') * Decimal(str(scale)))
    expected_target = float(Decimal('95.32') * Decimal(str(scale)))
    assert result['status'] == 'feasible'
    assert result['reference_stop'] == expected_stop
    assert result['reference_target'] == expected_target
    plan = plan_at_bound(data, quote, rule, cfg, result, short=True)
    assert result['max_net_rr'] == pytest.approx(plan.rr, rel=1e-12)
    assert plan.stop_loss == expected_stop
    assert plan.take_profit == expected_target


def test_target_buffer_is_computed_before_tick_quantization_near_two_r_boundary():
    data, quote, rule, cfg = fixture()
    quote = replace(quote, ask=100.0245)
    for tf in data.values():
        tf['atr14'] = 1.5
        for bar in tf['candles']:
            bar['open'] = bar['close'] = quote.ask
    observed_levels(data, support=98.695, resistance=104.19)

    result = market_feasibility(data, quote, rule, cfg)

    # 104.19 - .1 * 1.5 must give 104.04, not a float just below it
    # subsequently floored to 104.03. That extra tick caused a false WAIT.
    assert result['status'] == 'feasible'
    assert result['reference_stop'] == 98.47
    assert result['reference_target'] == 104.04
    plan = plan_at_bound(data, quote, rule, cfg, result)
    assert plan.rr >= 2.
    assert result['max_net_rr'] == pytest.approx(plan.rr, rel=1e-12)
