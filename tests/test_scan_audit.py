import json
import pytest
from app.scan_audit import scan_summary
from test_engine import rig


def test_scan_summary_distinguishes_seen_from_analysable_and_candidates():
    scan={'at':'fixture','rows':[
        {'symbol':'BTCUSDT','status':'ATTENTE','reason':'H1 sous EMA21'},
        {'symbol':'CAP:US100','status':'CANDIDAT','asset_class':'index_cfd'},
        {'symbol':'CAP:EURUSD','status':'INDISPONIBLE','reason':'Bougie M5 absente (reçu=1, attendu=2).'},
        {'symbol':'CAP:GOLD','status':'ATTENTE','reason':'ATR hors plage'}]}
    report=scan_summary(scan)
    assert report['total']==4 and report['valid']==3
    assert report['groups']['cfd']['seen']==3
    assert report['groups']['cfd']['valid']==2
    assert report['groups']['cfd']['candidates']==1
    assert report['groups']['cfd']['unavailable']==1
    assert report['families']['index_cfd']['candidates']==1
    assert report['cfd_examples'][1]['reason'].startswith('Bougie M5')


def test_scan_summary_is_bounded_and_does_not_copy_unrelated_fields():
    scan={'api_key':'fixture-private','rows':[{'symbol':f'CAP:FIX{i}','status':'INDISPONIBLE',
        'reason':'X'*1000,'credentials':'fixture-private'} for i in range(100)]}
    report=scan_summary(scan)
    assert len(report['cfd_examples'])==8
    assert all(len(row['reason'])<=500 for row in report['cfd_examples'])
    assert 'fixture-private' not in json.dumps(report)


@pytest.mark.asyncio
async def test_all_unavailable_scan_keeps_current_per_market_reasons(rig):
    engine,store,market,ai,_=rig
    market.stale=True
    await engine._analyze()
    assert not ai.contexts
    assert len(engine.scan['rows'])==3
    assert engine.scan['summary']['valid']==0
    saved=json.loads(store.rows('SELECT detail FROM decisions')[-1]['detail'])
    assert len(saved['scanner']['rows'])==3
    assert all('reçu=' in row['reason'] for row in saved['scanner']['rows'])
