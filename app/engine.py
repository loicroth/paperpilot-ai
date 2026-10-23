from __future__ import annotations
import asyncio, logging, time, json
from datetime import datetime, timezone
from dataclasses import asdict
from .config import Settings
from .market import BinanceClient, MarketError
from .indicators import snapshot, candidates
from .ai import AIClient, Decision, wait, SYSTEM_PROMPT, MAX_OUTPUT_TOKENS
from .risk import RiskEngine, RiskRejected
from .execution import PaperExecutionEngine
from .metrics import statistics
from .storage import utcnow, dumps

log=logging.getLogger('paperpilot')

class TradingEngine:
    def __init__(self,store,env,market=None,ai=None):
        self.store=store; self.env=env
        self.market=market or BinanceClient(env.binance_url)
        self.ai=ai or AIClient(env.api_key,model=env.model)
        self.paper=PaperExecutionEngine(store)
        self.quotes={}; self.rules={}
        self.health=dict(status='DONNÉES INDISPONIBLES',message='Connexion aux données publiques…',market_ok=False,ai_ok=False,last_quote_at=None,last_analysis_at=store.get('last_analysis_at'))
        self.lock=asyncio.Lock(); self.task=None; self.analysis_task=None
        self.started_ms=int(time.time()*1000); self.last_error=None; self.last_snapshot=0.; self.last_analysis_poll=0.; self.closing=False
        self.reconciled=False; self._settings_revision=0; self.critical_error=None
        # Process restarts never silently resume autonomous entries.
        self.store.set('mode','stopped')
        self.store.event('Serveur démarré : analyses arrêtées, protection de toute position conservée.')
    def settings(self): return Settings.model_validate(self.store.get('settings'))
    async def start(self): self.task=asyncio.create_task(self.run())
    async def close(self):
        self.closing=True
        for t in (self.analysis_task,self.task):
            if t: t.cancel()
        await asyncio.gather(*[t for t in (self.analysis_task,self.task) if t],return_exceptions=True)
        await self.market.close()
        if hasattr(self.ai,'close'): await self.ai.close()
    def error(self,message):
        self.critical_error=message
        self.health.update(status='ERREUR',message=message,market_ok=False,ai_ok=False)
        try: self.store.set('mode','paused')
        except Exception: log.error('Impossible de persister la pause ; blocage conservé en mémoire.')
        if message!=self.last_error:
            log.error(message); self.store.event(message,'error'); self.last_error=message
    async def run(self):
        while not self.closing:
            try: await self.tick()
            except asyncio.CancelledError: raise
            except Exception as exc:
                self.health['market_ok']=False
                try: self.error(f'Moteur suspendu : {type(exc).__name__} : {str(exc)[:400]}')
                except Exception: log.exception('Base indisponible : nouvelles entrées bloquées')
            await asyncio.sleep(5)
    def day_state(self,equity,now=None):
        day=(now or datetime.now(timezone.utc)).strftime('%Y-%m-%d')
        state=self.store.get('day_state')
        if not state or state['day']!=day:
            # When midnight was missed use the latest preceding valuation as baseline;
            # choose the higher value to avoid forgiving an observed overnight loss.
            previous=self.store.get('last_equity',equity)
            baseline=max(equity,previous)
            state={'day':day,'start_equity':baseline,'blocked':False,'baseline_approximation': bool(self.store.position())}
        if equity<=state['start_equity']*(1-self.settings().daily_loss_pct/100): state['blocked']=True
        self.store.set('day_state',state)
        return state
    async def reconcile(self,quote):
        p=self.store.position()
        if not p: self.reconciled=True; return
        gap=quote.time_ms-p['last_market_at']
        pending=self.store.get('reconcile_pending')
        if pending and pending['id']!=p['id']: pending=None
        if gap<=15000 and not pending:
            self.reconciled=True; return
        if not pending:
            pending={'id':p['id'],'since':p['last_market_at'],'until':quote.time_ms}
            self.store.set('reconcile_pending',pending)
        self.reconciled=False
        begin=pending['since']//60000*60000
        end=quote.time_ms
        seen=[]; cursor=begin
        # Paginate gaps instead of pretending the most recent 1000 bars cover an outage.
        for _ in range(45):
            bars=await self.market.candles(p['symbol'],'1m',limit=1000,start_time=cursor,end_time=end)
            if not bars: break
            if bars[0].open_time!=cursor: raise MarketError('Réconciliation : premières bougies M1 manquantes.')
            seen.extend(bars); cursor=bars[-1].close_time+1
            if cursor+60000>end: break
        if cursor+60000<=end: raise MarketError('Interruption trop longue ou données M1 incomplètes : entrées bloquées.')
        quote=await self.market.quote(p['symbol'])
        self.quotes[p['symbol']]=quote
        closed=self.paper.reconcile(quote,seen,since_time=pending['since'])
        self.store.event('Reprise après interruption : M1 vérifiées ; toute touche est marquée comme approximation prudente.','warning')
        # Retain an unresolved partial minute until Binance publishes its closed M1.
        # quote protection may advance last_market_at without erasing this gap record.
        if closed or cursor>pending['until']:
            self.store.set('reconcile_pending',None)
            self.reconciled=True
        else:
            self.store.set('reconcile_pending',pending)
            self.reconciled=False
        if not closed:
            # Full current M1 is not yet known; no retrospective favorable fill inferred.
            self.paper.protect(quote)
    async def tick(self):
        cfg=self.settings(); p=self.store.position()
        symbols=list(dict.fromkeys(cfg.symbols+(['BTCUSDT'])+([p['symbol']] if p else [])))
        try:
            now=await self.market.server_time()
            if abs(now-int(time.time()*1000))>5000: raise MarketError('Horloge ordinateur décalée de plus de 5 secondes : synchronisez-la.')
            fresh=await asyncio.gather(*[self.market.quote(s) for s in symbols],return_exceptions=True)
            errors=[]
            for symbol,q in zip(symbols,fresh):
                if isinstance(q,BaseException): errors.append(f'{symbol}: {q}'); continue
                previous=self.quotes.get(symbol)
                if previous and abs(q.bid/previous.bid-1)>.20:
                    errors.append(f'{symbol}: variation incohérente supérieure à 20 %, cotation rejetée.'); continue
                self.quotes[symbol]=q
            now=await self.market.server_time() # quotes are timestamped at response receipt, after the first clock read
            if p:
                q=self.quotes.get(p['symbol'])
                if q and 0<=now-q.time_ms<=15000:
                    async with self.lock:
                        # Recover any touches during a gap BEFORE checking the current quote.
                        try:
                            await self.reconcile(q)
                        except MarketError:
                            # A fresh observed breached stop remains actionable even if
                            # historic M1 is missing. No favorable TP is inferred here.
                            observed=await self.market.quote(p['symbol'])
                            self.quotes[p['symbol']]=observed
                            held=self.store.position()
                            if held and observed.bid<=held['stop_loss']:
                                self.paper.sell(observed,'STOP_REPRISE_DONNEES_INCOMPLETES',{'uncertain':True,'method':'Stop dépassé au bid frais observé, historique indisponible ; aucune exécution normale supposée.'})
                            raise
                        self.paper.protect(self.quotes[p['symbol']])
                else: self.reconciled=False
            else: self.reconciled=True
            if errors: raise MarketError('; '.join(errors))
            now=await self.market.server_time()
            if not all(s in self.quotes and 0<=now-self.quotes[s].time_ms<=15000 for s in symbols): raise MarketError('Cotations périmées.')
            self.health.update(market_ok=True,last_quote_at=utcnow())
            if not self.store.get('btc_baseline'):
                entry=self.quotes['BTCUSDT'].ask*(1+cfg.slippage_bps/10000)
                self.store.set('btc_baseline',{'quantity':self.store.get('initial_capital')/(entry*(1+cfg.fee_rate)),
                    'fee_rate':cfg.fee_rate,'slippage_bps':cfg.slippage_bps})
            stats,_=statistics(self.store,self.quotes)
            ds=self.day_state(stats['equity'])
            self.store.set('last_equity',stats['equity'])
            if time.monotonic()-self.last_snapshot>=30:
                self.store.conn.execute('INSERT INTO equity(at,equity,cash,btc_equity) VALUES(?,?,?,?)',(utcnow(),stats['equity'],stats['cash'],stats['btc_benchmark_equity']))
                self.last_snapshot=time.monotonic()
            mode=self.store.get('mode')
            if self.critical_error:
                self.health.update(status='ERREUR',message=self.critical_error,ai_ok=False)
            elif not self.env.api_key and not getattr(self.ai,'test_enabled',False):
                self.health.update(status='PAUSE',message='IA non configurée : ajoutez OPENAI_API_KEY dans .env et redémarrez.',ai_ok=False)
            elif ds['blocked']:
                self.health.update(status='PAUSE',message='Limite de perte journalière atteinte. Nouvelles entrées bloquées jusqu’à demain UTC.')
            elif self.health.get('ai_error'):
                self.health.update(status='ERREUR',message=self.health['ai_error'],ai_ok=False)
            elif not self.health['ai_ok'] and mode=='running':
                self.health.update(status='PAUSE',message='IA configurée, en attente de vérification à la prochaine clôture M5. Aucun achat avant une réponse valide.')
            else:
                self.health.update(status='ACTIVE' if mode=='running' else 'PAUSE',message={'running':'Surveillance active ; attente d’une nouvelle clôture M5.','paused':'Entrées en pause ; sorties IA et protection surveillées.','stopped':'Analyses arrêtées ; protection surveillée.'}[mode])
            self.last_error=None
            if not self.critical_error and mode in {'running','paused'} and (not self.analysis_task or self.analysis_task.done()) and time.monotonic()-self.last_analysis_poll>=10:
                self.last_analysis_poll=time.monotonic()
                self.analysis_task=asyncio.create_task(self.analyze())
        except MarketError as exc:
            self.health.update(status='DONNÉES INDISPONIBLES',message=str(exc),market_ok=False)
            if str(exc)!=self.last_error:
                self.store.event(str(exc),'error'); self.last_error=str(exc); log.warning(str(exc))
    async def analyze(self):
        try: await self._analyze()
        except asyncio.CancelledError: raise
        except Exception as exc:
            message=f'Analyse annulée : {type(exc).__name__}: {str(exc)[:400]}'
            try: self.error(message)
            except Exception: log.exception('Erreur critique : base indisponible ; entrées bloquées en mémoire.')
            self.health['ai_error']=message
    async def _analyze(self):
        cfg=self.settings(); revision=self._settings_revision
        if not self.health['market_ok']: return
        now=await self.market.server_time()
        expected=(now//300000)*300000-1
        # Never produce a virtual trade on a candle closed before this process started.
        if expected<self.started_ms: return
        key=f'm5:{expected}'
        if self.store.get('last_cycle')==key: return
        async def market_context(symbol):
            m5,h1=await asyncio.gather(self.market.candles(symbol,'5m',150),self.market.candles(symbol,'1h',150))
            if not m5 or m5[-1].close_time!=expected or now-m5[-1].close_time>45000: raise MarketError('Bougie M5 absente ou trop ancienne pour une entrée.')
            expected_h1=(now//3600000)*3600000-1
            if not h1 or h1[-1].close_time!=expected_h1: raise MarketError('Données H1 trop anciennes.')
            a,b=snapshot(m5),snapshot(h1)
            return symbol,{'m5':a,'h1':b,'quote':asdict(self.quotes[symbol])},candidates(a,b)
        try:
            results=await asyncio.gather(*[market_context(s) for s in cfg.symbols])
            rules=await self.market.rules(cfg.symbols); self.rules.update(rules)
        except Exception as exc:
            self.health.update(market_ok=False,status='DONNÉES INDISPONIBLES',message=f'Analyse bloquée : {str(exc)[:400]}')
            if self.store.claim_signal(key):
                self.store.set('last_cycle',key)
                self.store.decision(wait(self.health['message']),self.health['message'],{'closed_candle_ms':expected})
                self.health['last_analysis_at']=self.store.get('last_analysis_at')
                self.store.event(self.health['message'],'error')
            return
        async with self.lock:
            if not self.store.claim_signal(key):
                self.store.set('last_cycle',key); return
            self.store.set('last_cycle',key)
        stats,p=statistics(self.store,self.quotes); day=self.day_state(stats['equity'])
        context={'at_utc':utcnow(),'closed_candle_ms':expected,'symbols':cfg.symbols,
            'markets':{s:data for s,data,_ in results},'candidates':{s:setups for s,_,setups in results},
            'positions':[p] if p else [],'cash':stats['cash'],'equity':stats['equity'],
            'daily_pnl':stats['equity']-day['start_equity'],'rules':cfg.model_dump(),
            'entries_paused':self.store.get('mode')!='running' or day['blocked']}
        # Strip older nested AI histories so tokens stay bounded over many trades.
        if p: context['positions']=[{k:v for k,v in p.items() if k not in {'detail'}}]
        reserve=sum(len(v.encode('utf-8')) for v in (dumps(context),SYSTEM_PROMPT,dumps(Decision.model_json_schema())))+MAX_OUTPUT_TOKENS+1000 # Conservative byte upper bound and protocol allowance.
        if not self.env.api_key and not getattr(self.ai,'test_enabled',False):
            result=await self.ai.decide(context)
        elif not self.store.reserve_usage(day['day'],cfg.max_daily_calls,cfg.max_daily_tokens,reserve):
            decision=Decision(symbol='ALL',action='WAIT',reason='Plafond API quotidien atteint.',entry=None,stop_loss=None,take_profit=None,invalidation=None,confirmation='Aucun appel API autorisé.',risks=['Budget API'],setup='none',confirmation_met=False)
            self.store.decision(decision,'Budget API épuisé',{'context':context})
            self.health.update(ai_ok=False,ai_error='Plafond API quotidien atteint : WAIT.'); return
        else:
            self.ai.model=cfg.model
            result=await self.ai.decide(context)
            self.store.settle_usage(day['day'],reserve,result.input_tokens+result.output_tokens)
        rejection=result.error; decision=result.decision
        detail={'context':context,'proposal':decision.model_dump(),'api_error':result.error,'model':cfg.model,
                'usage':{'input_tokens':result.input_tokens,'output_tokens':result.output_tokens}}
        self.health.update(ai_ok=not bool(result.error),ai_error=result.error)
        if result.error: self.store.event(result.error,'error')
        async with self.lock:
            try:
                if result.error: raise RiskRejected(result.error)
                if revision!=self._settings_revision: raise RiskRejected('Paramètres modifiés pendant l’analyse ; décision annulée.')
                if self.store.get('mode')=='stopped': raise RiskRejected('Analyses arrêtées pendant l’appel IA.')
                if not self.health['market_ok'] or not self.reconciled: raise RiskRejected('Données indisponibles ou réconciliation incomplète.')
                # Fresh quote AFTER the potentially slow AI call. Do not execute on its input price.
                if decision.action in {'BUY','SELL'}:
                    q=await self.market.quote(decision.symbol); self.quotes[decision.symbol]=q
                    if q.time_ms-expected>65000: raise RiskRejected('Décision IA trop tardive après clôture M5 (>65 s).')
                    stats,p=statistics(self.store,self.quotes); day=self.day_state(stats['equity'])
                    if decision.action=='BUY':
                        self.rules.update(await self.market.rules([decision.symbol]))
                        remaining=max(0,day['start_equity']*cfg.daily_loss_pct/100+(stats['equity']-day['start_equity']))
                        sized_cfg=cfg.model_copy(update={'risk_pct':min(cfg.risk_pct,remaining/max(stats['equity'],1e-12)*100)})
                        plan=RiskEngine.plan(decision,q,self.rules[decision.symbol],sized_cfg,stats['cash'],stats['equity'],p,
                            paused=self.store.get('mode')!='running',day_blocked=day['blocked'],atr=context['markets'][decision.symbol]['m5']['atr14'],now_ms=q.time_ms)
                        self.paper.buy(plan,q,key,decision,cfg,{**detail,'plan':asdict(plan)})
                    else:
                        if not p or p['symbol']!=decision.symbol: raise RiskRejected('SELL sans actif détenu interdit.')
                        await self.reconcile(q)
                        if self.store.position(): self.paper.sell(q,'SIGNAL_IA',detail)
                elif decision.action=='HOLD':
                    if not self.store.position(): raise RiskRejected('HOLD sans position : aucun ordre.')
            except (ValueError,MarketError) as exc: rejection=str(exc)
            self.store.decision(decision,rejection,detail)
            self.health['last_analysis_at']=self.store.get('last_analysis_at')
