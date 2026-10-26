"""Liquidity stays available for independent signals; fixtures are not performance."""
from dataclasses import replace
from types import SimpleNamespace
import time
import pytest
from app.config import Settings
from app.market import Quote,Rules
from app.risk import RiskEngine
from test_structural_risk import structural_fixture


def test_three_narrow_stop_spot_entries_share_liquidity_and_total_risk():
    now=int(time.time()*1000);cfg=Settings();cash=900.;positions=[];quotes={}
    for symbol in cfg.symbols:
        q=Quote(symbol,100.,100.02,now);quotes[symbol]=q
        d=SimpleNamespace(symbol=symbol,action='BUY',entry=100.02,stop_loss=99.5,
            take_profit=103.,invalidation=99.5,confirmation_met=True,setup='breakout',risk_pct=None)
        p=RiskEngine.plan(d,q,Rules(symbol,.001,1e6,.001,5.,1e9,.01),cfg,cash,900.,atr=.5,now_ms=now,
            structure_context=structural_fixture(d,.5,now),portfolio_positions=positions,portfolio_quotes=quotes)
        cost=p.quantity*p.entry+p.entry_fee
        assert 295<cost<=300.000001
        cash-=cost
        positions.append({'symbol':symbol,'quantity':p.quantity,'entry':p.entry,'stop_loss':p.stop_loss,
            'fee_rate':cfg.fee_rate,'slippage_bps':cfg.slippage_bps,'kind':'spot'})
    assert 0<=cash<2
    assert len(positions)==3


def test_one_slot_retains_single_position_liquidity_behavior():
    now=int(time.time()*1000);cfg=Settings(max_positions=1)
    q=Quote('BTCUSDT',100.,100.02,now)
    d=SimpleNamespace(symbol=q.symbol,action='BUY',entry=100.02,stop_loss=99.5,take_profit=103.,
        invalidation=99.5,confirmation_met=True,setup='breakout',risk_pct=None)
    p=RiskEngine.plan(d,q,Rules(q.symbol,.001,1e6,.001,5.,1e9,.01),cfg,900.,900.,atr=.5,now_ms=now,
        structure_context=structural_fixture(d,.5,now))
    assert 895<p.quantity*p.entry+p.entry_fee<=900.000001


def test_cfd_margin_allocation_also_reserves_other_slots():
    now=int(time.time()*1000);cfg=Settings(cfd_enabled=True,cfd_leverage=5).model_copy(update={'symbols':['CAP:NVDA']})
    q=Quote('CAP:NVDA',100.,100.02,now,currency='USD')
    d=SimpleNamespace(symbol=q.symbol,action='BUY',entry=100.02,stop_loss=99.8,take_profit=104.,
        invalidation=99.8,confirmation_met=True,setup='breakout',risk_pct=1.4)
    rules=Rules(q.symbol,.001,1e6,.001,0,1e9,.01,asset_class='share_cfd',provider='capital_demo',
        currency='USD',min_margin_pct=20.,allow_short=True)
    p=RiskEngine.plan(d,q,rules,cfg,900.,900.,atr=.2,now_ms=now,
        structure_context=structural_fixture(d,.2,now))
    assert 295<p.margin+p.entry_fee<=300.000001
