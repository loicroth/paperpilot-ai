"""Structural-risk fixtures are synthetic tests, never live trading evidence."""
from types import SimpleNamespace
from dataclasses import replace
import math,time
import pytest
from app.config import Settings
from app.market import Quote,Rules
from app.structure import structural_levels,StructureRejected
from app.risk import RiskEngine,RiskRejected,remaining_position_risk,portfolio_remaining_risk


def structural_fixture(decision,atr=1.,now_ms=None):
    """A valid closed-bar fixture with one explicit support and resistance."""
    now=int(time.time()*1000) if now_ms is None else now_ms
    short=decision.action=='SHORT'
    lower=(decision.take_profit-.2*atr) if short else (decision.stop_loss+.2*atr)
    upper=(decision.stop_loss-.2*atr) if short else (decision.take_profit+.2*atr)
    entry=decision.entry
    context={}
    for name,span in [('m5',300000),('h1',3600000)]:
        end=now//span*span-1;bars=[]
        for i in range(7):
            low=lower if i==2 else min(entry,lower+.4*atr)
            high=upper if i==3 else max(entry,upper-.4*atr)
            bars.append(dict(open_time=end-(6-i)*span-span+1,close_time=end-(6-i)*span,
                open=entry,close=entry,low=low,high=high,volume=100.))
        context[name]={'candles':bars,'last':bars[-1],'atr14':atr}
    return context


def proposal(stop=98.,target=108.,risk=None):
    return SimpleNamespace(symbol='BTCUSDT',action='BUY',entry=100.02,stop_loss=stop,
        take_profit=target,invalidation=stop,confirmation_met=True,setup='breakout',risk_pct=risk)


def plan(d=None,**changes):
    d=d or proposal();q=changes.pop('quote',Quote(d.symbol,100.,100.02,int(time.time()*1000)))
    cfg=changes.pop('settings',Settings())
    args=dict(decision=d,quote=q,rules=Rules(d.symbol,.001,1e6,.001,5.,1e9,.01),settings=cfg,
        cash=900.,equity=900.,atr=1.,structure_context=structural_fixture(d,1.,q.time_ms))
    args.update(changes);return RiskEngine.plan(**args)


def test_context_mandatory_and_future_bars_rejected():
    with pytest.raises(RiskRejected,match='Contexte'):plan(structure_context=None)
    d=proposal();ctx=structural_fixture(d);ctx['m5']['candles'][-1]['close_time']=int(time.time()*1000)+60000
    with pytest.raises(RiskRejected,match='futures'):plan(d,structure_context=ctx)


def test_anchored_stop_and_target_audited_without_repairing_levels():
    d=proposal();p=plan(d)
    assert p.stop_loss==98 and p.take_profit==108
    assert p.structure_audit['validated'] is True
    assert p.structure_audit['stop_reference']['price']==pytest.approx(98.2)
    assert p.structure_audit['target_obstacle']['price']==pytest.approx(108.2)


def test_target_behind_first_obstacle_rejected_even_with_nominal_2r():
    d=proposal();ctx=structural_fixture(d)
    for tf in ctx.values():
        for bar in tf['candles']:bar['high']=min(bar['high'],102.5)
    with pytest.raises(RiskRejected,match='obstacle'):plan(d,structure_context=ctx)


def test_stop_inside_pivot_noise_rejected():
    d=proposal();ctx=structural_fixture(d)
    for tf in ctx.values():
        for bar in tf['candles']:bar['low']=max(bar['low'],97.95)
        tf['candles'][2]['low']=98.05
    with pytest.raises(RiskRejected,match='bruit du pivot'):plan(d,structure_context=ctx)


def test_no_objective_reference_waits_instead_of_inventing_two_r():
    d=proposal();ctx=structural_fixture(d)
    for tf in ctx.values():
        for bar in tf['candles']:bar['high']=100.02
    with pytest.raises(RiskRejected,match='Aucun obstacle'):plan(d,structure_context=ctx)


def test_wide_structural_stop_reduces_quantity_not_risk_budget():
    narrow=plan()
    wide=plan(proposal(stop=94.,target=116.))
    assert wide.entry-wide.stop_loss>4
    assert wide.quantity<narrow.quantity
    assert narrow.risk_usdt<=9.00000001 and wide.risk_usdt<=9.00000001


def test_requested_one_point_four_percent_requires_structure_and_is_capped():
    p=plan(proposal(stop=94.,target=116.,risk=1.4))
    assert p.risk_usdt<=12.60000001 and p.risk_pct_used<=1.4
    assert p.risk_usdt>9
    with pytest.raises(RiskRejected):plan(proposal(risk=1.41))
    with pytest.raises(RiskRejected):plan(proposal(risk=1.4),settings=Settings(max_trade_risk_pct=1.))


def test_daily_budget_further_reduces_quantity_without_changing_levels():
    base=plan(proposal(risk=1.4));limited=plan(proposal(risk=1.4),risk_budget_pct=.3)
    assert limited.quantity<base.quantity and limited.risk_usdt<=2.70000001
    assert limited.stop_loss==base.stop_loss and limited.take_profit==base.take_profit
    with pytest.raises(RiskRejected):plan(risk_budget_pct=0)


def existing(symbol='ETHUSDT',**changes):
    p=dict(symbol=symbol,quantity=2.,entry=100.,stop_loss=98.,slippage_bps=5.,fee_rate=.001,kind='spot')
    p.update(changes);return p


def test_parallel_admission_same_symbol_and_slot_cap():
    now=int(time.time()*1000);p=existing();quotes={'ETHUSDT':Quote('ETHUSDT',100.,100.02,now)}
    new=plan(portfolio_positions=[p],portfolio_quotes=quotes)
    assert new.risk_usdt>0
    with pytest.raises(RiskRejected,match='Position déjà'):plan(portfolio_positions=[existing('BTCUSDT')],portfolio_quotes=quotes)
    with pytest.raises(RiskRejected,match='Nombre maximal'):plan(portfolio_positions=[p]*3,portfolio_quotes=quotes)


def test_remaining_risk_is_mark_to_stop_including_exit_costs_and_no_netting():
    now=int(time.time()*1000);p=existing();q=Quote('ETHUSDT',100.,100.02,now)
    expected=2*(100-98*.9995*.999)
    assert remaining_position_risk(p,q,now)==pytest.approx(expected)
    higher=replace(q,bid=104.,ask=104.02)
    assert remaining_position_risk(p,higher,now)>remaining_position_risk(p,q,now)
    assert portfolio_remaining_risk([p,p],{'ETHUSDT':q},now)==pytest.approx(2*expected)
    assert math.isinf(remaining_position_risk(p,None,now))
    assert math.isinf(remaining_position_risk(p,replace(q,time_ms=now-16000),now))


def test_portfolio_budget_independent_of_slot_availability():
    now=int(time.time()*1000);p=existing(quantity=6.)
    quote=Quote('ETHUSDT',100.,100.02,now)
    used=remaining_position_risk(p,quote,now)
    new=plan(proposal(risk=1.4),portfolio_positions=[p],portfolio_quotes={'ETHUSDT':quote})
    assert new.risk_usdt+used<=18.00000001
    with pytest.raises(RiskRejected):plan(portfolio_positions=[p],portfolio_quotes={})


def test_cfd_remaining_risk_includes_financing_and_fx_ask():
    now=int(time.time()*1000)
    p=existing('CAP:NVDA',kind='cfd',direction='SHORT',stop_loss=103.,financing_daily=.001)
    q=Quote('CAP:NVDA',100.,100.02,now,currency='USD',conversion_bid=.99,conversion_ask=1.01)
    expected=2*((103*1.0005-100.02)+103*1.0005*.001+100*.001)*1.01
    assert remaining_position_risk(p,q,now)==pytest.approx(expected)


def test_structural_levels_require_confirmed_closed_bars():
    d=proposal();levels=structural_levels(structural_fixture(d),d.entry)
    assert all(r['confirmed_at']>=r['pivot_at'] for k in ('supports','resistances') for r in levels[k])
    assert levels['stop_buffer']==pytest.approx(.15)
    assert levels['target_buffer']==pytest.approx(.1)


def test_new_settings_bounds_and_existing_settings_fill_defaults():
    cfg=Settings();assert cfg.max_positions==3 and cfg.max_trade_risk_pct==1.4 and cfg.max_portfolio_risk_pct==2
    legacy=cfg.model_dump()
    for key in ('max_positions','max_trade_risk_pct','max_portfolio_risk_pct'):legacy.pop(key)
    assert Settings.model_validate(legacy).max_positions==3
    for kwargs in ({'max_positions':7},{'max_trade_risk_pct':1.41},{'risk_pct':1.41},{'max_portfolio_risk_pct':4.01}):
        with pytest.raises(ValueError):Settings(**kwargs)


def structural_plan(*args,**kwargs):
    """Supply explicit synthetic structure to legacy accounting/risk fixtures."""
    decision=args[0] if args else kwargs.get('decision')
    quote=args[1] if len(args)>1 else kwargs.get('quote')
    atr=kwargs.get('atr',args[9] if len(args)>9 else None)
    if 'structure_context' not in kwargs:
        values=[getattr(decision,k,None) for k in ('entry','stop_loss','take_profit')]+[atr]
        valid=all(isinstance(v,(int,float)) and math.isfinite(v) and v>0 for v in values)
        kwargs['structure_context']=structural_fixture(decision,atr,quote.time_ms) if valid and quote is not None else None
    return RiskEngine.plan(*args,**kwargs)
