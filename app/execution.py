from __future__ import annotations
import json, math, time, logging
from .storage import utcnow, dumps
from .risk import cfd_fx

log=logging.getLogger('paperpilot')
CFD_FINANCING_METHOD='Estimation prorata du temps écoulé ; ne reproduit pas le rollover exact du courtier.'


def _financing_source(position):
    detail=position.get('detail') or {}
    costs=detail.get('cfd_costs') or {};plan=detail.get('plan') or {}
    return costs.get('financing_source') or plan.get('financing_source') or 'legacy_daily_estimate'

class LiveExecutionEngine:
    """Intentionally unavailable: no signed request or API order endpoint exists in V1."""
    def __init__(self,*args,**kwargs):
        raise RuntimeError('Mode réel verrouillé. Cette version permet uniquement le paper trading.')

class PaperExecutionEngine:
    def __init__(self,store): self.store=store
    @staticmethod
    def _trade_audit(event,p,plan=None):
        """Private structured audit, deliberately excluding balances and credentials."""
        try:
            detail=p.get('detail') or {}; stored_plan=detail.get('plan') or {}
            structure=getattr(plan,'structure_audit',None) if plan is not None else stored_plan.get('structure_audit')
            data={'event':event,'at':utcnow(),'symbol':p['symbol'],'trade_id':p['id'],
                'kind':p.get('kind','spot'),'direction':p.get('direction','LONG'),
                'entry':p['entry'],'stop_loss':p['stop_loss'],'take_profit':p['take_profit'],
                'quantity':p['quantity'],'risk_usdt':p['risk_usdt'],
                'rr':getattr(plan,'rr',None) if plan is not None else stored_plan.get('rr'),
                'risk_pct_used':getattr(plan,'risk_pct_used',None) if plan is not None else stored_plan.get('risk_pct_used'),
                'exit':p.get('exit'),'net_pnl':p.get('net_pnl'),
                'reason':p.get('exit_reason') if event=='CLOSE' else p.get('entry_reason'),
                'structure_audit':structure}
            if p.get('kind')=='cfd':
                data.update(fee_rate=p.get('fee_rate'),financing_daily=p.get('financing_daily'),
                    financing_source=_financing_source(p),financing_method=CFD_FINANCING_METHOD)
            log.info('PAPER_TRADE_AUDIT %s',json.dumps(data,ensure_ascii=False,allow_nan=False))
        except (KeyError,TypeError,ValueError,AttributeError):
            log.warning('PAPER_TRADE_AUDIT unavailable for a recorded trade')
    def buy(self,plan,quote,signal,decision,settings,detail):
        if getattr(plan,'kind','spot')=='cfd':
            return self._cfd_open(plan,quote,signal,decision,settings,detail)
        if quote.symbol!=plan.symbol: raise ValueError('Marché de cotation différent du plan.')
        now=int(time.time()*1000)
        if not 0<=now-quote.time_ms<=15000: raise ValueError('Achat bloqué : cotation périmée.')
        with self.store.transaction():
            if self.store.position(plan.symbol): raise ValueError('Une position est déjà ouverte sur ce marché.')
            if self.store.rows('SELECT id FROM trades WHERE signal=?',(signal,)): raise ValueError('Signal déjà exécuté.')
            cost=plan.entry*plan.quantity+plan.entry_fee
            cash=self.store.get('cash')
            if cost>cash+1e-8: raise ValueError('Capital insuffisant.')
            self.store.conn.execute('''INSERT INTO trades(symbol,opened_at,entry,quantity,stop_loss,take_profit,
                entry_fee,fees,risk_usdt,entry_reason,detail,last_market_at,entry_bid,entry_ask,signal,fee_rate,slippage_bps,last_bid,last_ask)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (plan.symbol,utcnow(),plan.entry,plan.quantity,plan.stop_loss,plan.take_profit,plan.entry_fee,
                 plan.entry_fee,plan.risk_usdt,decision.reason,dumps(detail),quote.time_ms,quote.bid,quote.ask,
                 signal,settings.fee_rate,settings.slippage_bps,quote.bid,quote.ask))
            self.store.set('cash',max(0,cash-cost))
            self.store.event(f'ACHAT PAPER {plan.symbol} : {plan.quantity:g} à {plan.entry:.6g} USDT. Risque estimé {plan.risk_usdt:.2f} USDT.')
            opened=self.store.position(plan.symbol)
        self._trade_audit('OPEN',opened,plan)
        return opened
    def validate_quote(self,quote):
        p=self.store.position(quote.symbol)
        if not p: raise ValueError('Aucune position détenue : vente à découvert impossible.')
        if quote.symbol!=p['symbol']: raise ValueError('Cotation différente de la position détenue.')
        if not all(math.isfinite(v) and v>0 for v in (quote.bid,quote.ask)) or quote.bid>quote.ask: raise ValueError('Cotation incohérente.')
        if not 0<=int(time.time()*1000)-quote.time_ms<=15000: raise ValueError('Cotation périmée : trop ancienne.')
    def sell(self,quote,reason,detail=None,price_cap=None):
        p=self.store.position(quote.symbol)
        if p and p.get('kind')=='cfd':
            return self._cfd_close(quote,reason,detail,price_cap)
        self.validate_quote(quote)
        if not math.isfinite(quote.bid) or quote.bid<=0 or quote.bid>quote.ask: raise ValueError('Cotation de sortie incohérente.')
        if not 0<=int(time.time()*1000)-quote.time_ms<=15000: raise ValueError('Sortie bloquée : cotation périmée.')
        with self.store.transaction():
            p=self.store.position(quote.symbol)
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
            self._trade_audit('CLOSE',{**p,'exit':fill,'net_pnl':net,'exit_reason':reason})
            return net
    def protect(self,quote,allow_take_profit=True,portfolio_quotes=None):
        p=self.store.position(quote.symbol)
        if not p: return None
        if p.get('kind')=='cfd': return self._cfd_protect(quote,allow_take_profit,portfolio_quotes)
        self.validate_quote(quote)
        if quote.bid<=p['stop_loss']: return self.sell(quote,'STOP_LOSS',{'method':'bid observé; gap exécuté au bid courant'})
        if allow_take_profit and quote.bid>=p['take_profit']: return self.sell(quote,'TAKE_PROFIT',{'method':'bid observé; plafond objectif prudent'},price_cap=p['take_profit'])
        self.store.conn.execute('UPDATE trades SET last_market_at=?,last_bid=?,last_ask=? WHERE id=?',(quote.time_ms,quote.bid,quote.ask,p['id']))
        return None
    def reconcile(self,quote,candles,since_time=None):
        """Backfilled M1 bars detect touches; execution is booked NOW, never as a historical live fill."""
        p=self.store.position(quote.symbol)
        if not p: return False
        if p.get('kind')=='cfd': return self._cfd_reconcile(quote,candles,since_time)
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


    def _validate_cfd_quote(self,quote):
        self.validate_quote(quote)
        if getattr(quote,'currency','')!='USD': raise ValueError('Cotation CFD : devise USD requise.')
        if getattr(quote,'market_status','')!='TRADEABLE': raise ValueError('Marché CFD fermé : aucune exécution fictive autorisée.')
        return cfd_fx(quote)

    def _cfd_open(self,plan,quote,signal,decision,settings,detail):
        now=int(time.time()*1000)
        if not getattr(settings,'cfd_enabled',False): raise ValueError('Simulation CFD désactivée.')
        if not 0<=now-quote.time_ms<=15000: raise ValueError('Ouverture CFD bloquée : cotation périmée.')
        if quote.symbol!=plan.symbol or not str(plan.symbol).startswith('CAP:'): raise ValueError('Marché CFD incohérent.')
        if getattr(quote,'market_status','')!='TRADEABLE' or getattr(quote,'currency','')!='USD': raise ValueError('Marché CFD fermé ou devise incompatible.')
        fx_bid,fx_ask=cfd_fx(quote)
        if plan.direction not in {'LONG','SHORT'} or plan.multiplier!=1 or plan.currency!='USD': raise ValueError('Contrat CFD invalide.')
        if decision.action!=('SHORT' if plan.direction=='SHORT' else 'BUY'): raise ValueError('Direction du plan CFD contradictoire.')
        if not all(math.isfinite(v) and v>0 for v in (plan.quantity,plan.entry,plan.stop_loss,plan.take_profit,plan.margin,plan.leverage,plan.fx_open)):
            raise ValueError('Plan CFD invalide.')
        if not 1<=plan.leverage<=5 or not math.isfinite(plan.entry_fee) or plan.entry_fee<0:
            raise ValueError('Levier ou frais CFD invalides.')
        # New CFD plans carry their own commission. Legacy/manual plans retain
        # the previous configured rate; existing persisted rows are never changed.
        fee_rate=getattr(plan,'fee_rate',None)
        if fee_rate is None:fee_rate=settings.fee_rate
        if not isinstance(fee_rate,(int,float)) or not math.isfinite(fee_rate) or not 0<=fee_rate<=.01:
            raise ValueError('Taux de commission CFD invalide.')
        info=dict(detail or {})
        info['cfd_costs']={'fee_rate':fee_rate,'financing_daily':plan.financing_daily,
            'financing_source':getattr(plan,'financing_source',None) or 'legacy_daily_estimate',
            'financing_method':CFD_FINANCING_METHOD}
        with self.store.transaction():
            if self.store.position(plan.symbol): raise ValueError('Une position est déjà ouverte sur ce marché.')
            if self.store.rows('SELECT id FROM trades WHERE signal=?',(signal,)): raise ValueError('Signal déjà exécuté.')
            cost=plan.margin+plan.entry_fee; cash=self.store.get('cash')
            if cost>cash+1e-8: raise ValueError('Marge CFD et commissions : capital insuffisant.')
            self.store.conn.execute("""INSERT INTO trades(symbol,opened_at,entry,quantity,stop_loss,take_profit,
                entry_fee,fees,risk_usdt,entry_reason,detail,last_market_at,entry_bid,entry_ask,signal,fee_rate,slippage_bps,
                kind,direction,margin,leverage,multiplier,currency,fx_open,financing_daily,financing_at,maintenance_margin_pct,last_bid,last_ask,last_fx_bid,last_fx_ask)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (plan.symbol,utcnow(),plan.entry,plan.quantity,plan.stop_loss,plan.take_profit,plan.entry_fee,plan.entry_fee,
                plan.risk_usdt,decision.reason,dumps(info),quote.time_ms,quote.bid,quote.ask,signal,fee_rate,settings.slippage_bps,
                'cfd',plan.direction,plan.margin,plan.leverage,1.,'USD',plan.fx_open,plan.financing_daily,now,plan.maintenance_margin_pct,quote.bid,quote.ask,fx_bid,fx_ask))
            self.store.set('cash',cash-cost)
            self.store.event(f'OUVERTURE CFD PAPER {plan.direction} {plan.symbol} : {plan.quantity:g} unités ; marge {plan.margin:.2f} USDT ; levier {plan.leverage:g}.')
            opened=self.store.position(plan.symbol)
        self._trade_audit('OPEN',opened,plan)
        return opened

    def _cfd_close(self,quote,reason,detail=None,price_cap=None):
        fx_bid,fx_ask=self._validate_cfd_quote(quote)
        with self.store.transaction():
            p=self.store.position(quote.symbol)
            if not p: raise ValueError('Aucune position détenue pour ce marché.')
            short=p['direction']=='SHORT'
            price=quote.ask if short else quote.bid
            if price_cap is not None:
                if not math.isfinite(price_cap) or price_cap<=0: raise ValueError('Plafond CFD invalide.')
                price=max(price,price_cap) if short else min(price,price_cap)
            fill=price*(1+p['slippage_bps']/10000 if short else 1-p['slippage_bps']/10000)
            direction=-1 if short else 1
            gross_usd=direction*(fill-p['entry'])*p['quantity']*p['multiplier']
            gross=gross_usd*(fx_bid if gross_usd>=0 else fx_ask)
            fee=fill*p['quantity']*p['multiplier']*p['fee_rate']*fx_ask
            financing=cfd_financing(p,int(time.time()*1000),fx_ask)
            net=gross-p['entry_fee']-fee-financing
            info=p['detail']; info['exit']=dict(detail or {})
            info['exit'].update(reason=reason,conversion_bid=fx_bid,conversion_ask=fx_ask,gross_pnl_usd=gross_usd,
                financing_usdt=financing,financing_estimate=True,financing_source=_financing_source(p),
                financing_method=CFD_FINANCING_METHOD)
            self.store.conn.execute("""UPDATE trades SET closed_at=?,exit=?,exit_fee=?,fees=?,net_pnl=?,exit_reason=?,
                detail=?,exit_bid=?,exit_ask=?,last_market_at=?,financing_accrued=?,financing_at=? WHERE id=? AND closed_at IS NULL""",
                (utcnow(),fill,fee,p['entry_fee']+fee,net,reason,dumps(info),quote.bid,quote.ask,quote.time_ms,
                financing,int(time.time()*1000),p['id']))
            # No zero clamp: a gap may lose more than collateral; it must remain visible.
            self.store.set('cash',self.store.get('cash')+p['margin']+gross-fee-financing)
            self.store.event(f'CLOTURE CFD PAPER {p["symbol"]} : {reason} ; résultat net {net:+.2f} USDT.', 'warning' if net<0 else 'info')
            self._trade_audit('CLOSE',{**p,'exit':fill,'net_pnl':net,'exit_reason':reason})
            return net

    def _cfd_protect(self,quote,allow_take_profit=True,portfolio_quotes=None):
        p=self.store.position(quote.symbol)
        # Closed markets do not produce executable prices. Financing still appears in valuation.
        if getattr(quote,'market_status','')!='TRADEABLE': return None
        fx_bid,fx_ask=self._validate_cfd_quote(quote)
        short=p['direction']=='SHORT'; price=quote.ask if short else quote.bid
        observed=dict(portfolio_quotes or {}); observed[quote.symbol]=quote
        with self.store.lock:
            values=portfolio_valuations(self.store,observed)
            equity=self.store.get('cash')+sum(row['equity_add'] for row in values)
            maintenance=sum(row.get('margin',0.)*row.get('maintenance_margin_pct',50.)/100 for row in values if row.get('kind')=='cfd')
        if equity<=maintenance:
            return self._cfd_close(quote,'MARGIN_STOP_OUT',{'method':'Equity sous marge de maintenance ; cotation exécutable courante.'})
        if (price>=p['stop_loss'] if short else price<=p['stop_loss']):
            return self._cfd_close(quote,'STOP_LOSS',{'method':'Ask observé pour SHORT, bid pour LONG ; gap au cours courant.'})
        if allow_take_profit and (price<=p['take_profit'] if short else price>=p['take_profit']):
            return self._cfd_close(quote,'TAKE_PROFIT',{'method':'Objectif plafonné prudemment.'},price_cap=p['take_profit'])
        now=int(time.time()*1000)
        opened=int(__import__('datetime').datetime.fromisoformat(p['opened_at']).timestamp()*1000)
        if now-opened>=86400000:
            return self._cfd_close(quote,'DUREE_MAX_24H',{'method':'Sortie au premier cours négociable après 24 h ; financement estimé.'})
        accrued=cfd_financing(p,now,fx_ask)
        self.store.conn.execute('UPDATE trades SET last_market_at=?,financing_accrued=?,financing_at=?,last_bid=?,last_ask=?,last_fx_bid=?,last_fx_ask=? WHERE id=?',
            (quote.time_ms,accrued,now,quote.bid,quote.ask,fx_bid,fx_ask,p['id']))
        return None

    def _cfd_reconcile(self,quote,candles,since_time=None):
        p=self.store.position(quote.symbol)
        if getattr(quote,'market_status','')!='TRADEABLE': return False
        self._validate_cfd_quote(quote)
        since=p['last_market_at'] if since_time is None else since_time
        short=p['direction']=='SHORT'
        opened=int(__import__('datetime').datetime.fromisoformat(p['opened_at']).timestamp()*1000)
        for c in candles:
            if c.close_time<=since: continue
            stop=c.high>=p['stop_loss'] if short else c.low<=p['stop_loss']
            target=c.low<=p['take_profit'] if short else c.high>=p['take_profit']
            if stop or target:
                # Envelope bars conservatively include bid and ask extremes; stop wins ties.
                cap=(max(c.high,p['stop_loss']) if short else min(c.low,p['stop_loss'])) if stop else p['take_profit']
                self._cfd_close(quote,'STOP_GAP_APPROXIMATION' if stop else 'OBJECTIF_GAP_APPROXIMATION',
                    {'uncertain':True,'both_touched':bool(stop and target),'partial_bar':c.open_time<max(opened,since),
                     'method':'Enveloppe M1 après interruption ; stop prioritaire ; prix courant ou borne défavorable.',
                     'candle':{'open_time':c.open_time,'close_time':c.close_time,'low':c.low,'high':c.high}},price_cap=cap)
                return True
        return False


def cfd_financing(p,now_ms,fx_cost=None):
    """Prorated calendar estimate, not the broker's discrete overnight rollover.

    Accrued amounts persist so restarting cannot forgive recorded financing.
    """
    since=p.get('financing_at',0)
    if not since: since=int(__import__('datetime').datetime.fromisoformat(p['opened_at']).timestamp()*1000)
    elapsed=max(0,now_ms-since)/86400000
    fx=max(p.get('fx_open',1.),fx_cost or p.get('fx_open',1.))
    return p.get('financing_accrued',0.)+p['entry']*p['quantity']*p.get('multiplier',1.)*fx*p.get('financing_daily',0.)*elapsed


def cfd_valuation(p,quote=None,now_ms=None):
    """Margin is collateral, never a stock holding or sale proceeds."""
    short=p['direction']=='SHORT'
    if quote:
        fx_bid,fx_ask=cfd_fx(quote); market=quote.ask if short else quote.bid
    else:
        fx_bid=p.get('last_fx_bid') or p.get('fx_open',1.)
        fx_ask=p.get('last_fx_ask') or p.get('fx_open',1.)
        market=(p.get('last_ask') if short else p.get('last_bid')) or p['entry']
    fill=market*(1+p['slippage_bps']/10000 if short else 1-p['slippage_bps']/10000)
    gross_usd=(-1 if short else 1)*(fill-p['entry'])*p['quantity']*p.get('multiplier',1.)
    gross=gross_usd*(fx_bid if gross_usd>=0 else fx_ask)
    fee=fill*p['quantity']*p.get('multiplier',1.)*p['fee_rate']*fx_ask
    financing=cfd_financing(p,int(time.time()*1000) if now_ms is None else now_ms,fx_ask)
    return dict(current_price=market,valuation_stale=quote is None or not 0<=int(time.time()*1000)-quote.time_ms<=15000,
        valuation_at=getattr(quote,'time_ms',p.get('last_market_at')),financing_usdt=financing,unrealized_pnl=gross-fee-financing-p['entry_fee'],
        equity_add=p['margin']+gross-fee-financing,margin_used=p['margin'],exit_fee_estimate=fee,
        financing_estimate=True,financing_source=_financing_source(p),financing_method=CFD_FINANCING_METHOD)



def portfolio_valuations(store,quotes):
    """Value every holding once. Missing quotes use labelled last observations.

    No execution or account mutation occurs here. Native CFD prices are USD;
    collateral, converted PnL and portfolio totals are USDT.
    """
    positions=[]; now=int(time.time()*1000)
    for p in store.positions():
        q=quotes.get(p['symbol'])
        if p.get('kind')=='cfd':
            p.update(cfd_valuation(p,q,now_ms=now))
        else:
            market=q.bid if q else p.get('last_bid') or p['entry']
            net_exit=market*(1-p['slippage_bps']/10000)*(1-p['fee_rate'])
            p.update(current_price=market,unrealized_pnl=p['quantity']*(net_exit-p['entry'])-p['entry_fee'],
                equity_add=p['quantity']*net_exit,margin_used=0.,financing_usdt=0.,
                valuation_stale=q is None or not 0<=now-q.time_ms<=15000,
                valuation_at=q.time_ms if q else p.get('last_market_at'))
        positions.append(p)
    return positions

