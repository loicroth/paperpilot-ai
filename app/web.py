from __future__ import annotations
import asyncio, base64, csv, io, json, secrets, sqlite3
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .config import Environment, Settings, ROOT
from .engine import TradingEngine
from .metrics import statistics
from .storage import Store, utcnow


def create_app(env=None,store=None,engine=None,background=True):
    env=env or Environment()
    db=store or Store(env.data_dir/'paperpilot.sqlite3')
    bot=engine or TradingEngine(db,env)
    csrf=secrets.token_urlsafe(32)
    @asynccontextmanager
    async def lifespan(app):
        if background: await bot.start()
        try: yield
        finally:
            if background: await bot.close()
            if store is None: db.close()
    app=FastAPI(title='PaperPilot AI • Simulation uniquement',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.engine=bot; app.state.store=db
    if not env.password:
        app.add_middleware(TrustedHostMiddleware,allowed_hosts=['localhost','127.0.0.1','[::1]','testserver'])
    @app.middleware('http')
    async def secure(request:Request,call_next):
        if request.url.path == '/healthz' and request.method == 'GET':
            return JSONResponse({'ok': True}, headers={'Cache-Control':'no-store'})
        if env.password:
            authorized=False
            try:
                scheme,encoded=request.headers.get('authorization','').split(' ',1)
                user,password=base64.b64decode(encoded,validate=True).decode().split(':',1)
                authorized=scheme.lower()=='basic' and secrets.compare_digest(user,'paperpilot') and secrets.compare_digest(password,env.password)
            except (ValueError,UnicodeError): pass
            if not authorized: return Response('Authentification requise. Identifiant : paperpilot.',401,headers={'WWW-Authenticate':'Basic realm="PaperPilot", charset="UTF-8"'})
        if request.method in {'POST','PUT','DELETE','PATCH'}:
            if not secrets.compare_digest(request.headers.get('x-csrf-token',''),csrf): return JSONResponse({'detail':'Jeton de sécurité invalide. Actualisez la page.'},403)
            origin=request.headers.get('origin')
            expected_origin=getattr(env,'public_origin','') or str(request.base_url).rstrip('/')
            if origin and origin.rstrip('/')!=expected_origin: return JSONResponse({'detail':'Origine de requête refusée.'},403)
            if int(request.headers.get('content-length','0') or 0)>16384: return JSONResponse({'detail':'Requête trop volumineuse.'},413)
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response
    @app.get('/')
    async def index(): return FileResponse(ROOT/'app/static/index.html')
    app.mount('/static',StaticFiles(directory=ROOT/'app/static'),name='static')
    @app.get('/api/state')
    async def state():
        try:
            stats,p=statistics(db,bot.quotes)
            day=datetime.now(timezone.utc).strftime('%Y-%m-%d')
            usage=db.rows('SELECT calls,tokens FROM usage WHERE day=?',(day,))
            usage=usage[0] if usage else {'calls':0,'tokens':0}
            cfg=bot.settings()
            usage.update(max_calls=cfg.max_daily_calls,max_tokens=cfg.max_daily_tokens)
            decisions=db.rows('SELECT * FROM decisions ORDER BY id DESC LIMIT 100')
            for row in decisions: row['detail']=json.loads(row['detail'])
            # Sample the long history for a bounded browser payload, exports keep full history.
            series=db.rows('SELECT at,equity,btc_equity FROM equity ORDER BY id')
            if len(series)>1500: series=series[::max(1,len(series)//1499)]+series[-1:]
            health=dict(bot.health)
            if health['last_quote_at']:
                age=(datetime.now(timezone.utc)-datetime.fromisoformat(health['last_quote_at'])).total_seconds()
                if age>20: health.update(market_ok=False,status='DONNÉES INDISPONIBLES',message='Dernière cotation trop ancienne. Aucune nouvelle entrée autorisée.')
            return {'settings':cfg.model_dump(),'control':{'mode':db.get('mode')},'health':health,'stats':stats,'position':p,
                'quotes':[{**asdict(q),'spread_bps':(q.ask-q.bid)/q.bid*10000} for q in bot.quotes.values()],
                'decisions':decisions,'trades':db.trades()[:250], 'equity':series,
                'events':db.rows('SELECT at,level,message FROM events ORDER BY id DESC LIMIT 60'),
                'usage':usage,'csrf_token':csrf,'ai_configured':bool(env.api_key),'day_state':db.get('day_state'),
                'paper_only':True,'session_started':db.get('session_started')}
        except sqlite3.Error:
            bot.health.update(market_ok=False,status='ERREUR',message='Base de données indisponible : entrées suspendues.')
            raise HTTPException(503,'Base de données indisponible.')
    @app.post('/api/control')
    async def control(request:Request):
        body=await request.json(); action=body.get('action')
        modes={'start':'running','pause':'paused','stop':'stopped'}
        if action not in modes: raise HTTPException(400,'Commande inconnue.')
        async with bot.lock:
            if action=='start':
                bot.critical_error=None
                bot.started_ms=int(__import__('time').time()*1000)
                bot.health['ai_error']=None
            db.set('mode',modes[action])
            db.event({'start':'Simulation démarrée. Seules les futures clôtures M5 sont analysées.',
                'pause':'Nouvelles entrées en pause ; protection et sorties IA conservées.',
                'stop':'Analyses arrêtées ; protection de la position conservée tant que le serveur fonctionne.'}[action])
        return {'ok':True,'mode':modes[action]}
    @app.put('/api/settings')
    async def settings(request:Request):
        try: cfg=Settings.model_validate(await request.json())
        except Exception as exc: raise HTTPException(422,str(exc)[:1000])
        p=db.position()
        if p and p['symbol'] not in cfg.symbols: raise HTTPException(409,'Conservez le marché de la position ouverte jusqu’à sa clôture.')
        try: await bot.market.rules(cfg.symbols)
        except Exception as exc: raise HTTPException(400,f'Validation Binance impossible : {str(exc)[:300]}')
        async with bot.lock:
            db.set('settings',cfg.model_dump()); bot._settings_revision+=1
            # Re-evaluate a tighter daily threshold immediately.
            stats,_=statistics(db,bot.quotes); bot.day_state(stats['equity'])
            db.event('Paramètres enregistrés. Les frais de chaque position restent ceux de son ouverture.')
        return {'ok':True}
    @app.post('/api/reset')
    async def reset(request:Request):
        body=await request.json()
        if body.get('confirmation')!='REINITIALISER': raise HTTPException(400,'Confirmation REINITIALISER requise.')
        async with bot.lock:
            backup_dir=env.data_dir/'backups'; backup_dir.mkdir(parents=True,exist_ok=True)
            name='avant_reset_'+datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')+'.sqlite3'
            with sqlite3.connect(backup_dir/name) as target: db.conn.backup(target)
            db.reset(force=True); bot.started_ms=int(__import__('time').time()*1000); bot._settings_revision+=1
            bot.reconciled=True; bot.health['last_analysis_at']=None
        return {'ok':True,'capital':900,'backup':name}
    def csv_response(rows,fields,filename):
        buffer=io.StringIO(newline=''); buffer.write('\ufeff')
        writer=csv.DictWriter(buffer,fields,extrasaction='ignore'); writer.writeheader()
        for row in rows:
            # Prevent spreadsheet formulas originating in human/model text.
            safe={k:("'"+v if isinstance(v,str) and v[:1] in {'=','+','-','@'} else v) for k,v in row.items()}
            writer.writerow(safe)
        return Response(buffer.getvalue(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="{filename}"'})
    @app.get('/api/export/trades.csv')
    async def export_trades():
        return csv_response(db.trades(),['id','symbol','opened_at','closed_at','entry','exit','quantity','fees','net_pnl','risk_usdt','entry_reason','exit_reason'],'trades_paper.csv')
    @app.get('/api/export/stats.csv')
    async def export_stats():
        stats,_=statistics(db,bot.quotes)
        rows=[{'statistique':k,'valeur':json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v} for k,v in stats.items()]
        return csv_response(rows,['statistique','valeur'],'statistiques_paper.csv')
    return app
