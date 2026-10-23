from __future__ import annotations
import json, math, time
from .storage import utcnow, dumps

class LiveExecutionEngine:
    """Intentionally unavailable: no signed request or API order endpoint exists in V1."""
    def __init__(self,*args,**kwargs):
        raise RuntimeError('Mode réel verrouillé. Cette version permet uniquement le paper trading.')

class PaperExecutionEngine:
    def __init__(self,store): self.store=store
    def buy(self,plan,quote,signal,decision,settings,detail):
        now=int(time.time()*1000)
        if not 0<=now-quote.time_ms<=15000: raise ValueError('Achat bloqué : cotation périmée.')
        with self.store.transaction():
            if self.store.position(): raise ValueError('Une position est déjà ouverte.')
            if self.store.rows('SELECT id FROM trades WHERE signal=?',(signal,)): raise ValueError('Signal déjà exécuté.')
            cost=plan.entry*plan.quantity+plan.entry_fee
            cash=self.store.get('cash')
            if cost>cash+1e-8: raise ValueError('Capital insuffisant.')
            self.store.conn.execute('''INSERT INTO trades(symbol,opened_at,entry,quantity,stop_loss,take_profit,
                entry_fee,fees,risk_usdt,entry_reason,detail,last_market_at,entry_bid,entry_ask,signal,fee_rate,slippage_bps)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (plan.symbol,utcnow(),plan.entry,plan.quantity,plan.stop_loss,plan.take_profit,plan.entry_fee,
                 plan.entry_fee,plan.risk_usdt,decision.reason,dumps(detail),quote.time_ms,quote.bid,quote.ask,
                 signal,settings.fee_rate,settings.slippage_bps))
            self.store.set('cash',max(0,cash-cost))
            self.store.event(f'ACHAT PAPER {plan.symbol} : {plan.quantity:g} à {plan.entry:.6g} USDT. Risque estimé {plan.risk_usdt:.2f} USDT.')
        return self.store.position()
    def validate_quote(self,quote):
        p=self.store.position()
        if not p: raise ValueError('Aucune position détenue : vente à découvert impossible.')
        if quote.symbol!=p['symbol']: raise ValueError('Cotation différente de la position détenue.')
        if not all(math.isfinite(v) and v>0 for v in (quote.bid,quote.ask)) or quote.bid>quote.ask: raise ValueError('Cotation incohérente.')
        if not 0<=int(time.time()*1000)-quote.time_ms<=15000: raise ValueError('Cotation périmée : trop ancienne.')
    def sell(self,quote,reason,detail=None,price_cap=None):
        self.validate_quote(quote)
        if not math.isfinite(quote.bid) or quote.bid<=0 or quote.bid>quote.ask: raise ValueError('Cotation de sortie incohérente.')
        if not 0<=int(time.time()*1000)-quote.time_ms<=15000: raise ValueError('Sortie bloquée : cotation périmée.')
        with self.store.transaction():
            p=self.store.position()
            if not p: raise ValueError('Aucune position détenue : vente à découvert impossible.')
            if quote.symbol!=p['symbol']: raise ValueError('Marché de sortie différent de la position.')
            price=min(quote.bid,price_cap) if price_cap is not None else quote.bid
            fill=price*(1-p['slippage_bps']/10000)
            proceeds=fill*p['quantity']
            fee=proceeds*p['fee_rate']
            net=proceeds-fee-p['quantity']*p['entry']-p['entry_fee']
            info=p['detail']; info['exit']=detail or {}; info['exit']['reason']=reason
            self.store.conn.execute('''UPDATE trades SET closed_at=?,exit=?,exit_fee=?,fees=?,net_pnl=?,exit_reason=?,
                  detail=?,exit_bid=?,exit_ask=?,last_market_at=? WHERE id=? AND closed_at IS NULL''',
                  (utcnow(),fill,fee,p['entry_fee']+fee,net,reason,dumps(info),quote.bid,quote.ask,quote.time_ms,p['id']))
            self.store.set('cash',self.store.get('cash')+proceeds-fee)
            self.store.event(f'VENTE PAPER {p["symbol"]} : {reason} ; résultat net {net:+.2f} USDT.', 'warning' if net<0 else 'info')
            return net
    def protect(self,quote):
        p=self.store.position()
        if not p: return None
        self.validate_quote(quote)
        if quote.bid<=p['stop_loss']: return self.sell(quote,'STOP_LOSS',{'method':'bid observé; gap exécuté au bid courant'})
        if quote.bid>=p['take_profit']: return self.sell(quote,'TAKE_PROFIT',{'method':'bid observé; plafond objectif prudent'},price_cap=p['take_profit'])
        self.store.conn.execute('UPDATE trades SET last_market_at=? WHERE id=?',(quote.time_ms,p['id']))
        return None
    def reconcile(self,quote,candles,since_time=None):
        """Backfilled M1 bars detect touches; execution is booked NOW, never as a historical live fill."""
        p=self.store.position()
        if not p: return False
        since=p['last_market_at'] if since_time is None else since_time
        opened=int(__import__('datetime').datetime.fromisoformat(p['opened_at']).timestamp()*1000)
        for c in candles:
            if c.close_time<=since: continue
            stop=c.low<=p['stop_loss']; target=c.high>=p['take_profit']
            # The bar containing the last observation includes an unknown pre-gap portion.
            partial=c.open_time<max(opened,since)
            if stop or target:
                reason='STOP_GAP_APPROXIMATION' if stop else 'OBJECTIF_GAP_APPROXIMATION'
                cap=min(c.low,p['stop_loss']) if stop else p['take_profit']
                self.sell(quote,reason,{'uncertain':True,'method':'M1 après interruption ; stop prioritaire ; exécution enregistrée à la reprise au moins favorable du bid courant et du niveau prudent',
                    'candle':{'open_time':c.open_time,'close_time':c.close_time,'low':c.low,'high':c.high},
                    'both_touched':bool(stop and target),'partial_bar':partial},price_cap=cap)
                return True
        return False
