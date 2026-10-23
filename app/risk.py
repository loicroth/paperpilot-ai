from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
import math, time

class RiskRejected(ValueError): pass
@dataclass
class OrderPlan:
    symbol: str
    quantity: float
    entry: float
    stop_loss: float
    take_profit: float
    entry_fee: float
    risk_usdt: float
    rr: float

def D(v): return Decimal(str(v))
def floor_step(value,step):
    if step<=0: raise RiskRejected('Précision de quantité invalide.')
    return float((D(value)/D(step)).to_integral_value(rounding=ROUND_DOWN)*D(step))

class RiskEngine:
    @staticmethod
    def plan(decision,quote,rules,settings,cash,equity,position=None,paused=False,day_blocked=False,atr=None,now_ms=None):
        def reject(reason): raise RiskRejected(reason)
        if decision.action!='BUY': reject('Proposition sans achat.')
        if position: reject('Une position est déjà ouverte : aucun renforcement autorisé.')
        if paused: reject('Nouvelles entrées en pause.')
        if day_blocked: reject('Limite de perte journalière atteinte : pause jusqu’à demain UTC.')
        if decision.symbol not in settings.symbols or quote.symbol!=decision.symbol or rules.symbol!=decision.symbol:
            reject('Marché non autorisé ou données contradictoires.')
        now_ms=now_ms or int(time.time()*1000)
        if not 0<=now_ms-quote.time_ms<=15000: reject('Cotation trop ancienne ou horodatage futur.')
        values=[cash,equity,quote.bid,quote.ask,decision.entry,decision.stop_loss,decision.take_profit,decision.invalidation]
        if any(v is None or not math.isfinite(v) or v<=0 for v in values): reject('Montant ou prix invalide.')
        if quote.bid>quote.ask: reject('Carnet bid/ask incohérent.')
        if (quote.ask-quote.bid)/quote.bid*10000>settings.max_spread_bps: reject('Spread trop élevé.')
        if not decision.stop_loss<=decision.invalidation<decision.entry<decision.take_profit: reject('Invalidation, stop et objectif contradictoires.')
        if not decision.confirmation_met or decision.setup=='none': reject('Confirmation de configuration absente.')
        if atr is None or not math.isfinite(atr) or atr<=0: reject('ATR indisponible.')
        if atr/quote.ask*100>settings.max_atr_pct: reject('Volatilité excessive.')
        if abs(decision.entry-quote.ask)>max(quote.ask*.001,atr*.25): reject('Cours déplacé depuis l’entrée proposée.')
        slip=settings.slippage_bps/10000
        entry=quote.ask*(1+slip)
        stop=floor_step(decision.stop_loss,rules.tick_size)
        target=floor_step(decision.take_profit,rules.tick_size)
        if not 0<stop<entry<target: reject('Stop/objectif invalides après arrondi Binance.')
        if entry-stop<atr*.35: reject('Stop trop proche du bruit de marché (0,35 ATR minimum).')
        if entry-stop>atr*4: reject('Stop trop éloigné (4 ATR maximum).')
        loss=entry*(1+settings.fee_rate)-stop*(1-slip)*(1-settings.fee_rate)
        reward=target*(1-slip)*(1-settings.fee_rate)-entry*(1+settings.fee_rate)
        rr=reward/loss
        if reward<=0 or rr+1e-10<settings.min_rr: reject(f'Ratio net insuffisant : {rr:.2f}R.')
        max_by_risk=equity*settings.risk_pct/100/loss
        max_by_cash=cash/(entry*(1+settings.fee_rate))
        qty=floor_step(min(max_by_risk,max_by_cash,rules.max_qty),rules.step_size)
        if qty<=0 or qty+1e-12<rules.min_qty: reject('Quantité sous le minimum Binance ou capital insuffisant.')
        notional=qty*entry
        if notional<rules.min_notional or (rules.max_notional and notional>rules.max_notional): reject('Montant hors limites Binance.')
        fee=notional*settings.fee_rate
        if notional+fee>cash+1e-8: reject('Capital insuffisant, commissions incluses.')
        if qty*loss>equity*settings.risk_pct/100+1e-8: reject('Risque maximal dépassé.')
        return OrderPlan(decision.symbol,qty,entry,stop,target,fee,qty*loss,rr)
