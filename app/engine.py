from __future__ import annotations
import asyncio, logging, time, json, os
from datetime import datetime, timezone
from dataclasses import asdict
from .config import Settings
from .market import BinanceClient, MarketError
from .multimarket import MultiMarket
from .indicators import snapshot, candidates, short_candidates, long_refusals
from .ai import AIClient, Decision, wait, SYSTEM_PROMPT, MAX_OUTPUT_TOKENS
from .risk import RiskEngine, RiskRejected
from .execution import PaperExecutionEngine
from .metrics import statistics
from .storage import utcnow, dumps

log=logging.getLogger('paperpilot')

class TradingEngine:
    def __init__(self,store,env,market=None,ai=None):
        self.store=store; self.env=env
        self.market=market or MultiMarket(env)
        self.ai=ai or AIClient(env.api_key,model=env.model)
        self.paper=PaperExecutionEngine(store)
        self.quotes={}; self.rules={}
        self.scan = {'at': None, 'rows': [], 'selected': [], 'error': None}
        self._universe = []; self._universe_at = 0.; self._h1 = {}
        self.health=dict(status='DONNÉES INDISPONIBLES',message='Connexion aux données publiques…',market_ok=False,ai_ok=False,last_quote_at=None,last_analysis_at=store.get('last_analysis_at'))
        self.lock=asyncio.Lock(); self.task=None; self.analysis_task=None; self.verify_task=None
        self.started_ms=int(time.time()*1000); self.last_error=None; self.last_snapshot=0.; self.last_analysis_poll=0.; self.closing=False
        self.reconciled=False; self.provider_setup=False; self._settings_revision=0; self.critical_error=None
        # Persist the user's run intent. New entries still require fresh, post-start
        # candles, current data, reconciliation, API/risk checks and daily loss gate.
        mode=self.store.get('mode','stopped')
        if mode not in {'running','paused','stopped'}: mode='paused'
        request=os.getenv('START_PAPER_ONCE','').strip()
        if request and request!=self.store.get('consumed_start_request'):
            with self.store.transaction():
                self.store.set('consumed_start_request',request)
                self.store.set('mode','running')
            mode='running'
        else: self.store.set('mode',mode)
        self.last_diagnostic=0.
        try: self.history_diagnostic()
        except Exception: log.warning('Rapport historique indisponible ; démarrage conservé.')
        self.store.event('Serveur démarré : '+{'running':'reprise de la simulation demandée','paused':'pause conservée','stopped':'arrêt conservé'}[mode]+', protection des positions conservée.')
    def settings(self): return Settings.model_validate(self.store.get('settings'))
    async def start(self):
        self.task=asyncio.create_task(self.run())
        if self.env.api_key: self.verify_task=asyncio.create_task(self.verify_ai())
    async def verify_ai(self):
        """One budgeted authentication/format check. Never a trading decision."""
        cfg=self.settings();day=datetime.now(timezone.utc).strftime('%Y-%m-%d')
        reserve=20000
        try:
            if not self.store.reserve_usage(day,cfg.max_daily_calls,cfg.max_daily_tokens,reserve):
                raise ValueError('Budget API quotidien atteint : vérification IA non effectuée.')
            self.ai.model=cfg.model
            result=await self.ai.decide({'diagnostic_only':True,'symbols':[],'markets':{},'candidates':{},'positions':[],
                'cash':self.store.get('cash'),'rules':cfg.model_dump()})
            self.store.settle_usage(day,reserve,result.input_tokens+result.output_tokens)
            if result.error: raise ValueError(result.error)
            if result.decision.action!='WAIT': raise ValueError('Réponse de diagnostic IA non conforme.')
            self.health.update(ai_ok=True,ai_error=None)
            log.info('PAPER_AI_CHECK connexion et format structurés vérifiés ; aucun ordre ni décision de marché.')
        except asyncio.CancelledError: raise
        except Exception as exc:
            self.health.update(ai_ok=False,ai_error=str(exc)[:300])
            log.warning('PAPER_AI_CHECK %s',str(exc)[:300])
        self.diagnostic()
    async def close(self):
        self.closing=True
        for t in (self.verify_task,self.analysis_task,self.task):
            if t: t.cancel()
        await asyncio.gather(*[t for t in (self.verify_task,self.analysis_task,self.task) if t],return_exceptions=True)
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
            if time.monotonic()-self.last_diagnostic>=60:
                try: self.diagnostic()
                except Exception: log.warning('État opérationnel indisponible ; vérifier la base.')
                self.last_diagnostic=time.monotonic()
            await asyncio.sleep(5)
    def history_diagnostic(self):
        """Private operational report, no credentials, balances or raw AI context."""
        from collections import Counter
        since=datetime.fromtimestamp(time.time()-48*3600,timezone.utc).isoformat(timespec='milliseconds')
        rows=self.store.rows('SELECT at,symbol,action,reason,rejection,detail FROM decisions WHERE at>=? ORDER BY id DESC',(since,))
        actions=Counter(row['action'] for row in rows)
        rejections=Counter(row['rejection'] for row in rows if row['rejection'])
        selected=[]; orders=[]
        for row in rows:
            try: detail=json.loads(row['detail']) if isinstance(row['detail'],str) else row['detail']
            except Exception: detail={}
            if row['action'] in {'BUY','SHORT','SELL'} and len(orders)<40:
                proposal=detail.get('proposal') if isinstance(detail,dict) else None
                orders.append({'at':row['at'],'symbol':row['symbol'],'action':row['action'],
                    'reason':row['reason'],'rejection':row['rejection'],
                    'proposal':{k:proposal[k] for k in ('entry','stop_loss','take_profit','invalidation','setup','confirmation_met') if k in proposal} if isinstance(proposal,dict) else None})
            context=detail.get('context',{}) if isinstance(detail,dict) else {}
            proposals=context.get('candidates',{}) if isinstance(context,dict) else {}
            if not isinstance(proposals,dict): continue
            admitted={symbol:setups for symbol,setups in proposals.items() if setups}
            if admitted and len(selected)<8:
                selected.append({'at':row['at'],'symbol':row['symbol'],'action':row['action'],
                    'reason':row['reason'],'rejection':row['rejection'],'admitted':admitted,
                    'proposal':detail.get('proposal'),'usage':detail.get('usage')})
        report={'since_utc':since,'decisions':len(rows),'actions':dict(actions),
            'rejections':dict(rejections.most_common(8)),'candidate_decisions':selected,
            'order_decisions':orders,
            'trade_count':self.store.rows('SELECT COUNT(*) AS n FROM trades WHERE opened_at>=?',(since,))[0]['n'],
            'open_trade_count':self.store.rows('SELECT COUNT(*) AS n FROM trades WHERE closed_at IS NULL')[0]['n']}
        log.info('PAPER_HISTORY_REPORT %s',json.dumps(report,ensure_ascii=False))
        return report
    def diagnostic(self):
        cfg=self.settings()
        provider=getattr(self.market,'capital',None)
        state={'mode':self.store.get('mode'),'market_ok':self.health['market_ok'],
            'ai_configured':bool(self.env.api_key),'ai_ok':self.health['ai_ok'],
            'ai_error':self.health.get('ai_error'),'status':self.health['status'],
            'message':self.health.get('message'),'last_analysis_at':self.health['last_analysis_at'],
            'scan_count':len(self.scan['rows']),'candidate_count':len(self.scan['selected']),
            'cfd_configured':bool(provider and provider.configured),'cfd_enabled':cfg.cfd_enabled,
            'position_open':bool(self.store.position())}
        log.info('PAPER_STATE %s',json.dumps(state,ensure_ascii=False))
        return state
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
            self.paper.protect(quote,allow_take_profit=self.reconciled)
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
                    if p and symbol==p['symbol']:
                        confirmation=await self.market.quote(symbol)
                        if abs(confirmation.bid/q.bid-1)>.01:
                            errors.append(f'{symbol}: cotations de gap contradictoires.');continue
                        q=confirmation
                        self.store.event('Gap supérieur à 20 % confirmé sur deux cotations ; protection au cours observé, exécution incertaine.','warning')
                    else:
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
                            if held and held['symbol'].startswith('CAP:'):
                                self.paper.protect(observed,allow_take_profit=False)
                            elif held and observed.bid<=held['stop_loss']:
                                self.paper.sell(observed,'STOP_REPRISE_DONNEES_INCOMPLETES',{'uncertain':True,'method':'Stop dépassé au bid frais observé, historique indisponible ; aucune exécution normale supposée.'})
                            raise
                        self.paper.protect(self.quotes[p['symbol']],allow_take_profit=self.reconciled)
                else: self.reconciled=False
            else: self.reconciled=True
            cfd_available=cfg.cfd_enabled and getattr(getattr(self.market,'capital',None),'configured',False)
            if errors and (not cfd_available or (p and any(e.startswith(p['symbol']+':') for e in errors))):
                raise MarketError('; '.join(errors))
            now=await self.market.server_time()
            required=[p['symbol']] if p and cfd_available else ([] if cfd_available else symbols)
            if not all(s in self.quotes and 0<=now-self.quotes[s].time_ms<=15000 for s in required): raise MarketError('Cotations périmées.')
            self.health.update(market_ok=True,last_quote_at=utcnow())
            if not self.store.get('btc_baseline') and 'BTCUSDT' in self.quotes:
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
                self.health.update(status='ACTIVE',message='Scanner actif ; IA non encore vérifiée. Aucun achat avant une réponse valide.')
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
        symbols = cfg.symbols
        expanded = cfg.auto_scanner or cfg.cfd_enabled
        slots_by_provider = {'crypto': asyncio.Semaphore(2), 'cfd': asyncio.Semaphore(2)}
        async def market_context(symbol):
            async with slots_by_provider['cfd' if symbol.startswith('CAP:') else 'crypto']:
                if expanded:
                    quote = await self.market.quote(symbol)
                    previous = self.quotes.get(symbol)
                    if previous and abs(quote.bid / previous.bid - 1) > .20:
                        raise MarketError('Variation incohérente supérieure à 20 %.')
                    self.quotes[symbol] = quote
                    m5 = await self.market.candles(symbol, '5m', 150)
                    expected_h1 = (now//3600000)*3600000-1
                    cached = self._h1.get(symbol)
                    h1 = cached if cached and cached[-1].close_time == expected_h1 else await self.market.candles(symbol, '1h', 150)
                    self._h1[symbol] = h1
                else:
                    m5,h1=await asyncio.gather(self.market.candles(symbol,'5m',150),self.market.candles(symbol,'1h',150))
                if not m5 or m5[-1].close_time!=expected or (await self.market.server_time())-m5[-1].close_time>45000:
                    raise MarketError('Bougie M5 absente ou trop ancienne pour une entrée.')
                expected_h1=(now//3600000)*3600000-1
                if not h1 or h1[-1].close_time!=expected_h1: raise MarketError('Données H1 trop anciennes.')
                a,b=snapshot(m5),snapshot(h1)
                q = self.quotes[symbol]
                setups = candidates(a,b,require_volume=False) if symbol.startswith('CAP:') else candidates(a,b)
                shorts = short_candidates(a,b,require_volume=False) if symbol.startswith('CAP:') and cfg.allow_short else []
                spread = (q.ask-q.bid)/q.bid*10000
                if spread > cfg.max_spread_bps or a['atr_pct'] > cfg.max_atr_pct:
                    setups = []; shorts = []
                directions = (['BUY'] if setups else []) + (['SHORT'] if shorts else [])
                return symbol,{'m5':a,'h1':b,'quote':asdict(q),'directions':directions,
                    'direction_candidates':{'BUY':setups,'SHORT':shorts},
                    'filter_reason':('Spread trop élevé' if spread>cfg.max_spread_bps else 'Volatilité supérieure au plafond configuré' if a['atr_pct']>cfg.max_atr_pct else '; '.join(long_refusals(a,b)) if not symbol.startswith('CAP:') and not setups else None),
                    'instrument':self.market.describe(symbol) if hasattr(self.market,'describe') else {'provider':'Binance Spot','asset_class':'crypto'}},list(dict.fromkeys(setups+shorts))
        try:
            if expanded:
                if cfg.auto_scanner and (not self._universe or time.monotonic()-self._universe_at>900 or len(self._universe)!=cfg.scanner_limit):
                    try:
                        self._universe = await self.market.liquid_symbols(cfg.scanner_limit)
                        self._universe_at = time.monotonic()
                    except MarketError:
                        if not cfg.cfd_enabled: raise
                        self._universe=[]
                held = self.store.position()
                crypto_symbols = self._universe if cfg.auto_scanner else cfg.symbols
                cfd_symbols = await self.market.cfd_symbols(cfg.cfd_scan_limit) if cfg.cfd_enabled and hasattr(self.market,'cfd_symbols') else []
                symbols = list(dict.fromkeys(crypto_symbols + cfd_symbols + ([held['symbol']] if held else [])))
                if held: symbols=[held['symbol']]+[s for s in symbols if s!=held['symbol']]

                # Slow or unavailable symbols must not consume the entire fresh
                # entry window and prevent ready markets from being evaluated.
                tasks={symbol:asyncio.create_task(market_context(symbol)) for symbol in symbols}
                try:
                    remaining=max(0.,min(30.,(expected+43000-await self.market.server_time())/1000))
                    done,pending=await asyncio.wait(list(tasks.values()),timeout=remaining)
                    for task in pending: task.cancel()
                    await asyncio.gather(*pending,return_exceptions=True)
                    raw=[]
                    for symbol in symbols:
                        task=tasks[symbol]
                        if task in pending: raw.append(MarketError('Délai de scan dépassé ; marché ignoré pour ce cycle.'))
                        else:
                            try: raw.append(task.result())
                            except Exception as exc: raw.append(exc)
                finally:
                    outstanding=[t for t in tasks.values() if not t.done()]
                    for task in outstanding: task.cancel()
                    await asyncio.gather(*outstanding,return_exceptions=True)
                rows=[]; valid=[]
                for symbol, item in zip(symbols, raw):
                    if isinstance(item, BaseException):
                        rows.append({'symbol':symbol,'status':'INDISPONIBLE','reason':str(item)[:180],**(self.market.describe(symbol) if hasattr(self.market,'describe') else {})})
                        if held and held['symbol']==symbol: raise MarketError('Analyse de la position ouverte indisponible.')
                    else:
                        valid.append(item)
                        rows.append({'symbol':symbol,**item[1]['instrument'],'status':'CANDIDAT' if item[2] else 'ATTENTE',
                            'reason':', '.join(item[2]) or item[1].get('filter_reason') or 'Critères techniques, spread ou volatilité non réunis.'})
                if not valid: raise MarketError('Aucun marché analysable sur ce cycle.')
                ranked=sorted((r for r in valid if r[2]), key=lambda r:(-r[1]['m5']['volume_ratio'],r[0]))
                results=ranked[:cfg.ai_shortlist]
                if cfg.cfd_enabled:
                    # Volume is not comparable between OTC CFDs and exchange
                    # crypto. Give each ready family a place, rotating ties.
                    families={}
                    for item in ranked:
                        family=item[1]['instrument'].get('asset_class','unknown')
                        families.setdefault(family,[]).append(item)
                    order=list(families)
                    if order:
                        offset=(expected//300000)%len(order)
                        order=order[offset:]+order[:offset]
                        results=[]
                        while order and len(results)<cfg.ai_shortlist:
                            next_order=[]
                            for family in order:
                                if families[family] and len(results)<cfg.ai_shortlist:
                                    results.append(families[family].pop(0))
                                if families[family]: next_order.append(family)
                            order=next_order
                if held:
                    results=[r for r in valid if r[0]==held['symbol']]+[r for r in results if r[0]!=held['symbol']]
                self.scan={'at':utcnow(),'rows':rows,'selected':[r[0] for r in results],'error':None}
                if not results:
                    if self.store.claim_signal(key):
                        self.store.set('last_cycle',key)
                        self.store.decision(wait('Aucune configuration admissible après scan ; aucun appel IA facturé.'),None,{'scanner':self.scan,'closed_candle_ms':expected})
                        self.health['last_analysis_at']=self.store.get('last_analysis_at')
                    return
                cfg=cfg.model_copy(update={'symbols':[r[0] for r in results]})
                # Only the shortlist needs detailed exchange filters and AI payloads.
                for _, data, _ in results:
                    data['m5']['candles']=data['m5']['candles'][-22:]
                    data['h1']['candles']=data['h1']['candles'][-12:]
            else:
                results=await asyncio.gather(*[market_context(s) for s in cfg.symbols])
            rules=await self.market.rules([r[0] for r in results]); self.rules.update(rules)
        except Exception as exc:
            self.scan['error']=str(exc)[:400]
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
        context={'directions':{s:data.get('directions',[]) for s,data,_ in results},
            'direction_candidates':{s:data.get('direction_candidates',{}) for s,data,_ in results},
            'scanner':self.scan if expanded else None,'at_utc':utcnow(),'closed_candle_ms':expected,'symbols':cfg.symbols,
            'markets':{s:data for s,data,_ in results},'candidates':{s:setups for s,_,setups in results},
            'positions':[p] if p else [],'cash':stats['cash'],'equity':stats['equity'],
            'daily_pnl':stats['equity']-day['start_equity'],'rules':cfg.model_dump(),
            'entries_paused':self.store.get('mode')!='running' or day['blocked']}
        # Give the model explicit cost geometry. These are cost illustrations,
        # never a new signal or permission to bypass a structural stop.
        for symbol,data,_ in results:
            if symbol.startswith('CAP:'): continue
            from math import ceil
            q=self.quotes[symbol]; rule=self.rules[symbol]
            slip=cfg.slippage_bps/10000; factor=(1-slip)*(1-cfg.fee_rate)
            fill=q.ask*(1+slip); examples=[]
            for source,raw_stop in [('recent_low',data['m5']['recent_low']-.1*data['m5']['atr14']),('support',data['m5']['support']-.1*data['m5']['atr14'])]:
                from .risk import floor_step
                stop=floor_step(raw_stop,rule.tick_size)
                distance=fill-stop;atr=data['m5']['atr14']
                if not .35*atr<=distance<=4*atr: continue
                loss=fill*(1+cfg.fee_rate)-stop*factor
                required=(fill*(1+cfg.fee_rate)+cfg.min_rr*loss)/factor
                target=ceil(required/rule.tick_size)*rule.tick_size
                examples.append({'structural_reference':source,'stop':stop,'minimum_take_profit_for_net_rr':target,'net_rr_required':cfg.min_rr})
            data['cost_geometry']={'currency':'USDT','estimated_entry_fill':fill,
                'break_even_exit_before_exit_slippage':fill*(1+cfg.fee_rate)/factor,
                'invalidation_constraint':'stop_loss <= invalidation < entry',
                'examples_not_signals':examples,'note':'Un objectif inférieur au seuil net ne respecte pas le risque ; aucun exemple ne garantit un niveau structurel ou un gain.'}
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
                if expected<self.started_ms: raise RiskRejected('Cycle antérieur au dernier démarrage : décision annulée.')
                if revision!=self._settings_revision: raise RiskRejected('Paramètres modifiés pendant l’analyse ; décision annulée.')
                if self.store.get('mode')=='stopped': raise RiskRejected('Analyses arrêtées pendant l’appel IA.')
                if not self.health['market_ok'] or not self.reconciled: raise RiskRejected('Données indisponibles ou réconciliation incomplète.')
                # Fresh quote AFTER the potentially slow AI call. Do not execute on its input price.
                if decision.action in {'BUY','SHORT','SELL'}:
                    if decision.symbol not in context['markets']: raise RiskRejected('Marché absent de la sélection analysée.')
                    if decision.action in {'BUY','SHORT'}:
                        if decision.symbol.startswith('CAP:') and self.provider_setup: raise RiskRejected('Connexion de données en cours de modification.')
                        if decision.action not in context['directions'].get(decision.symbol,[]): raise RiskRejected('Direction non admissible selon le scanner.')
                        if decision.setup not in context['direction_candidates'].get(decision.symbol,{}).get(decision.action,[]): raise RiskRejected('Configuration non admissible pour cette direction.')
                        # Finish metadata I/O before the final executable quote.
                        self.rules.update(await self.market.rules([decision.symbol]))
                    q=await self.market.quote(decision.symbol); self.quotes[decision.symbol]=q
                    if q.time_ms-expected>65000: raise RiskRejected('Décision IA trop tardive après clôture M5 (>65 s).')
                    stats,p=statistics(self.store,self.quotes); day=self.day_state(stats['equity'])
                    if decision.action in {'BUY','SHORT'}:
                        remaining=max(0,day['start_equity']*cfg.daily_loss_pct/100+(stats['equity']-day['start_equity']))
                        sized_cfg=cfg.model_copy(update={'risk_pct':min(cfg.risk_pct,remaining/max(stats['equity'],1e-12)*100)})
                        plan=RiskEngine.plan(decision,q,self.rules[decision.symbol],sized_cfg,stats['cash'],stats['equity'],p,
                            paused=self.store.get('mode')!='running',day_blocked=day['blocked'],atr=context['markets'][decision.symbol]['m5']['atr14'],now_ms=int(time.time()*1000))
                        self.paper.buy(plan,q,key,decision,cfg,{**detail,'plan':asdict(plan)})
                    else:
                        if not p or p['symbol']!=decision.symbol: raise RiskRejected('SELL sans actif détenu interdit.')
                        await self.reconcile(q)
                        if self.store.position(): self.paper.sell(q,'SIGNAL_IA',detail)
                elif decision.action=='HOLD':
                    if not self.store.position(): raise RiskRejected('HOLD sans position : aucun ordre.')
            except (ValueError,MarketError) as exc: rejection=str(exc)
            self.store.decision(decision,rejection,detail)
            if decision.action in {'BUY','SHORT','SELL'}:
                reviewed_quote=self.quotes.get(decision.symbol)
                log.info('PAPER_ORDER_REVIEW %s',json.dumps({'at':self.store.get('last_analysis_at'),
                    'symbol':decision.symbol,'action':decision.action,'rejection':rejection,'signal_id':key,
                    'quote_age_ms':int(time.time()*1000)-reviewed_quote.time_ms if reviewed_quote else None},ensure_ascii=False))
            self.health['last_analysis_at']=self.store.get('last_analysis_at')
