from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import math, time
from .structure import validate_structural_plan, StructureRejected

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
    structure_audit: dict = field(default_factory=dict)
    risk_pct_used: float = 0.
    fee_rate: float | None = None
    financing_source: str | None = None

def D(v): return Decimal(str(v))
def floor_step(value,step):
    if step<=0: raise RiskRejected('Précision de quantité invalide.')
    return float((D(value)/D(step)).to_integral_value(rounding=ROUND_DOWN)*D(step))

class RiskEngine:
    @staticmethod
    def plan(decision,quote,rules,settings,cash,equity,position=None,paused=False,day_blocked=False,atr=None,now_ms=None,structure_context=None,portfolio_positions=None,portfolio_quotes=None,risk_budget_pct=None):
        if str(decision.symbol).startswith('CAP:'):
            return RiskEngine.cfd_plan(decision,quote,rules,settings,cash,equity,position,paused,day_blocked,atr,now_ms,structure_context,portfolio_positions,portfolio_quotes,risk_budget_pct)
        def reject(reason): raise RiskRejected(reason)
        if decision.action!='BUY': reject('Proposition sans achat.')
        positions=admit_portfolio(decision,settings,position,portfolio_positions)
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
        validate_entry_zone(decision,quote,atr,settings)
        slip=settings.slippage_bps/10000
        entry=quote.ask*(1+slip)
        stop=floor_step(decision.stop_loss,rules.tick_size)
        target=floor_step(decision.take_profit,rules.tick_size)
        if not 0<stop<entry<target: reject('Stop/objectif invalides après arrondi Binance.')
        if entry-stop<atr*.35: reject('Stop trop proche du bruit de marché (0,35 ATR minimum).')
        try: structure=validate_structural_plan(structure_context,entry,stop,target,now_ms=now_ms)
        except StructureRejected as exc: reject(str(exc))
        risk_pct=trade_risk_pct(decision,settings,equity,positions,portfolio_quotes,now_ms,risk_budget_pct)
        loss=entry*(1+settings.fee_rate)-stop*(1-slip)*(1-settings.fee_rate)
        reward=target*(1-slip)*(1-settings.fee_rate)-entry*(1+settings.fee_rate)
        rr=reward/loss
        if reward<=0 or rr+1e-10<settings.min_rr: reject(f'Ratio net insuffisant : {rr:.2f}R.')
        max_by_risk=equity*risk_pct/100/loss
        # Reserve liquidity for the other configured slots; a narrow Spot stop
        # must not consume the whole wallet before the second signal arrives.
        allocation=min(cash,equity/settings.max_positions)
        max_by_cash=allocation/(entry*(1+settings.fee_rate))
        qty=floor_step(min(max_by_risk,max_by_cash,rules.max_qty),rules.step_size)
        if qty<=0 or qty+1e-12<rules.min_qty: reject('Quantité sous le minimum Binance ou capital insuffisant.')
        notional=qty*entry
        if notional<rules.min_notional or (rules.max_notional and notional>rules.max_notional): reject('Montant hors limites Binance.')
        fee=notional*settings.fee_rate
        if notional+fee>cash+1e-8: reject('Capital insuffisant, commissions incluses.')
        if qty*loss>equity*risk_pct/100+1e-8: reject('Risque maximal dépassé.')
        return OrderPlan(decision.symbol,qty,entry,stop,target,fee,qty*loss,rr,structure_audit=structure,risk_pct_used=qty*loss/equity*100)


    @staticmethod
    def cfd_plan(decision,quote,rules,settings,cash,equity,position=None,paused=False,day_blocked=False,atr=None,now_ms=None,structure_context=None,portfolio_positions=None,portfolio_quotes=None,risk_budget_pct=None):
        def reject(reason): raise RiskRejected(reason)
        if not getattr(settings,'cfd_enabled',False): reject('Simulation CFD désactivée.')
        if decision.action not in {'BUY','SHORT'}: reject('Proposition sans ouverture CFD.')
        short=decision.action=='SHORT'; direction=-1 if short else 1
        if short and (not getattr(settings,'allow_short',False) or not getattr(rules,'allow_short',False)):
            reject('Vente à découvert CFD non autorisée pour ce marché.')
        positions=admit_portfolio(decision,settings,position,portfolio_positions)
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
        if not math.isfinite(settings.risk_pct) or not 0<settings.risk_pct<=1.4: reject('Risque maximal CFD limité à 1,4 %.')
        if quote.bid>quote.ask: reject('Cotation CFD incohérente.')
        if (quote.ask-quote.bid)/quote.bid*10000>settings.max_spread_bps: reject('Spread CFD trop élevé.')
        if short:
            if not decision.take_profit<decision.entry<decision.invalidation<=decision.stop_loss: reject('Niveaux SHORT contradictoires.')
        elif not decision.stop_loss<=decision.invalidation<decision.entry<decision.take_profit: reject('Niveaux LONG contradictoires.')
        if not decision.confirmation_met or decision.setup=='none': reject('Confirmation de configuration absente.')
        if atr/quote.ask*100>settings.max_atr_pct: reject('Volatilité CFD excessive.')
        reference=quote.bid if short else quote.ask
        validate_entry_zone(decision,quote,atr,settings,short=short)
        tick=rules.tick_size
        if not math.isfinite(tick) or tick<=0: reject('Pas de prix CFD inconnu.')
        rounding=ROUND_UP if short else ROUND_DOWN
        def rounded(price): return float((D(price)/D(tick)).to_integral_value(rounding=rounding)*D(tick))
        stop,target=rounded(decision.stop_loss),rounded(decision.take_profit)
        slip=settings.slippage_bps/10000
        fee_rate=getattr(settings,'cfd_fee_rate',0.)
        if not isinstance(fee_rate,(int,float)) or not math.isfinite(fee_rate) or not 0<=slip<=.01 or not 0<=fee_rate<=.01:
            reject('Frais/glissement CFD invalides.')
        entry=reference*(1-slip if short else 1+slip)
        distance=direction*(entry-stop)
        if distance<.35*atr or direction*(target-entry)<=0: reject('Stop CFD incompatible avec le bruit ATR ou objectif invalide.')
        try: structure=validate_structural_plan(structure_context,entry,stop,target,short=short,now_ms=now_ms)
        except StructureRejected as exc: reject(str(exc))
        risk_pct=trade_risk_pct(decision,settings,equity,positions,portfolio_quotes,now_ms,risk_budget_pct)
        margin_pct=getattr(rules,'min_margin_pct',0)
        requested=getattr(settings,'cfd_leverage',1)
        if not math.isfinite(margin_pct) or not 0<margin_pct<=100: reject('Marge fournisseur inconnue ou incompatible.')
        if not math.isfinite(requested) or not 1<=requested<=5: reject('Levier CFD limité de 1 à 5.')
        leverage=min(requested,100/margin_pct)
        provider_financing=getattr(rules,'financing_short_daily' if short else 'financing_long_daily',None)
        if provider_financing is not None:
            # A verified signed provider debit can be below the conservative
            # configured fallback. Positive provider credits are already ignored.
            financing=provider_financing
            financing_source=getattr(rules,'financing_source',None) or 'provider_directional_daily_estimate'
        else:
            financing=max(getattr(settings,'cfd_financing_daily',.001),getattr(rules,'financing_daily',0))
            financing_source='configured_daily_estimate'
        maintenance=getattr(rules,'maintenance_margin_pct',50.)
        if not isinstance(financing,(int,float)) or not math.isfinite(financing) or not 0<=financing<=.05 or not math.isfinite(maintenance) or not 0<maintenance<=100:
            reject('Financement ou marge de maintenance CFD invalide.')
        stop_fill=stop*(1+slip if short else 1-slip)
        target_fill=target*(1+slip if short else 1-slip)
        fee_per_entry=entry*fee_rate*fx_ask
        carry=entry*financing*fx_ask # One-day reserve; execution exits after 24h at next tradeable quote.
        loss=direction*(entry-stop_fill)*fx_ask+fee_per_entry+stop_fill*fee_rate*fx_ask+carry
        reward=direction*(target_fill-entry)*fx_bid-fee_per_entry-target_fill*fee_rate*fx_ask-carry
        if loss<=0 or reward<=0 or reward/loss+1e-10<settings.min_rr: reject('Ratio CFD net insuffisant après frais et financement estimé.')
        margin_unit=entry*fx_ask/leverage
        filters=[rules.max_qty,rules.min_qty,rules.step_size,rules.min_notional,rules.max_notional]
        if not all(math.isfinite(x) and x>=0 for x in filters) or rules.max_qty<=0 or rules.step_size<=0:
            reject('Contraintes de quantité CFD invalides.')
        allocation=min(cash,equity/settings.max_positions)
        max_qty=min(equity*risk_pct/100/loss,allocation/(margin_unit+fee_per_entry),rules.max_qty)
        qty=floor_step(max_qty,rules.step_size)
        if qty<=0 or qty+1e-12<rules.min_qty: reject('Quantité CFD minimale incompatible avec le capital/risque.')
        notional=qty*entry
        if notional<rules.min_notional or (rules.max_notional and notional>rules.max_notional): reject('Montant CFD hors limites fournisseur.')
        margin=qty*margin_unit; fee=qty*fee_per_entry
        if margin+fee>cash+1e-8 or qty*loss>equity*risk_pct/100+1e-8: reject('Capital ou risque CFD dépassé.')
        return OrderPlan(decision.symbol,qty,entry,stop,target,fee,qty*loss,reward/loss,
            kind='cfd',direction='SHORT' if short else 'LONG',margin=margin,leverage=leverage,
            multiplier=1.,currency='USD',fx_open=fx_ask,financing_daily=financing,maintenance_margin_pct=maintenance,
            structure_audit=structure,risk_pct_used=qty*loss/equity*100,fee_rate=fee_rate,financing_source=financing_source)


def cfd_fx(quote):
    fallback=getattr(quote,'conversion_rate',1.)
    bid=getattr(quote,'conversion_bid',None)
    ask=getattr(quote,'conversion_ask',None)
    bid=fallback if bid is None else bid
    ask=fallback if ask is None else ask
    if not all(isinstance(v,(int,float)) and math.isfinite(v) and v>0 for v in (bid,ask)) or bid>ask:
        raise RiskRejected('Conversion USD/USDT invalide.')
    return bid,ask



def validate_entry_zone(decision,quote,atr,settings,short=False):
    """Accept a bounded fresh entry, never repair a broken structural trade plan.

    A cheaper LONG / higher SHORT may improve net reward/risk, but must not have
    crossed structural invalidation. The exit side detects an invalidation even
    when the spread or adverse fill would put the entry back above/below it.
    """
    if (quote.ask>=decision.invalidation if short else quote.bid<=decision.invalidation):
        raise RiskRejected('Invalidation structurelle touchée ou franchie : entrée annulée.')
    coefficient=getattr(settings,'entry_tolerance_atr',.75)
    if not math.isfinite(coefficient) or not .25<=coefficient<=1.0:
        raise RiskRejected('Paramètre de zone d’entrée invalide (0,25 à 1 ATR).')
    reference=quote.bid if short else quote.ask
    distance=abs(reference-decision.entry)
    tolerance=max(decision.entry*.001,atr*coefficient)
    if distance>tolerance+max(decision.entry,atr)*1e-12:
        drift_pct=distance/decision.entry*100
        tolerance_pct=tolerance/decision.entry*100
        raise RiskRejected(f'Cours déplacé hors zone d’entrée : écart {drift_pct:.4f} % > tolérance {tolerance_pct:.4f} % ({coefficient:g} ATR ou 0,1 % minimum).')


def admit_portfolio(decision,settings,position=None,portfolio_positions=None):
    if portfolio_positions is None:
        positions=list(position) if isinstance(position,(list,tuple)) else [position] if position else []
    else: positions=list(portfolio_positions)
    maximum=getattr(settings,'max_positions',3)
    if not isinstance(maximum,int) or not 1<=maximum<=6:raise RiskRejected('Nombre maximal de positions invalide.')
    if len(positions)>=maximum:raise RiskRejected('Nombre maximal de positions ouvertes atteint.')
    if any(p.get('symbol')==decision.symbol for p in positions):
        raise RiskRejected('Position déjà ouverte sur ce marché : renforcement, moyennage et couverture interdits.')
    return positions


def trade_risk_pct(decision,settings,equity,positions,quotes=None,now_ms=None,budget=None):
    if not math.isfinite(settings.min_rr) or settings.min_rr<2:raise RiskRejected('Ratio net minimal de sécurité : 2R.')
    desired=getattr(decision,'risk_pct',None)
    desired=settings.risk_pct if desired is None else desired
    per_trade=getattr(settings,'max_trade_risk_pct',1.4)
    portfolio=getattr(settings,'max_portfolio_risk_pct',2.)
    if not all(isinstance(x,(int,float)) and math.isfinite(x) and x>0 for x in (desired,per_trade,portfolio,equity)):
        raise RiskRejected('Budget de risque invalide.')
    if desired>1.4 or per_trade>1.4 or portfolio>4:
        raise RiskRejected('Plafond de risque de sécurité dépassé.')
    if desired>per_trade+1e-12:raise RiskRejected('Risque proposé supérieur au plafond par trade.')
    remaining=equity*portfolio/100-portfolio_remaining_risk(positions,quotes,now_ms)
    if remaining<=0 or not math.isfinite(remaining):raise RiskRejected('Budget de risque total du portefeuille épuisé ou non vérifiable.')
    allowed=min(desired,per_trade,remaining/equity*100)
    if budget is not None:
        if not isinstance(budget,(int,float)) or not math.isfinite(budget) or budget<=0:
            raise RiskRejected('Budget de risque restant nul ou invalide.')
        allowed=min(allowed,budget)
    return allowed


def remaining_position_risk(position,quote=None,now_ms=None):
    """Conservative additional mark-to-stop exposure, never net winning positions.

    Current mark is gross exit-side value; stop includes adverse slippage, exit
    fees and another day of estimated CFD financing. Entry fees are already paid.
    No quote or an already breached stop gives infinity to block fresh entries.
    """
    now_ms=int(time.time()*1000) if now_ms is None else now_ms
    try:
        if quote is None or quote.symbol!=position['symbol'] or not 0<=now_ms-quote.time_ms<=15000:return math.inf
        quantity=float(position['quantity']);stop=float(position['stop_loss'])
        slip=float(position.get('slippage_bps',0))/10000;fee=float(position.get('fee_rate',0))
        values=[quantity,stop,quote.bid,quote.ask]
        if any(not math.isfinite(v) or v<=0 for v in values) or quote.bid>quote.ask or not 0<=slip<=.01 or not 0<=fee<=.01:return math.inf
        if position.get('kind','spot')=='cfd':
            if getattr(quote,'currency',None)!='USD' or float(position.get('multiplier',1))!=1:return math.inf
            short=position.get('direction')=='SHORT';sign=-1 if short else 1
            reference=quote.ask if short else quote.bid
            if sign*(reference-stop)<=0:return math.inf
            fx_bid,fx_ask=cfd_fx(quote)
            fill=stop*(1+slip if short else 1-slip)
            financing=float(position.get('financing_daily',0));entry=float(position['entry'])
            if not math.isfinite(financing) or not 0<=financing<=.05:return math.inf
            return max(0.,quantity*(sign*(reference-fill)+fill*fee+entry*financing)*fx_ask)
        if quote.bid<=stop:return math.inf
        return max(0.,quantity*(quote.bid-stop*(1-slip)*(1-fee)))
    except (KeyError,ValueError,TypeError,AttributeError,RiskRejected):return math.inf


def portfolio_remaining_risk(positions,quotes=None,now_ms=None):
    quotes=quotes or {}
    return sum(remaining_position_risk(p,quotes.get(p['symbol']),now_ms) for p in positions)

