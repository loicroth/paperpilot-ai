import json
from app.decision_audit import cfd_decision_record
from app.ai import wait
from test_engine import rig


def saved(mode='entry'):
    return {'context':{'mode':mode,'scanner':{'selected':['CAP:TSLA','PUMPUSDT']},
        'entry_plans':[{'id':'CAP:TSLA:BUY:retest','symbol':'CAP:TSLA'}] if mode=='entry' else [],
        'portfolio':{'open_count':1,'max_positions':3,'risk_known':True,
            'risk_used_usdt':20.,'risk_available_usdt':0.,'api_key':'secret-not-to-log'},
        'entries_paused':False,'password':'secret-not-to-log','markets':{'raw':'secret-not-to-log'}},
        'api_secret':'secret-not-to-log'}


def test_entry_wait_audit_keeps_cfd_selection_and_risk_without_raw_context():
    row={'at':'2026-10-29T14:40:00Z','symbol':'ALL','action':'WAIT','reason':'Attente confirmée.','rejection':None}
    result=cfd_decision_record(row,saved())
    assert result['selected_cfd']==['CAP:TSLA']
    assert result['offered_cfd_plans']==['CAP:TSLA:BUY:retest']
    assert result['portfolio']['risk_available_usdt']==0.
    assert 'secret-not-to-log' not in json.dumps(result)


def test_management_audit_exposes_entry_risk_even_without_entry_call():
    result=cfd_decision_record({'action':'HOLD','symbol':'PUMPUSDT','reason':'Conserver.'},saved('management'))
    assert result['mode']=='management' and result['offered_cfd_plans']==[]
    assert result['selected_cfd']==['CAP:TSLA'] and result['portfolio']['risk_used_usdt']==20.


def test_no_cfd_context_does_not_create_cfd_record():
    assert cfd_decision_record({}, {'context':{'scanner':{'selected':['BTCUSDT']}}}) is None
    assert cfd_decision_record({},None) is None


def test_startup_history_reports_archived_cfd_wait_reason(rig,caplog):
    engine,store,_,_,_=rig
    store.decision(wait('Risque disponible nul.'),None,saved('management'))
    with caplog.at_level('INFO',logger='paperpilot'):
        engine.history_diagnostic()
    records=[r.message for r in caplog.records if r.message.startswith('PAPER_CFD_DECISION_HISTORY ')]
    assert len(records)==1 and 'Risque disponible nul.' in records[0]
    assert 'secret-not-to-log' not in records[0]
