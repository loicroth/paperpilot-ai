from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
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
    kind: str = 'spot'
    direction: str = 'LONG'
    margin: float = 0.
    leverage: float = 1.
    multiplier: float = 1.
    currency: str = 'USDT'
    fx_open: float = 1.
    financing_daily: float = 0.
    maintenance_margin_pct: float = 50.

def D(v): return Decimal(str(v))
def floor_step(value,step):
    if step<=0: raise RiskRejected('Précision de quantité invalide.')
    return float((D(value)/D(step)).to_integral_value(rounding=ROUND_DOWN)*D(step))

class RiskEngine:
    @staticmethod
    def plan(decision,quote,rules,settings,cash,equity,position=None,paused=False,day_blocked=False,atr=None,now_ms=None):
        if str(decision.symbol).startswith('CAP:'):
            return RiskEngine.cfd_plan(decision,quote,rules,settings,cash,equity,position,paused,day_blocked,atr,now_ms)
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


    @staticmethod
    def cfd_plan(decision,quote,rules,settings,cash,equity,position=None,paused=False,day_blocked=False,atr=None,now_ms=None):
        def reject(reason): raise RiskRejected(reason)
        if not getattr(settings,'cfd_enabled',False): reject('Simulation CFD désactivée.')
        if decision.action not in {'BUY','SHORT'}: reject('Proposition sans ouverture CFD.')
        short=decision.action=='SHORT'; direction=-1 if short else 1
        if short and (not getattr(settings,'allow_short',False) or not getattr(rules,'allow_short',False)):
            reject('Vente à découvert CFD non autorisée pour ce marché.')
        if position: reject('Une position est déjà ouverte : aucun renforcement autorisé.')
        if paused or day_blocked: reject('Nouvelles entrées en pause ou limite journalière atteinte.')
        if decision.symbol not in settings.symbols or quote.symbol!=decision.symbol or rules.symbol!=decision.symbol:
            reject('Marché non autorisé ou données contradictoires.')
        if getattr(quote,'market_status','')!='TRADEABLE': reject('Marché CFD fermé ou non négociable.')
        if getattr(rules,'currency','')!='USD' or getattr(quote,'currency','')!='USD' or getattr(rules,'multiplier',0)!=1:
            reject('Contrat CFD non pris en charge : devise USD et multiplicateur unitaire requis.')
        now_ms=int(time.time()*1000) if now_ms is None else now_ms
        if not 0<=now_ms-quote.time_ms<=15000: reject('Cotation CFD périmée ou future.')
        fx_bid,fx_ask=cfd_fx(quote)
        prices=[cash,equity,quote.bid,quote.ask,decision.entry,decision.stop_loss,decision.take_profit,decision.invalidation,atr]
        if any(x is None or not math.isfinite(x) or x<=0 for x in prices): reject('Montant, prix ou ATR CFD invalide.')
        if not math.isfinite(settings.risk_pct) or not 0<settings.risk_pct<=1: reject('Risque maximal CFD limité à 1 %.')
        if quote.bid>quote.ask: reject('Cotation CFD incohérente.')
        if (quote.ask-quote.bid)/quote.bid*10000>settings.max_spread_bps: reject('Spread CFD trop élevé.')
        if short:
            if not decision.take_profit<decision.entry<decision.invalidation<=decision.stop_loss: reject('Niveaux SHORT contradictoires.')
        elif not decision.stop_loss<=decision.invalidation<decision.entry<decision.take_profit: reject('Niveaux LONG contradictoires.')
        if not decision.confirmation_met or decision.setup=='none': reject('Confirmation de configuration absente.')
        if atr/quote.ask*100>settings.max_atr_pct: reject('Volatilité CFD excessive.')
        reference=quote.bid if short else quote.ask
        if abs(decision.entry-reference)>max(reference*.001,atr*.25): reject('Cours CFD déplacé depuis la proposition.')
        tick=rules.tick_size
        if not math.isfinite(tick) or tick<=0: reject('Pas de prix CFD inconnu.')
        rounding=ROUND_UP if short else ROUND_DOWN
        def rounded(price): return float((D(price)/D(tick)).to_integral_value(rounding=rounding)*D(tick))
        stop,target=rounded(decision.stop_loss),rounded(decision.take_profit)
        slip=settings.slippage_bps/10000
        if not 0<=slip<=.01 or not 0<=settings.fee_rate<=.01: reject('Frais/glissement CFD invalides.')
        entry=reference*(1-slip if short else 1+slip)
        distance=direction*(entry-stop)
        if not .35*atr<=distance<=4*atr or direction*(target-entry)<=0: reject('Stop CFD incompatible avec le bruit ATR ou objectif invalide.')
        margin_pct=getattr(rules,'min_margin_pct',0)
        requested=getattr(settings,'cfd_leverage',1)
        if not math.isfinite(margin_pct) or not 0<margin_pct<=100: reject('Marge fournisseur inconnue ou incompatible.')
        if not math.isfinite(requested) or not 1<=requested<=5: reject('Levier CFD limité de 1 à 5.')
        leverage=min(requested,100/margin_pct)
        financing=max(getattr(settings,'cfd_financing_daily',.001),getattr(rules,'financing_daily',0))
        maintenance=getattr(rules,'maintenance_margin_pct',50.)
        if not math.isfinite(financing) or not 0<=financing<=.05 or not math.isfinite(maintenance) or not 0<maintenance<=100:
            reject('Financement ou marge de maintenance CFD invalide.')
        stop_fill=stop*(1+slip if short else 1-slip)
        target_fill=target*(1+slip if short else 1-slip)
        fee_per_entry=entry*settings.fee_rate*fx_ask
        carry=entry*financing*fx_ask # One-day reserve; execution exits after 24h at next tradeable quote.
        loss=direction*(entry-stop_fill)*fx_ask+fee_per_entry+stop_fill*settings.fee_rate*fx_ask+carry
        reward=direction*(target_fill-entry)*fx_bid-fee_per_entry-target_fill*settings.fee_rate*fx_ask-carry
        if loss<=0 or reward<=0 or reward/loss+1e-10<settings.min_rr: reject('Ratio CFD net insuffisant après frais et financement estimé.')
        margin_unit=entry*fx_ask/leverage
        filters=[rules.max_qty,rules.min_qty,rules.step_size,rules.min_notional,rules.max_notional]
        if not all(math.isfinite(x) and x>=0 for x in filters) or rules.max_qty<=0 or rules.step_size<=0:
            reject('Contraintes de quantité CFD invalides.')
        max_qty=min(equity*settings.risk_pct/100/loss,cash/(margin_unit+fee_per_entry),rules.max_qty)
        qty=floor_step(max_qty,rules.step_size)
        if qty<=0 or qty+1e-12<rules.min_qty: reject('Quantité CFD minimale incompatible avec le capital/risque.')
        notional=qty*entry
        if notional<rules.min_notional or (rules.max_notional and notional>rules.max_notional): reject('Montant CFD hors limites fournisseur.')
        margin=qty*margin_unit; fee=qty*fee_per_entry
        if margin+fee>cash+1e-8 or qty*loss>equity*settings.risk_pct/100+1e-8: reject('Capital ou risque CFD dépassé.')
        return OrderPlan(decision.symbol,qty,entry,stop,target,fee,qty*loss,reward/loss,
            kind='cfd',direction='SHORT' if short else 'LONG',margin=margin,leverage=leverage,
            multiplier=1.,currency='USD',fx_open=fx_ask,financing_daily=financing,maintenance_margin_pct=maintenance)


def cfd_fx(quote):
    fallback=getattr(quote,'conversion_rate',1.)
    bid=getattr(quote,'conversion_bid',None)
    ask=getattr(quote,'conversion_ask',None)
    bid=fallback if bid is None else bid
    ask=fallback if ask is None else ask
    if not all(isinstance(v,(int,float)) and math.isfinite(v) and v>0 for v in (bid,ask)) or bid>ask:
        raise RiskRejected('Conversion USD/USDT invalide.')
    return bid,ask
