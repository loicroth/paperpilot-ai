from types import SimpleNamespace
from app.config import Settings
from app.engine import TradingEngine
from app.storage import Store
from test_engine import MarketFixture, AIFixture


def test_upgrade_preserves_cash_mode_risk_and_later_explicit_choice(tmp_path):
    db=Store(tmp_path/'migration.sqlite3')
    old=Settings(strategy_profile='expanded_v1',risk_pct=.7,max_positions=2).model_dump()
    db.set('settings',old);db.set('cash',877.3);db.set('mode','paused')
    env=SimpleNamespace(api_key='',model='fixture')
    def restart():return TradingEngine(db,env,market=MarketFixture(1800000000000),ai=AIFixture())
    restart()
    assert db.get('settings')=={**old,'strategy_profile':'expanded_v2'}
    assert db.get('cash')==877.3 and db.get('mode')=='paused'
    db.set('settings',old);restart()
    assert db.get('settings')==old
    db.close()


def test_upgrade_keeps_explicit_classic_choice(tmp_path):
    db=Store(tmp_path/'classic.sqlite3');settings=Settings(strategy_profile='classic').model_dump()
    db.set('settings',settings)
    TradingEngine(db,SimpleNamespace(api_key='',model='fixture'),market=MarketFixture(1800000000000),ai=AIFixture())
    assert db.get('settings')==settings and db.get('management_v2_migrated')
    db.close()
