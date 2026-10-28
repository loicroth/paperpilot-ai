"""Admission regression fixtures; no market performance or live trading claims."""
import asyncio
from dataclasses import replace
import pytest
from app.ai import wait
from app.indicators import snapshot
from app.market import MarketError
from test_engine import rig, buy_decision


def install_ranked_markets(rig,monkeypatch):
    engine,store,market,ai,_=rig
    symbols=['BTCUSDT','ETHUSDT','SOLUSDT','ADAUSDT','XRPUSDT']
    cfg=engine.settings().model_copy(update={'auto_scanner':True,'scanner_limit':5,'ai_shortlist':4})
    store.set('settings',cfg.model_dump())
    async def liquid_symbols(limit):return symbols
    monkeypatch.setattr(market,'liquid_symbols',liquid_symbols,raising=False)
    original=market.candles
    async def candles(symbol,*args,**kwargs):
        rows=await original(symbol,*args,**kwargs)
        rows=[replace(c,open=100.,close=100.,high=100.,low=99.5,volume=100.) for c in rows]
        rows[-6]=replace(rows[-6],high=108.2 if symbol=='XRPUSDT' else 100.3)
        rows[-5]=replace(rows[-5],low=98.2)
        rows[-1]=replace(rows[-1],volume=600.-symbols.index(symbol)*100)
        return rows
    monkeypatch.setattr(market,'candles',candles)
    monkeypatch.setattr('app.engine.candidates',lambda *args,**kwargs:['breakout'])
    return symbols,cfg


@pytest.mark.asyncio
async def test_feasible_fifth_candidate_reaches_ai_and_opens_with_unchanged_risk_rules(rig,monkeypatch):
    engine,store,market,ai,_=rig
    symbols,cfg=install_ranked_markets(rig,monkeypatch)
    ai.proposal=buy_decision().model_copy(update={'symbol':'XRPUSDT','stop_loss':98.05,
        'take_profit':108.1,'invalidation':98.05})
    await engine._analyze()
    assert len(ai.contexts)==1
    assert ai.contexts[0]['symbols']==['XRPUSDT']
    position=store.position('XRPUSDT')
    assert position is not None and position['quantity']>0
    assert position['risk_usdt']<=900*cfg.risk_pct/100
    assert position['stop_loss']==98.05 and position['take_profit']==108.1
    rows={row['symbol']:row for row in engine.scan['rows']}
    assert all(rows[s]['status']=='ATTENTE' for s in symbols[:4])
    assert all(rows[s]['feasibility']['BUY']['status']=='insufficient_rr' for s in symbols[:4])
    assert rows['XRPUSDT']['feasibility']['BUY']['max_net_rr']>3
    assert engine.scan['summary']['groups']['crypto']['evaluated']==5


async def prepared_inputs(rig,symbols):
    engine,_,market,_,_=rig
    ranked=[]
    for i,symbol in enumerate(symbols):
        data={name:snapshot(await market.candles(symbol,interval)) for name,interval in [('m5','5m'),('h1','1h')]}
        data.update(instrument={'asset_class':'index_cfd' if symbol.startswith('CAP:') else 'crypto'},
            directions=['BUY'],direction_candidates={'BUY':['breakout'],'SHORT':[]})
        ranked.append((symbol,data,['breakout']))
    rows=[{'symbol':s,'status':'CANDIDAT','reason':'breakout'} for s in symbols]
    return ranked,rows,market.now//300000*300000-1


@pytest.mark.asyncio
async def test_unknown_family_cannot_take_slot_from_verified_feasible_market(rig,monkeypatch):
    engine,_,market,_,_=rig
    cfg=engine.settings().model_copy(update={'auto_scanner':True,'cfd_enabled':True,'ai_shortlist':1})
    ranked,rows,expected=await prepared_inputs(rig,['CAP:UNKNOWN','BTCUSDT'])
    def bound(data,q,rules,cfg,short=False):
        return {'status':'unknown' if q.symbol.startswith('CAP:') else 'feasible','reason':'Fixture bound.'}
    monkeypatch.setattr('app.engine.market_feasibility',bound)
    selected=await engine._prepare_entry_candidates(ranked,rows,cfg,expected,True)
    assert [r[0] for r in selected]==['BTCUSDT']
    assert rows[0]['status']=='CANDIDAT' and rows[0]['feasibility']['BUY']['status']=='unknown'
    assert rows[0]['selection_reason']


@pytest.mark.asyncio
async def test_unknown_bounds_remain_available_after_feasible_candidates(rig,monkeypatch):
    engine,_,_,_,_=rig
    cfg=engine.settings().model_copy(update={'auto_scanner':True,'cfd_enabled':True,'ai_shortlist':2})
    ranked,rows,expected=await prepared_inputs(rig,['CAP:UNKNOWN','BTCUSDT'])
    monkeypatch.setattr('app.engine.market_feasibility',lambda data,q,*args,**kwargs:
        {'status':'unknown' if q.symbol.startswith('CAP:') else 'feasible','reason':'Fixture bound.'})
    selected=await engine._prepare_entry_candidates(ranked,rows,cfg,expected,True)
    assert [r[0] for r in selected]==['BTCUSDT','CAP:UNKNOWN']


@pytest.mark.asyncio
async def test_only_demonstrably_impossible_direction_is_removed(rig,monkeypatch):
    engine,_,_,_,_=rig
    cfg=engine.settings().model_copy(update={'cfd_enabled':True})
    ranked,rows,expected=await prepared_inputs(rig,['CAP:INDEX'])
    data=ranked[0][1]
    data['directions']=['BUY','SHORT']
    data['direction_candidates']={'BUY':['breakout'],'SHORT':['retest']}
    monkeypatch.setattr('app.engine.market_feasibility',lambda data,q,*args,short=False,**kwargs:
        {'status':'feasible' if short else 'insufficient_rr','reason':'Fixture directional bound.'})
    selected=await engine._prepare_entry_candidates(ranked,rows,cfg,expected,True)
    assert selected[0][1]['directions']==['SHORT'] and selected[0][2]==['retest']
    assert selected[0][1]['direction_candidates']['BUY']==[]
    assert set(rows[0]['feasibility'])=={'BUY','SHORT'}


@pytest.mark.asyncio
async def test_bad_cfd_metadata_does_not_discard_other_candidates(rig,monkeypatch):
    engine,_,market,_,_=rig
    cfg=engine.settings().model_copy(update={'cfd_enabled':True})
    ranked,rows,expected=await prepared_inputs(rig,['CAP:BAD','BTCUSDT'])
    original=market.rules
    async def rules(symbols):
        if 'CAP:BAD' in symbols:raise MarketError('Fixture invalid contract.')
        return await original(symbols)
    monkeypatch.setattr(market,'rules',rules)
    selected=await engine._prepare_entry_candidates(ranked,rows,cfg,expected,True)
    assert [r[0] for r in selected]==['BTCUSDT']
    assert rows[0]['status']=='INDISPONIBLE' and 'invalid contract' in rows[0]['reason']


@pytest.mark.asyncio
async def test_slow_candidate_is_cancelled_without_hiding_ready_candidate(rig,monkeypatch):
    engine,_,market,_,_=rig
    cfg=engine.settings().model_copy(update={'auto_scanner':True,'ai_shortlist':1})
    ranked,rows,expected=await prepared_inputs(rig,['BTCUSDT','ETHUSDT'])
    original=market.quote;cancelled=asyncio.Event()
    async def quote(symbol):
        if symbol=='BTCUSDT':
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        return await original(symbol)
    monkeypatch.setattr(market,'quote',quote)
    monkeypatch.setattr('app.engine.ENTRY_EVALUATION_SECONDS',.01)
    selected=await engine._prepare_entry_candidates(ranked,rows,cfg,expected,True)
    assert [r[0] for r in selected]==['ETHUSDT'] and cancelled.is_set()
    assert rows[0]['status']=='INDISPONIBLE' and 'Délai de validation' in rows[0]['reason']
