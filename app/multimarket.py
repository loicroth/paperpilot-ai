"""Read-only routing, demo feed credentials and observed USD/USDT conversion.

There are no broker order endpoints, signed Binance endpoints or live hosts here.
"""
from __future__ import annotations
import asyncio, json, math, os, tempfile, time
from dataclasses import replace
from pathlib import Path
import httpx
from .market import BinanceClient, MarketError
from .capital import CapitalClient

class DollarConversion:
    def __init__(self, client=None):
        self.client=client or httpx.AsyncClient(timeout=8,follow_redirects=False)
        self.owned=client is None; self.cached=None; self.lock=asyncio.Lock()
        self.error=None
    async def rates(self):
        async with self.lock:
            if self.cached and 0<=int(time.time()*1000)-self.cached['at']<2000:
                return self.cached
            started=time.monotonic()
            try:
                response=await self.client.get('https://api.kraken.com/0/public/Ticker',params={'pair':'USDTUSD'})
                response.raise_for_status(); data=response.json()
                if data.get('error') or len(data.get('result',{}))!=1: raise ValueError()
                row=next(iter(data['result'].values()))
                bid,ask=float(row['b'][0]),float(row['a'][0])
                if not all(math.isfinite(v) for v in [bid,ask]) or not .8<=bid<=ask<=1.2 or time.monotonic()-started>8:
                    raise ValueError()
                # Public ticker has no exchange event time. Label reception explicitly.
                self.cached={'bid':1/ask,'ask':1/bid,'at':int(time.time()*1000),'source':'Kraken USDT/USD · réception REST'}
                self.error=None
                return self.cached
            except Exception:
                self.error='Conversion USD/USDT indisponible : entrées CFD bloquées.'
                raise MarketError(self.error) from None
    async def close(self):
        if self.owned: await self.client.aclose()

class MultiMarket:
    def __init__(self, env, binance=None, capital=None, fx=None):
        self.binance=binance or BinanceClient(env.binance_url)
        directory=getattr(env,'data_dir',None)
        self.credentials_path=Path(directory)/'capital-demo-credentials.json' if directory else None
        credentials=self._load()
        self.capital=capital or CapitalClient(
            api_key=(os.getenv('CAPITAL_API_KEY') or credentials.get('api_key','')),
            identifier=(os.getenv('CAPITAL_IDENTIFIER') or credentials.get('identifier','')),
            api_password=(os.getenv('CAPITAL_API_PASSWORD') or credentials.get('api_password','')))
        self.fx=fx or DollarConversion();self.catalog_rows=[];self.catalog_error=None
        self.catalog_at=0.;self.catalog_limit=0;self.setup_lock=asyncio.Lock()
    def _load(self):
        p=self.credentials_path
        if not p or not p.exists(): return {}
        if p.is_symlink() or not p.is_file(): raise ValueError('Fichier de connexion CFD non autorisé.')
        try:
            data=json.loads(p.read_text())
            if not isinstance(data,dict): raise ValueError()
            return {k:str(data.get(k,'')) for k in ('api_key','identifier','api_password')}
        except Exception: raise ValueError('Configuration CFD illisible ; corrigez le fichier de connexion serveur.') from None
    def _save(self, credentials):
        p=self.credentials_path
        if p is None: raise ValueError('Dossier persistant absent.')
        p.parent.mkdir(parents=True,exist_ok=True)
        if p.is_symlink(): raise ValueError('Destination de connexion non autorisée.')
        fd,tmp=tempfile.mkstemp(prefix='.capital-',dir=p.parent)
        try:
            os.fchmod(fd,0o600)
            with os.fdopen(fd,'w') as f:
                json.dump(credentials,f);f.flush();os.fsync(f.fileno())
            os.replace(tmp,p)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
    async def setup_capital(self, credentials):
        async with self.setup_lock:
            client=CapitalClient(**credentials)
            try:
                await client.server_time()
                # An authenticated read confirms the demo session, never places an order.
                rows=await client.catalog(3)
                self._save(credentials)
            except Exception:
                await client.close()
                raise MarketError('Connexion démo refusée ou impossible. Vérifiez la clé, l’e-mail et le mot de passe API personnalisé.') from None
            old=self.capital;self.capital=client
            self.catalog_rows=rows;self.catalog_at=0.;self.catalog_error=None
            await old.close()
    async def server_time(self):
        try: return await self.binance.server_time()
        except MarketError:
            if self.capital.configured: return await self.capital.server_time()
            raise
    async def liquid_symbols(self,limit): return await self.binance.liquid_symbols(limit)
    async def cfd_symbols(self,limit):
        if not self.capital.configured:
            self.catalog_error='Compte de cotations Capital.com démo non connecté.'
            return []
        try:
            if time.monotonic()-self.catalog_at>900 or limit!=self.catalog_limit:
                self.catalog_rows=await self.capital.catalog(limit)
                self.catalog_at=time.monotonic();self.catalog_limit=limit
            self.catalog_error=None
            return [r['symbol'] for r in self.catalog_rows if r.get('supported') and r.get('market_status',r.get('status'))=='TRADEABLE'][:limit]
        except Exception as exc:
            self.catalog_error=str(exc)[:200]
            return []
    def describe(self,symbol):
        if symbol.startswith('CAP:'):
            row=next((r for r in self.catalog_rows if r['symbol']==symbol),{})
            return {'label':row.get('label',symbol[4:]),'asset_class':row.get('asset_class','cfd'),'provider':'Capital.com démo'}
        return {'label':symbol,'asset_class':'crypto','provider':'Binance Spot'}
    def status(self):
        configured=self.capital.configured
        return {'capital':{'configured':configured,'connected':configured and bool(self.catalog_at) and not self.catalog_error,
            'error':self.catalog_error,'label':'Capital.com démo',
            'supported':[r for r in self.catalog_rows if r.get('supported')],
            'blocked':[r for r in self.catalog_rows if not r.get('supported')]},
            'fx':{'configured':True,'source':'Kraken USDT/USD','last':self.fx.cached,'error':self.fx.error}}
    async def quote(self,symbol):
        if not symbol.startswith('CAP:'): return await self.binance.quote(symbol)
        q=await self.capital.quote(symbol);rates=await self.fx.rates()
        now=int(time.time()*1000)
        if not 0<=now-q.time_ms<=15000 or not 0<=now-rates['at']<=15000:
            raise MarketError('Cotation CFD ou conversion périmée.')
        return replace(q,conversion_rate=rates['ask'],conversion_bid=rates['bid'],conversion_ask=rates['ask'])
    async def rules(self,symbols):
        crypto=[s for s in symbols if not s.startswith('CAP:')];cfd=[s for s in symbols if s.startswith('CAP:')]
        a,b=await asyncio.gather(self.binance.rules(crypto),self.capital.rules(cfd))
        return {**a,**b}
    async def candles(self,symbol,*args,**kwargs):
        client=self.capital if symbol.startswith('CAP:') else self.binance
        return await client.candles(symbol,*args,**kwargs)
    async def close(self):
        await asyncio.gather(self.binance.close(),self.capital.close(),self.fx.close())
