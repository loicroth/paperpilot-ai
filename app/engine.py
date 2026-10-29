from __future__ import annotations
import asyncio, logging, time, json, os
from datetime import datetime, timezone
from dataclasses import asdict
from .config import Settings
from .market import BinanceClient, MarketError
from .multimarket import MultiMarket
from .indicators import snapshot, candidates, short_candidates, long_refusals, short_refusals
from .ai import AIClient, Decision, wait, SYSTEM_PROMPT, MAX_OUTPUT_TOKENS, validate_context
from .risk import RiskEngine, RiskRejected, portfolio_remaining_risk
from .structure import structural_levels
from .entry_plans import build_entry_plans
from .decision_audit import cfd_decision_record
from .execution import PaperExecutionEngine
from .metrics import statistics
from .storage import utcnow, dumps
from .scan_audit import scan_summary
from .opportunity import market_feasibility

log=logging.getLogger('paperpilot')
ENTRY_EVALUATION_SECONDS=10.

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
        self.lock=asyncio.Lock(); self.task=None; self.analysis_task=None; self.verify_task=None; self.catalog_task=None
        self.started_ms=int(time.time()*1000); self.last_error=None; self.last_snapshot=0.; self.last_analysis_poll=0.; self.closing=False
        self.reconciled=False; self.provider_setup=False; self._settings_revision=0; self.critical_error=None
        self._position_reconciled={}
        self._reconciliation_tasks={}; self._reconciliation_locks={}
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
        if self.settings().cfd_enabled and hasattr(self.market,'cfd_symbols'):
            self.catalog_task=asyncio.create_task(self._warm_cfd_catalog())
    async def _warm_cfd_catalog(self):
        try:
            symbols=await self.market.cfd_symbols(self.settings().cfd_scan_limit)
            log.info('PAPER_CFD_CATALOG %s',json.dumps({'ready':len(symbols)},ensure_ascii=False))
        except asyncio.CancelledError:raise
        except (MarketError,ValueError):log.warning('PAPER_CFD_CATALOG catalogue indisponible ; vérifier les motifs du scanner.')
    async def verify_ai(self):
        """One budgeted authentication/format check. Never a trading decision."""
        cfg=self.settings();day=datetime.now(timezone.utc).strftime('%Y-%m-%d')
        reserve=20000
        error_kind=None
        try:
            if not self.store.reserve_usage(day,cfg.max_daily_calls,cfg.max_daily_tokens,reserve):
                raise ValueError('Budget API quotidien atteint : vérification IA non effectuée.')
            self.ai.model=cfg.model
            result=await self.ai.decide({'diagnostic_only':True,'mode':'entry','entry_plans':[],
                'symbols':[],'markets':{},'candidates':{},'positions':[],
                'cash':self.store.get('cash'),'rules':cfg.model_dump()})
            self.store.settle_usage(day,reserve,result.input_tokens+result.output_tokens)
            if result.error:
                error_kind=getattr(result,'error_kind',None)
                raise ValueError(result.error)
            if result.decision.action!='WAIT': raise ValueError('Réponse de diagnostic IA non conforme.')
            self.health.update(ai_ok=True,ai_error=None,ai_error_kind=None)
            log.info('PAPER_AI_CHECK connexion et format de sélection de plan vérifiés ; aucun ordre ni décision de marché.')
        except asyncio.CancelledError: raise
        except Exception as exc:
            self.health.update(ai_ok=False,ai_error=str(exc)[:300],ai_error_kind=error_kind or 'operational')
            log.warning('PAPER_AI_CHECK %s',str(exc)[:300])
        self.diagnostic()
    async def close(self):
        self.closing=True
        tasks=[t for t in (self.verify_task,self.analysis_task,self.task,self.catalog_task) if t]+list(self._reconciliation_tasks.values())
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        self._reconciliation_tasks.clear();self._reconciliation_locks.clear()
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
        selected=[]; orders=[]; last_scan=None; cfd_decisions=[]
        for row in rows:
            try: detail=json.loads(row['detail']) if isinstance(row['detail'],str) else row['detail']
            except Exception: detail={}
            if len(cfd_decisions)<24:
                audit=cfd_decision_record(row,detail)
                if audit:cfd_decisions.append(audit)
            saved_scan=detail.get('scanner') if isinstance(detail,dict) else None
            if last_scan is None and isinstance(saved_scan,dict) and isinstance(saved_scan.get('rows'),list):
                last_scan=scan_summary(saved_scan)
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
                    'proposal':detail.get('proposal'),'usage':detail.get('usage'),
                    'ai_diagnostic':detail.get('ai_diagnostic')})
        report={'since_utc':since,'decisions':len(rows),'actions':dict(actions),
            'rejections':dict(rejections.most_common(8)),'candidate_decisions':selected,
            'order_decisions':orders,
            'trade_count':self.store.rows('SELECT COUNT(*) AS n FROM trades WHERE opened_at>=?',(since,))[0]['n'],
            'open_trade_count':len(self.store.positions()),'last_scan':last_scan,
            'trades':self.store.rows('SELECT id,symbol,kind,direction,opened_at,closed_at,entry,exit,stop_loss,take_profit,quantity,risk_usdt,fees,financing_accrued,net_pnl,exit_reason FROM trades WHERE opened_at>=? ORDER BY id DESC LIMIT 20',(since,))}
        log.info('PAPER_HISTORY_REPORT %s',json.dumps(report,ensure_ascii=False))
        for audit in cfd_decisions:
            log.info('PAPER_CFD_DECISION_HISTORY %s',json.dumps(audit,ensure_ascii=False))
        if last_scan:log.info('PAPER_SCAN_AUDIT %s',json.dumps({'source':'persisted',**last_scan},ensure_ascii=False))
        return report
    def scan_diagnostic(self):
        summary=scan_summary(self.scan);self.scan['summary']=summary
        log.info('PAPER_SCAN_AUDIT %s',json.dumps({'source':'current',**summary},ensure_ascii=False))
        return summary
    def diagnostic(self):
        cfg=self.settings()
        provider=getattr(self.market,'capital',None)
        state={'mode':self.store.get('mode'),'market_ok':self.health['market_ok'],
            'ai_configured':bool(self.env.api_key),'ai_ok':self.health['ai_ok'],
            'ai_error':self.health.get('ai_error'),'ai_error_kind':self.health.get('ai_error_kind'),'status':self.health['status'],
            'message':self.health.get('message'),'last_analysis_at':self.health['last_analysis_at'],
            'scan_count':len(self.scan['rows']),'candidate_count':len(self.scan['selected']),
            'cfd_configured':bool(provider and provider.configured),'cfd_enabled':cfg.cfd_enabled,
            'position_open':bool(self.store.positions()),'open_positions_count':len(self.store.positions()),
            'max_positions':cfg.max_positions,'max_trade_risk_pct':cfg.max_trade_risk_pct,
            'max_portfolio_risk_pct':cfg.max_portfolio_risk_pct}
        state['scanner_summary']=scan_summary(self.scan)
        if hasattr(self.market,'status'):
            source=self.market.status().get('capital',{})
            state['cfd_source']={'connected':bool(source.get('connected')),'error':source.get('error'),
                'supported':len(source.get('supported',[])),'blocked':len(source.get('blocked',[]))}
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
            state={'day':day,'start_equity':baseline,'blocked':False,'baseline_approximation': bool(self.store.positions())}
        if equity<=state['start_equity']*(1-self.settings().daily_loss_pct/100): state['blocked']=True
        self.store.set('day_state',state)
        return state
    def _pending_reconciliation(self):
        pending=dict(self.store.get('reconcile_pending_by_id',{}))
        legacy=self.store.get('reconcile_pending')
        if legacy and str(legacy['id']) not in pending:
            pending[str(legacy['id'])]=legacy
        return pending
    def _set_reconciliation(self,p,ready,pending=None):
        records=self._pending_reconciliation()
        live={row['id'] for row in self.store.positions()}
        if pending is None: records.pop(str(p['id']),None)
        else: records[str(p['id'])]=pending
        records={key:value for key,value in records.items() if value['id'] in live}
        self.store.set('reconcile_pending_by_id',records)
        # Preserve the old single-position recovery marker during the migration.
        self.store.set('reconcile_pending',next(iter(records.values()),None))
        self._position_reconciled[p['id']]=ready
        self._position_reconciled={key:value for key,value in self._position_reconciled.items() if key in live}
        self.reconciled=all(self._position_reconciled.get(key,False) for key in live)
    def _recovery_generation(self):
        return (self.started_ms,self._settings_revision)
    @staticmethod
    def _position_identity(position):
        return (position['id'],position.get('signal'),position.get('opened_at'))
    def _recovery_current(self,symbol,identity,generation):
        current=self.store.position(symbol)
        return bool(generation==self._recovery_generation() and current and self._position_identity(current)==identity)
    async def reset_reconciliation_tasks(self):
        """Cancel old recovery before resetting a wallet whose row IDs can be reused."""
        tasks=list(self._reconciliation_tasks.values())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        self._reconciliation_tasks.clear();self._reconciliation_locks.clear()
        self._position_reconciled={};self.reconciled=not bool(self.store.positions())
    async def _reconcile_background(self,quote,identity,generation):
        try:
            if self._recovery_current(quote.symbol,identity,generation):
                await self.reconcile(quote,expected_identity=identity,expected_generation=generation)
        except asyncio.CancelledError:raise
        except (MarketError,ValueError) as exc:
            if self._recovery_current(quote.symbol,identity,generation):
                p=self.store.position(quote.symbol)
                self._set_reconciliation(p,False,self._pending_reconciliation().get(str(p['id'])))
                message=f"{quote.symbol}: réconciliation incomplète : {str(exc)[:250]}"
                self.health.update(market_ok=False,status='DONNÉES INDISPONIBLES',message=message)
                if message!=self.last_error:
                    self.store.event(message,'warning');log.warning(message);self.last_error=message
        except Exception as exc:
            if generation==self._recovery_generation():self.error(f'Réconciliation suspendue : {type(exc).__name__}')
        finally:
            if self._reconciliation_tasks.get(identity[0]) is asyncio.current_task():
                self._reconciliation_tasks.pop(identity[0],None)
    async def reconcile(self,quote,expected_identity=None,expected_generation=None):
        """Awaitable recovery for explicit callers; tick schedules it per position."""
        p=self.store.position(quote.symbol)
        if not p:
            self.reconciled=all(self._position_reconciled.get(row['id'],False) for row in self.store.positions())
            return
        identity=expected_identity or self._position_identity(p)
        generation=self._recovery_generation() if expected_generation is None else expected_generation
        lock=self._reconciliation_locks.setdefault(identity[0],asyncio.Lock())
        async with lock:
            if not self._recovery_current(quote.symbol,identity,generation):return
            await self._reconcile_position(quote,identity,generation)
    async def _reconcile_position(self,quote,identity,generation):
        p=self.store.position(quote.symbol)
        if not self._recovery_current(quote.symbol,identity,generation):return
        gap=quote.time_ms-p['last_market_at']
        pending=self._pending_reconciliation().get(str(p['id']))
        if gap<=15000 and not pending:
            self._set_reconciliation(p,True);return
        if not pending:pending={'id':p['id'],'since':p['last_market_at'],'until':quote.time_ms}
        self._set_reconciliation(p,False,pending)
        begin=pending['since']//60000*60000;end=quote.time_ms
        seen=[];cursor=begin
        for _ in range(45):
            bars=await self.market.candles(p['symbol'],'1m',limit=1000,start_time=cursor,end_time=end)
            if not self._recovery_current(quote.symbol,identity,generation):return
            if not bars:break
            if bars[0].open_time!=cursor:raise MarketError('Réconciliation : premières bougies M1 manquantes.')
            seen.extend(bars);cursor=bars[-1].close_time+1
            if cursor+60000>end:break
        if cursor+60000<=end:raise MarketError('Interruption trop longue ou données M1 incomplètes : entrées bloquées.')
        fresh=await self.market.quote(p['symbol'])
        if not self._recovery_current(quote.symbol,identity,generation):return
        async with self.lock:
            if not self._recovery_current(quote.symbol,identity,generation):return
            self.quotes[p['symbol']]=fresh
            closed=self.paper.reconcile(fresh,seen,since_time=pending['since'])
            self.store.event('Reprise après interruption : M1 vérifiées ; toute touche est marquée comme approximation prudente.','warning')
            if closed or cursor>pending['until']:self._set_reconciliation(p,True)
            else:self._set_reconciliation(p,False,pending)
            if not closed:
                self.paper.protect(fresh,allow_take_profit=self._position_reconciled.get(p['id'],False),portfolio_quotes=self.quotes)
                if not self.store.position(p['symbol']):self._set_reconciliation(p,True)
    def _protect_observed_quote(self,quote,captured_position):
        """No I/O or await: protect fresh observed risk before any historic lookup."""
        current=self.store.position(quote.symbol)
        if not current or self._position_identity(current)!=self._position_identity(captured_position):return
        pending=self._pending_reconciliation().get(str(current['id']))
        needs_history=pending is not None or quote.time_ms-current['last_market_at']>15000
        if needs_history:
            if pending is None:pending={'id':current['id'],'since':current['last_market_at'],'until':quote.time_ms}
            self._set_reconciliation(current,False,pending)
        else:self._set_reconciliation(current,True)
        # The pending marker is recorded before protect advances last_market_at.
        short=current.get('kind')=='cfd' and current.get('direction')=='SHORT'
        breached=quote.ask>=current['stop_loss'] if short else quote.bid<=current['stop_loss']
        if needs_history and breached:
            self.paper.sell(quote,'STOP_REPRISE_DONNEES_INCOMPLETES',{
                'uncertain':True,'method':'Stop dépassé sur cotation fraîche à la reprise ; historique non encore réconcilié, aucune exécution passée normale supposée.'})
        else:self.paper.protect(quote,allow_take_profit=not needs_history,portfolio_quotes=self.quotes)
        if not self.store.position(quote.symbol):
            if needs_history:
                rows=self.store.rows('SELECT detail FROM trades WHERE id=?',(current['id'],))
                if rows:
                    detail=json.loads(rows[0]['detail']);detail.setdefault('exit',{}).update(uncertain=True,historical_gap_unresolved=True)
                    self.store.conn.execute('UPDATE trades SET detail=? WHERE id=?',(dumps(detail),current['id']))
            self._set_reconciliation(current,True);return needs_history
        if needs_history:
            task=self._reconciliation_tasks.get(current['id'])
            if task is None or task.done():
                identity=self._position_identity(current);generation=self._recovery_generation()
                self._reconciliation_tasks[current['id']]=asyncio.create_task(self._reconcile_background(quote,identity,generation))
    async def tick(self):
        cfg=self.settings(); positions=self.store.positions()
        held_symbols={p['symbol'] for p in positions}
        symbols=list(dict.fromkeys([p['symbol'] for p in positions]+cfg.symbols+['BTCUSDT']))
        try:
            now=await self.market.server_time()
            if abs(now-int(time.time()*1000))>5000: raise MarketError('Horloge ordinateur décalée de plus de 5 secondes : synchronisez-la.')
            captured={p['symbol']:p for p in positions}
            async def fetch(symbol):
                try:
                    quote=await self.market.quote(symbol);gap=False
                    previous=self.quotes.get(symbol)
                    if previous and abs(quote.bid/previous.bid-1)>.20:
                        if symbol not in held_symbols:raise MarketError('Variation incohérente supérieure à 20 %, cotation rejetée.')
                        confirmation=await self.market.quote(symbol)
                        if abs(confirmation.bid/quote.bid-1)>.01:raise MarketError('Cotations de gap contradictoires.')
                        quote=confirmation;gap=True
                    return symbol,quote,gap,None
                except (MarketError,ValueError) as exc:return symbol,None,False,str(exc)
            quote_tasks=[asyncio.create_task(fetch(symbol)) for symbol in symbols]
            errors=[]
            try:
                for completed in asyncio.as_completed(quote_tasks):
                    symbol,q,gap,error=await completed
                    if error is not None:
                        errors.append(f'{symbol}: {error}')
                        p=self.store.position(symbol)
                        if p and symbol in captured and self._position_identity(p)==self._position_identity(captured[symbol]):
                            self._set_reconciliation(p,False,self._pending_reconciliation().get(str(p['id'])))
                        continue
                    if not 0<=int(time.time()*1000)-q.time_ms<=15000:
                        errors.append(f'{symbol}: Cotation périmée.');continue
                    self.quotes[symbol]=q
                    if gap:self.store.event('Gap supérieur à 20 % confirmé sur deux cotations ; protection au cours observé, exécution incertaine.','warning')
                    if symbol in captured:
                        try:
                            if self._protect_observed_quote(q,captured[symbol]):
                                errors.append(f'{symbol}: sortie à la reprise sur données historiques incomplètes.')
                        except (MarketError,ValueError) as exc:
                            errors.append(f'{symbol}: protection indisponible : {exc}')
                            p=self.store.position(symbol)
                            if p:self._set_reconciliation(p,False,self._pending_reconciliation().get(str(p['id'])))
            finally:
                outstanding=[task for task in quote_tasks if not task.done()]
                for task in outstanding:task.cancel()
                await asyncio.gather(*outstanding,return_exceptions=True)
            # Give completed recovery jobs a chance to publish their readiness;
            # never wait for a slow M1 provider here.
            await asyncio.sleep(0)
            live=self.store.positions()
            self.reconciled=all(self._position_reconciled.get(p['id'],False) for p in live)
            if live and not self.reconciled:raise MarketError('Historique incomplet sur une position : nouvelles entrées suspendues.')
            cfd_available=cfg.cfd_enabled and getattr(getattr(self.market,'capital',None),'configured',False)
            if errors and (not cfd_available or any(any(e.startswith(symbol+':') for symbol in held_symbols) for e in errors)):
                raise MarketError('; '.join(errors))
            now=int(time.time()*1000)
            required=list(held_symbols) if cfd_available else symbols
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
                if self.health.get('ai_error_kind')=='invalid_decision':
                    run_message={'running':'Le scanner continue ; une nouvelle réponse valide est nécessaire pour exécuter un ordre.',
                        'paused':'Entrées en pause ; protection des positions surveillée.',
                        'stopped':'Analyses arrêtées ; protection des positions surveillée.'}[mode]
                    self.health.update(status='ACTIVE · RÉPONSE IA REJETÉE' if mode=='running' else 'PAUSE',
                        message=f"Dernière réponse IA rejetée : {self.health['ai_error']} {run_message}",ai_ok=False)
                else:
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
    async def _prepare_entry_candidates(self,ranked,rows,cfg,expected,expanded):
        """Check every technical candidate before spending the bounded AI slots.

        A high-volume but impossible plan must not hide a lower-ranked valid
        market. These bounds only exclude demonstrably impossible directions;
        unknown bounds remain visible behind verified feasible candidates.
        """
        if not ranked:return []
        by_symbol={row['symbol']:row for row in rows}
        crypto=[item[0] for item in ranked if not item[0].startswith('CAP:')]
        # Binance returns one exchangeInfo batch. CFD metadata is isolated by
        # symbol so one bad contract cannot discard the other valid markets.
        crypto_rules=asyncio.create_task(self.market.rules(crypto)) if crypto else None
        slots={'crypto':asyncio.Semaphore(4),'cfd':asyncio.Semaphore(2)}
        impossible={'missing_stop','missing_target','invalid_levels','insufficient_rr'}
        async def evaluate(item):
            symbol,data,setups=item
            cfd=symbol.startswith('CAP:')
            async with slots['cfd' if cfd else 'crypto']:
                found=(await self.market.rules([symbol])) if cfd else await asyncio.shield(crypto_rules)
                rules=found[symbol]
                q=await self.market.quote(symbol)
                if q.symbol!=symbol:raise MarketError('Cotation de validation sur un autre marché.')
                if not 0<=int(time.time()*1000)-q.time_ms<=15000:
                    raise MarketError('Cotation trop ancienne ou horodatage futur.')
                self.rules[symbol]=rules;self.quotes[symbol]=q;data['quote']=asdict(q)
                data['structure']={side:structural_levels(data,entry=q.ask if side=='BUY' else q.bid,
                    direction='LONG' if side=='BUY' else 'SHORT',now_ms=expected+1) for side in data['directions']}
                bounds={side:market_feasibility(data,q,rules,cfg,short=side=='SHORT') for side in data['directions']}
                data['feasibility']=bounds
                row=by_symbol[symbol];row['feasibility']=bounds
                row['technical_candidate']=True
                row['reason']+=' — '+' | '.join(side+' : '+bound['reason'] for side,bound in bounds.items())
                allowed=[side for side in data['directions'] if bounds[side].get('status') not in impossible]
                if not allowed:
                    row['status']='ATTENTE'
                    return None
                # Keep the full bounds for diagnostics, but do not offer an
                # impossible side to the model when the opposite side is valid.
                data['directions']=allowed
                data['direction_candidates']={side:values if side in allowed else []
                    for side,values in data['direction_candidates'].items()}
                setups=list(dict.fromkeys(setup for side in allowed for setup in data['direction_candidates'][side]))
                priority=0 if any(bounds[side].get('status')=='feasible' for side in allowed) else 1
                return (symbol,data,setups),priority
        tasks={item[0]:asyncio.create_task(evaluate(item)) for item in ranked}
        try:
            remaining=max(0.,min(ENTRY_EVALUATION_SECONDS,(expected+48000-await self.market.server_time())/1000))
            _,pending=await asyncio.wait(list(tasks.values()),timeout=remaining)
            for task in pending:task.cancel()
            await asyncio.gather(*pending,return_exceptions=True)
            prepared=[]
            for item in ranked:
                symbol=item[0];task=tasks[symbol]
                try:
                    if task in pending:raise MarketError('Délai de validation économique dépassé ; marché ignoré pour ce cycle.')
                    result=task.result()
                except (Exception,asyncio.CancelledError) as exc:
                    by_symbol[symbol].update(status='INDISPONIBLE',reason='Validation avant analyse IA indisponible : '+str(exc)[:180])
                else:
                    if result is not None:prepared.append(result)
        finally:
            owned=[*tasks.values()]+([crypto_rules] if crypto_rules is not None else [])
            for task in owned:
                if not task.done():task.cancel()
            await asyncio.gather(*owned,return_exceptions=True)
        def family_order(items):
            if not (expanded and cfg.cfd_enabled):return items
            families={}
            for item in items:families.setdefault(item[1]['instrument'].get('asset_class','unknown'),[]).append(item)
            order=list(families)
            if order:
                offset=(expected//300000)%len(order);order=order[offset:]+order[:offset]
            ordered=[]
            while order:
                next_order=[]
                for family in order:
                    ordered.append(families[family].pop(0))
                    if families[family]:next_order.append(family)
                order=next_order
            return ordered
        ordered=[]
        for priority in (0,1):ordered.extend(family_order([item for item,p in prepared if p==priority]))
        selected=ordered[:cfg.ai_shortlist] if expanded else ordered
        selected_symbols={item[0] for item in selected}
        for item in ordered:
            if item[0] not in selected_symbols:
                by_symbol[item[0]]['selection_reason']='Limite de présélection IA ; les configurations faisables sont prioritaires.'
        return selected
    async def _analyze(self):
        cfg=self.settings(); revision=self._settings_revision
        held=self.store.positions()
        if not self.health['market_ok'] and not held: return
        now=await self.market.server_time()
        expected=(now//300000)*300000-1
        if expected<self.started_ms: return
        key=f'm5:{expected}'
        if self.store.get('last_cycle')==key: return
        expanded=cfg.auto_scanner or cfg.cfd_enabled
        slots={'crypto':asyncio.Semaphore(2),'cfd':asyncio.Semaphore(2)}
        async def market_context(symbol):
            async with slots['cfd' if symbol.startswith('CAP:') else 'crypto']:
                quote=await self.market.quote(symbol)
                previous=self.quotes.get(symbol)
                if previous and abs(quote.bid/previous.bid-1)>.20:
                    raise MarketError('Variation incohérente supérieure à 20 %.')
                self.quotes[symbol]=quote
                if expanded:
                    m5=await self.market.candles(symbol,'5m',150)
                    expected_h1=(now//3600000)*3600000-1
                    cached=self._h1.get(symbol)
                    h1=cached if cached and cached[-1].close_time==expected_h1 else await self.market.candles(symbol,'1h',150)
                    self._h1[symbol]=h1
                else:
                    m5,h1=await asyncio.gather(self.market.candles(symbol,'5m',150),self.market.candles(symbol,'1h',150))
                if not m5 or m5[-1].close_time!=expected or (await self.market.server_time())-m5[-1].close_time>45000:
                    raise MarketError(f"Bougie M5 absente ou trop ancienne pour une entrée (reçu={m5[-1].close_time if m5 else None}, attendu={expected}).")
                if not h1 or h1[-1].close_time!=(now//3600000)*3600000-1:
                    raise MarketError(f"Données H1 trop anciennes (reçu={h1[-1].close_time if h1 else None}, attendu={(now//3600000)*3600000-1}).")
                a,b=snapshot(m5),snapshot(h1)
                setups=candidates(a,b,require_volume=False) if symbol.startswith('CAP:') else candidates(a,b)
                shorts=short_candidates(a,b,require_volume=False) if symbol.startswith('CAP:') and cfg.allow_short else []
                spread=(quote.ask-quote.bid)/quote.bid*10000
                if spread>cfg.max_spread_bps or a['atr_pct']>cfg.max_atr_pct:
                    setups=[];shorts=[]
                cfd_reason=None
                if symbol.startswith('CAP:') and not setups and not shorts:
                    cfd_reason='LONG : '+' / '.join(long_refusals(a,b))
                    cfd_reason+=(' | SHORT : '+' / '.join(short_refusals(a,b))) if cfg.allow_short else ' | SHORT désactivé'
                data={'m5':a,'h1':b,'quote':asdict(quote),
                    'directions':(['BUY'] if setups else [])+(['SHORT'] if shorts else []),
                    'direction_candidates':{'BUY':setups,'SHORT':shorts},
                    'instrument':self.market.describe(symbol) if hasattr(self.market,'describe') else {'provider':'Binance Spot','asset_class':'crypto'},
                    'filter_reason':('Spread trop élevé' if spread>cfg.max_spread_bps else 'Volatilité supérieure au plafond configuré' if a['atr_pct']>cfg.max_atr_pct else cfd_reason if symbol.startswith('CAP:') else '; '.join(long_refusals(a,b)) if not setups else None)}
                # Market references come first. Never derive an objective from a
                # desired multiple and then ask the model to justify that price.
                data['structure']={side:structural_levels(data,entry=quote.ask if side=='BUY' else quote.bid,
                    direction='LONG' if side=='BUY' else 'SHORT',now_ms=expected+1)
                    for side in data['directions']}
                return symbol,data,list(dict.fromkeys(setups+shorts))
        rows=[]
        try:
            if cfg.auto_scanner and (not self._universe or time.monotonic()-self._universe_at>900 or len(self._universe)!=cfg.scanner_limit):
                try:
                    self._universe=await self.market.liquid_symbols(cfg.scanner_limit)
                    self._universe_at=time.monotonic()
                except MarketError:
                    if not cfg.cfd_enabled: raise
                    self._universe=[]
            crypto=self._universe if cfg.auto_scanner else cfg.symbols
            cfd=await self.market.cfd_symbols(cfg.cfd_scan_limit) if cfg.cfd_enabled and hasattr(self.market,'cfd_symbols') else []
            held_symbols={p['symbol'] for p in held}
            symbols=list(dict.fromkeys([p['symbol'] for p in held]+crypto+cfd))
            tasks={symbol:asyncio.create_task(market_context(symbol)) for symbol in symbols}
            try:
                if expanded:
                    remaining=max(0.,min(30.,(expected+43000-await self.market.server_time())/1000))
                    done,pending=await asyncio.wait(list(tasks.values()),timeout=remaining)
                    for task in pending: task.cancel()
                    await asyncio.gather(*pending,return_exceptions=True)
                else:
                    await asyncio.gather(*tasks.values(),return_exceptions=True)
                    pending=set()
                rows=[];valid=[];held_errors=[]
                for symbol,task in tasks.items():
                    try:
                        if task in pending: raise MarketError('Délai de scan dépassé ; marché ignoré pour ce cycle.')
                        item=task.result()
                    except (Exception,asyncio.CancelledError) as exc:
                        rows.append({'symbol':symbol,'status':'INDISPONIBLE','reason':str(exc)[:180],
                            **(self.market.describe(symbol) if hasattr(self.market,'describe') else {})})
                        if symbol in held_symbols: held_errors.append(symbol)
                    else:
                        valid.append(item)
                        rows.append({'symbol':symbol,**item[1]['instrument'],'status':'CANDIDAT' if item[2] and symbol not in held_symbols else 'POSITION OUVERTE' if symbol in held_symbols else 'ATTENTE',
                            'reason':', '.join(item[2]) or item[1].get('filter_reason') or 'Critères techniques, spread ou volatilité non réunis.'})
            finally:
                outstanding=[t for t in tasks.values() if not t.done()]
                for task in outstanding: task.cancel()
                await asyncio.gather(*outstanding,return_exceptions=True)
            self.scan={'at':utcnow(),'rows':rows,'selected':[],'error':None}
            if not valid:
                messages='; '.join(row['reason'] for row in rows[:3])
                raise MarketError('Aucun marché analysable sur ce cycle. '+messages)
            ranked=sorted((r for r in valid if r[2] and r[0] not in held_symbols),key=lambda r:(-r[1]['m5']['volume_ratio'],r[0]))
            ready_held=[p for p in held if any(r[0]==p['symbol'] for r in valid)]
            cursor=self.store.get('last_managed_position_id',0)
            managed=next((p for p in ready_held if p['id']>cursor),ready_held[0] if ready_held else None)
            entry_results=await self._prepare_entry_candidates(ranked,rows,cfg,expected,expanded)
            selected=[r[0] for r in entry_results]+([managed['symbol']] if managed else [])
            self.scan={'at':utcnow(),'rows':rows,'selected':list(dict.fromkeys(selected)),
                'error':'Analyse indisponible pour '+', '.join(held_errors) if held_errors else None}
        except Exception as exc:
            self.scan['error']=str(exc)[:400]
            self.health.update(market_ok=False,status='DONNÉES INDISPONIBLES',message=f'Analyse bloquée : {str(exc)[:400]}')
            if self.store.claim_signal(key):
                self.store.set('last_cycle',key)
                self.scan_diagnostic()
                self.store.decision(wait(self.health['message']),self.health['message'],{'closed_candle_ms':expected,'scanner':self.scan})
                self.health['last_analysis_at']=self.store.get('last_analysis_at')
                self.store.event(self.health['message'],'error')
            return
        async with self.lock:
            if not self.store.claim_signal(key):
                self.store.set('last_cycle',key);return
            self.store.set('last_cycle',key)
            if managed: self.store.set('last_managed_position_id',managed['id'])
        self.scan_diagnostic()
        bounds={symbol:data.get('feasibility',{}) for symbol,data,_ in entry_results if data.get('feasibility')}
        if bounds:log.info('PAPER_FEASIBILITY_AUDIT %s',json.dumps({'at':utcnow(),'markets':bounds},ensure_ascii=False,allow_nan=False))
        stats,_=statistics(self.store,self.quotes);day=self.day_state(stats['equity'])
        positions=[{k:v for k,v in p.items() if k!='detail'} for p in stats['positions']]
        used=portfolio_remaining_risk(self.store.positions(),self.quotes,now_ms=int(time.time()*1000))
        import math
        risk_known=math.isfinite(used)
        remaining_day=max(0,day['start_equity']*cfg.daily_loss_pct/100+(stats['equity']-day['start_equity']))
        available=max(0,min(stats['equity']*cfg.max_portfolio_risk_pct/100,remaining_day)-used) if risk_known else 0.
        portfolio={'open_count':len(positions),'max_positions':cfg.max_positions,'risk_known':risk_known,
            'risk_used_usdt':used if risk_known else None,'risk_available_usdt':available,
            'max_trade_risk_pct':cfg.max_trade_risk_pct,'max_portfolio_risk_pct':cfg.max_portfolio_risk_pct}
        base={'at_utc':utcnow(),'closed_candle_ms':expected,'cash':stats['cash'],'equity':stats['equity'],
            'daily_pnl':stats['equity']-day['start_equity'],'portfolio':portfolio,
            'entries_paused':self.store.get('mode')!='running' or day['blocked'],
            'scanner':{'at':self.scan['at'],'selected':self.scan['selected']} if expanded else None}
        def context_for(results,mode,target=None):
            import copy
            markets={s:copy.deepcopy(data) for s,data,_ in results}
            for data in markets.values():
                data['m5']['candles']=data['m5']['candles'][-22:]
                data['h1']['candles']=data['h1']['candles'][-12:]
            symbols=[r[0] for r in results]
            context={**base,'mode':mode,'symbols':symbols,'rules':cfg.model_copy(update={'symbols':symbols}).model_dump(),
                'markets':markets,'candidates':{s:setups for s,_,setups in results},
                'directions':{s:data['directions'] for s,data,_ in results},
                'direction_candidates':{s:data['direction_candidates'] for s,data,_ in results},
                'positions':[p for p in positions if not target or p['symbol']==target]}
            if target: context['managed_symbol']=target
            if mode=='entry':context['entry_plans']=build_entry_plans(results,cfg)
            return context
        calls=[]
        if managed:
            results=[r for r in valid if r[0]==managed['symbol']]
            calls.append((context_for(results,'management',managed['symbol']),{r[0]:r[1] for r in results}))
        entry_context=context_for(entry_results,'entry')
        entry_ready=(entry_context['entry_plans'] and len(positions)<cfg.max_positions
            and available>0 and not base['entries_paused'] and self.health['market_ok'] and self.reconciled and not held_errors)
        if entry_ready:
            calls.append((entry_context,{r[0]:r[1] for r in entry_results}))
        if not calls:
            reason=('Aucune configuration admissible après scan ; aucun appel IA facturé.' if not ranked else
                'Aucun plan de marché vérifiable ne respecte le ratio net et les niveaux requis ; aucun appel IA facturé.' if not entry_context['entry_plans'] else
                'Nouvelles entrées suspendues : capacité, risque disponible, pause ou données insuffisantes.')
            self.store.decision(wait(reason),None,{'scanner':self.scan,'closed_candle_ms':expected,'portfolio':portfolio})
            self.health['last_analysis_at']=self.store.get('last_analysis_at')
            return
        # Management reserves its budget first. Parallel calls keep entry quotes
        # inside the same fresh M5 window; all execution is rechecked under lock.
        outcomes=await asyncio.gather(*(self._decide_and_execute(context,raw,cfg,revision,expected,key,day['day']) for context,raw in calls))
        errors=[outcome for outcome in outcomes if outcome['error']]
        if errors:
            # A successful entry response must not hide a rejected management
            # response (or vice versa). Keep operational faults ahead of a
            # malformed decision, and preserve both messages in the diagnostic.
            errors.sort(key=lambda outcome:outcome['error_kind']=='invalid_decision')
            messages=list(dict.fromkeys(outcome['error'] for outcome in errors))
            self.health.update(ai_ok=False,ai_error=' ; '.join(messages),ai_error_kind=errors[0]['error_kind'])
        else:
            self.health.update(ai_ok=True,ai_error=None,ai_error_kind=None)

    async def _decide_and_execute(self,context,raw_markets,cfg,revision,expected,key,day):
        if context.get('mode')=='entry' and int(time.time()*1000)-expected>=65000:
            message='Fenêtre d’entrée expirée avant appel IA après clôture M5 (65 s).'
            self.store.decision(wait(message),message,{'context':context,'closed_candle_ms':expected})
            self.health['last_analysis_at']=self.store.get('last_analysis_at')
            return {'error':None,'error_kind':None}
        reserve=sum(len(v.encode('utf-8')) for v in (dumps(context),SYSTEM_PROMPT,dumps(Decision.model_json_schema())))+MAX_OUTPUT_TOKENS+1000
        if not self.env.api_key and not getattr(self.ai,'test_enabled',False):
            result=await self.ai.decide(context)
        elif not self.store.reserve_usage(day,cfg.max_daily_calls,cfg.max_daily_tokens,reserve):
            self.store.decision(wait('Plafond API quotidien atteint.'),'Budget API épuisé',{'context':context})
            self.health.update(ai_ok=False,ai_error='Plafond API quotidien atteint : WAIT.',ai_error_kind='limits')
            return {'error':'Plafond API quotidien atteint : WAIT.','error_kind':'limits'}
        else:
            self.ai.model=cfg.model
            result=await self.ai.decide(context)
            self.store.settle_usage(day,reserve,result.input_tokens+result.output_tokens)
        decision=result.decision;rejection=result.error
        detail={'context':context,'proposal':decision.model_dump(),'api_error':result.error,'model':cfg.model,
            'usage':{'input_tokens':result.input_tokens,'output_tokens':result.output_tokens}}
        kind=getattr(result,'error_kind',None);diagnostic=getattr(result,'diagnostic',None)
        if diagnostic: detail['ai_diagnostic']=diagnostic
        outcome={'error':result.error,'error_kind':kind or ('operational' if result.error else None)}
        if result.error:
            # Show failures immediately, but clear them only after every call in
            # this cycle has completed and its outcome has been aggregated.
            self.health.update(ai_ok=False,ai_error=result.error,ai_error_kind=outcome['error_kind'])
            self.store.event(result.error,'warning' if kind=='invalid_decision' else 'error')
            log.warning('PAPER_AI_REJECTION %s',json.dumps({'at':utcnow(),'closed_candle_ms':expected,
                'mode':context['mode'],'error_kind':kind,'diagnostic':diagnostic},ensure_ascii=False))
        entry=decision.action in {'BUY','SHORT'}
        expected_target=next((p for p in context.get('positions',[]) if p.get('symbol')==decision.symbol),None)
        target_identity=self._position_identity(expected_target) if expected_target and 'id' in expected_target else None
        fetched_rules={};q=None;io_rejection=None
        try:
            if result.error:raise RiskRejected(result.error)
            validate_context(decision,context)
            if entry and int(time.time()*1000)-expected>65000:
                raise RiskRejected('Décision IA trop tardive après clôture M5 (>65 s).')
            if decision.action in {'BUY','SHORT','SELL'}:
                if decision.symbol not in context['markets']:raise RiskRejected('Marché absent de la sélection analysée.')
                if entry:
                    # Metadata may require network I/O; protection never waits
                    # behind the global accounting lock while it is fetched.
                    fetched_rules=await self.market.rules([decision.symbol])
                q=await self.market.quote(decision.symbol)
                if decision.action=='SELL':
                    live=self.store.position(decision.symbol)
                    if target_identity is None or not live or self._position_identity(live)!=target_identity:
                        raise RiskRejected('Position de gestion clôturée ou remplacée pendant l’analyse.')
                    await self.reconcile(q,expected_identity=target_identity,
                        expected_generation=(self.started_ms,revision))
                    q=self.quotes.get(decision.symbol,q)
        except (ValueError,MarketError) as exc:io_rejection=str(exc)
        async with self.lock:
            # All mutation/validation below is synchronous: fresh stops on other
            # positions cannot wait for broker or history requests.
            try:
                if io_rejection:raise RiskRejected(io_rejection)
                validate_context(decision,context)
                if self.critical_error:raise RiskRejected('Moteur suspendu : décision annulée.')
                if expected<self.started_ms:raise RiskRejected('Cycle antérieur au dernier démarrage : décision annulée.')
                if revision!=self._settings_revision:raise RiskRejected('Paramètres modifiés pendant l’analyse ; décision annulée.')
                if self.store.get('mode')=='stopped':raise RiskRejected('Analyses arrêtées pendant l’appel IA.')
                if context.get('mode')=='management':
                    live=self.store.position(decision.symbol)
                    if target_identity is None or not live or self._position_identity(live)!=target_identity:
                        raise RiskRejected('Position de gestion clôturée ou remplacée pendant l’analyse.')
                if entry and (not self.health['market_ok'] or not self.reconciled):
                    raise RiskRejected('Données indisponibles ou réconciliation incomplète.')
                if decision.action in {'BUY','SHORT','SELL'}:
                    if q is None:raise RiskRejected('Cotation exécutable absente.')
                    self.quotes[decision.symbol]=q
                    detail['execution_quote']={'bid':q.bid,'ask':q.ask,'time_ms':q.time_ms}
                    if entry and int(time.time()*1000)-expected>65000:raise RiskRejected('Décision IA trop tardive après clôture M5 (>65 s).')
                    if entry:
                        if decision.symbol.startswith('CAP:') and self.provider_setup:raise RiskRejected('Connexion de données en cours de modification.')
                        if decision.action not in context['directions'].get(decision.symbol,[]):raise RiskRejected('Direction non admissible selon le scanner.')
                        if decision.setup not in context['direction_candidates'].get(decision.symbol,{}).get(decision.action,[]):raise RiskRejected('Configuration non admissible pour cette direction.')
                        self.rules.update(fetched_rules)
                    stats,_=statistics(self.store,self.quotes);current_day=self.day_state(stats['equity'])
                    held=self.store.positions()
                    if entry:
                        import math
                        used=portfolio_remaining_risk(held,self.quotes,now_ms=int(time.time()*1000))
                        if not math.isfinite(used):raise RiskRejected('Risque des positions ouvertes indisponible : aucune nouvelle entrée.')
                        remaining=max(0,current_day['start_equity']*cfg.daily_loss_pct/100+(stats['equity']-current_day['start_equity']))
                        budget=max(0,min(remaining,stats['equity']*cfg.max_portfolio_risk_pct/100)-used)
                        execution_cfg=cfg.model_copy(update={'symbols':context['symbols']})
                        structure_context=raw_markets[decision.symbol]
                        plan=RiskEngine.plan(decision,q,self.rules[decision.symbol],execution_cfg,stats['cash'],stats['equity'],
                            paused=self.store.get('mode')!='running',day_blocked=current_day['blocked'],
                            atr=structure_context['m5']['atr14'],now_ms=int(time.time()*1000),structure_context=structure_context,
                            portfolio_positions=held,portfolio_quotes=self.quotes,risk_budget_pct=budget/max(stats['equity'],1e-12)*100)
                        detail['structure_context']={'m5':structure_context['m5'],'h1':structure_context['h1']}
                        # The feed event can still be fresh while the local M5
                        # entry deadline has elapsed. Recheck at the last instant
                        # before booking an order, including time spent planning.
                        if int(time.time()*1000)-expected>65000:
                            raise RiskRejected('Décision IA trop tardive après clôture M5 (>65 s).')
                        self.paper.buy(plan,q,f'{key}:{decision.symbol}',decision,cfg,{**detail,'plan':asdict(plan)})
                        opened=self.store.position(decision.symbol)
                        if opened:self._set_reconciliation(opened,True)
                    else:
                        live=self.store.position(decision.symbol)
                        if not live or target_identity is None or self._position_identity(live)!=target_identity:
                            raise RiskRejected('Position de gestion clôturée ou remplacée pendant l’analyse.')
                        self.paper.sell(q,'SIGNAL_IA',detail)
                        self._set_reconciliation(live,True)
                elif decision.action=='HOLD':
                    if not self.store.position(decision.symbol):raise RiskRejected('HOLD sans position : aucun ordre.')
            except (ValueError,MarketError) as exc:rejection=str(exc)
            self.store.decision(decision,rejection,detail)
            audit=cfd_decision_record({'at':self.store.get('last_analysis_at'),
                'symbol':decision.symbol,'action':decision.action,'reason':decision.reason,'rejection':rejection},detail)
            if audit:log.info('PAPER_CFD_DECISION %s',json.dumps(audit,ensure_ascii=False))
            if decision.action in {'BUY','SHORT','SELL'}:
                reviewed=self.quotes.get(decision.symbol)
                log.info('PAPER_ORDER_REVIEW %s',json.dumps({'at':self.store.get('last_analysis_at'),
                    'symbol':decision.symbol,'action':decision.action,'mode':context['mode'],'rejection':rejection,
                    'signal_id':key,'quote_age_ms':int(time.time()*1000)-reviewed.time_ms if reviewed else None},ensure_ascii=False))
            self.health['last_analysis_at']=self.store.get('last_analysis_at')
        return outcome

