"""Capital.com DEMO market data only. No order/account mutation endpoints exist.

API contract: https://open-api.capital.com/ and Capital.com's official Postman
collection. Demo CFD quotes are broker demo observations, not exchange executions.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import math
import re
import time
from typing import Any

import httpx

from .market import Candle, Quote, Rules, MarketError

DEMO_BASE = 'https://demo-api-capital.backend-capital.com/api/v1'
_INTERVALS = {'1m': ('MINUTE', 60000), '5m': ('MINUTE_5', 300000), '1h': ('HOUR', 3600000)}
_TYPE_MAP = {'SHARES':'share_cfd', 'ETF':'etf_cfd', 'ETFS':'etf_cfd', 'INDICES':'index_cfd',
             'CURRENCIES':'forex_cfd', 'COMMODITIES':'commodity_cfd'}
_PRIORITY = ['US500','US100','US30','DE40','UK100','J225','GOLD','SILVER','OIL_CRUDE','OIL_BRENT',
             'EURUSD','GBPUSD','USDJPY','AUDUSD','AAPL','MSFT','NVDA','TSLA','AMZN','META','GOOGL',
             'AMD','PLTR','COIN','SPY','QQQ','IWM','DIA','GLD','SLV']


def _positive(value: Any, zero: bool = False) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0 or (not zero and number == 0):
        raise ValueError('invalid number')
    return number


def _epic(symbol: str) -> str:
    if not isinstance(symbol,str) or not re.fullmatch(r'CAP:[A-Za-z0-9_.-]{1,80}',symbol):
        raise MarketError('Symbole CFD Capital invalide.')
    return symbol[4:]


def _timestamp(value: Any) -> int:
    if not isinstance(value,str) or not value:
        raise ValueError('missing UTC timestamp')
    stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
    if stamp.tzinfo is None: stamp=stamp.replace(tzinfo=timezone.utc)
    return int(stamp.timestamp()*1000)


class CapitalClient:
    """Only public time, login and authenticated market GET requests are allowed."""
    def __init__(self, api_key: str = '', identifier: str = '', api_password: str = '',
                 client: httpx.AsyncClient | None = None):
        self.api_key=api_key.strip();self.identifier=identifier.strip();self.api_password=api_password
        self.client=client or httpx.AsyncClient(timeout=httpx.Timeout(12,connect=6),follow_redirects=False)
        self._owns_client=client is None
        self._tokens:dict[str,str]={};self._session_lock=asyncio.Lock();self._request_lock=asyncio.Lock()
        self._last_request=0.;self._last_auth=0.;self._blocked_until=0.
        self._server_ms:float|None=None;self._server_at=0.
        self.metadata:dict[str,dict]={};self._details:dict[str,dict]={};self._details_at:dict[str,float]={}
        self.last_error:str|None=None

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.identifier and self.api_password)

    async def close(self):
        self._tokens.clear()
        if self._owns_client: await self.client.aclose()

    @staticmethod
    def _allowed(method: str, path: str):
        valid=(method=='POST' and path=='/session') or (method=='GET' and bool(
            re.fullmatch(r'/(time|ping|markets|marketnavigation|markets/[A-Za-z0-9_.-]+|prices/[A-Za-z0-9_.-]+|marketnavigation/[A-Za-z0-9_.-]+)',path)))
        if not valid: raise MarketError('Opération non autorisée : données démo uniquement.')

    def _record_clock(self,response):
        try:
            value=response.headers.get('date')
            if value:
                stamp=parsedate_to_datetime(value)
                if stamp.tzinfo is None: stamp=stamp.replace(tzinfo=timezone.utc)
                self._server_ms=stamp.timestamp()*1000;self._server_at=time.monotonic()
        except (TypeError,ValueError,OverflowError): pass

    async def _send(self, method: str, path: str, *, params=None, body=None, authenticate=True):
        self._allowed(method,path)
        if time.monotonic()<self._blocked_until:
            raise MarketError('Capital démo impose une pause ; nouvelle requête différée.')
        if authenticate and not self._tokens: await self._login()
        headers=dict(self._tokens) if authenticate else {}
        if path=='/session': headers['X-CAP-API-KEY']=self.api_key
        try:
            async with self._request_lock:
                delay=.26-(time.monotonic()-self._last_request)
                if delay>0: await asyncio.sleep(delay)
                if time.monotonic()<self._blocked_until:
                    raise MarketError('Capital démo impose une pause ; nouvelle requête différée.')
                self._last_request=time.monotonic()
            response=await self.client.request(method,DEMO_BASE+path,params=params,json=body,headers=headers)
            self._record_clock(response)
            if response.status_code==429:
                try: pause=max(1,min(3600,float(response.headers.get('Retry-After','60'))))
                except ValueError: pause=60
                self._blocked_until=time.monotonic()+pause
                raise MarketError('Limite de requêtes Capital démo ; pause automatique.')
            return response
        except (httpx.HTTPError,ValueError):
            raise MarketError('Connexion Capital démo indisponible. Aucun prix fabriqué.') from None

    async def _login(self, *, force=False):
        if not self.configured:
            raise MarketError('Capital démo non configuré : clé API, identifiant et mot de passe API requis.')
        async with self._session_lock:
            if self._tokens and not force:return
            delay=1.05-(time.monotonic()-self._last_auth)
            if delay>0:await asyncio.sleep(delay)
            self._last_auth=time.monotonic()
            response=await self._send('POST','/session',body={'identifier':self.identifier,
                'password':self.api_password,'encryptedPassword':False},authenticate=False)
            if response.status_code!=200:
                self._tokens.clear()
                raise MarketError('Authentification Capital démo refusée. Vérifiez les trois identifiants et la validité de la clé API.')
            cst=response.headers.get('CST');security=response.headers.get('X-SECURITY-TOKEN')
            if not cst or not security:
                raise MarketError('Session Capital démo incomplète ; aucune analyse autorisée.')
            self._tokens={'CST':cst,'X-SECURITY-TOKEN':security}

    async def _get(self,path,params=None):
        response=await self._send('GET',path,params=params,authenticate=path!='/time')
        if response.status_code in (401,403) and path!='/time':
            self._tokens.clear();await self._login()
            response=await self._send('GET',path,params=params)
        if response.status_code!=200:
            raise MarketError(f'Données Capital démo indisponibles (HTTP {response.status_code}).')
        try:
            result=response.json()
            if not isinstance(result,dict) or result.get('errorCode'):
                raise ValueError()
            return result
        except (ValueError,TypeError):
            raise MarketError('Réponse de données Capital démo invalide.') from None

    async def server_time(self) -> int:
        if self._server_ms is None or time.monotonic()-self._server_at>60:
            data=await self._get('/time')
            try:
                value=data.get('serverTime',data.get('timestamp'))
                if value is not None:
                    stamp=float(value)
                    if 1500000000000<=stamp<=4102444800000:
                        self._server_ms=stamp;self._server_at=time.monotonic()
            except (TypeError,ValueError):pass
        if self._server_ms is None:
            raise MarketError('Horloge Capital démo indisponible ; cotations refusées.')
        return int(self._server_ms+(time.monotonic()-self._server_at)*1000)

    @staticmethod
    def _classify(row):
        raw=str(row.get('instrumentType',row.get('type',''))).upper()
        asset=_TYPE_MAP.get(raw)
        name=str(row.get('instrumentName',row.get('name','')))
        if raw=='SHARES' and re.search(r'\bETF\b|iShares|Vanguard|SPDR|Invesco',name,re.I):
            asset='etf_cfd'
        return asset,raw

    async def catalog(self,limit=40) -> list[dict]:
        if not self.configured:
            self.last_error='Capital démo non configuré.'
            return []
        data=await self._get('/markets')
        if not isinstance(data.get('markets'),list):
            raise MarketError('Catalogue Capital démo invalide.')
        grouped={k:[] for k in ['index_cfd','share_cfd','etf_cfd','forex_cfd','commodity_cfd']}
        for row in data['markets']:
            try:
                symbol='CAP:'+str(row['epic']);_epic(symbol)
                asset,raw=self._classify(row)
                if not asset:continue
                # Non-expiring CFDs only. Exchange futures need their own contract model.
                if row.get('expiry','-') not in ['-',None,'']:continue
                status=str(row.get('marketStatus','UNKNOWN'))
                label=str(row.get('instrumentName',row['epic']))
                entry={'symbol':symbol,'epic':row['epic'],'label':label,'asset_class':asset,
                    'instrument_type':raw,'market_status':status,'provider':'capital_demo',
                    'source_label':'Cotations CFD démo Capital.com', 'delay_minutes':row.get('delayTime'),
                    'update_time_utc':row.get('updateTimeUTC'),'reason':None,'supported':False}
                if status!='TRADEABLE':entry['reason']='Marché fermé ou suspendu.'
                elif row.get('delayTime')!=0:entry['reason']='Cotations retardées ou délai non précisé.'
                self.metadata[symbol]=entry
                grouped[asset].append(entry)
            except (KeyError,ValueError,TypeError,MarketError):continue
        def order(row):
            epic=row['epic'];priority=_PRIORITY.index(epic) if epic in _PRIORITY else len(_PRIORITY)
            return (row['reason'] is not None,priority,row['label'])
        limit=max(1,min(int(limit),60))
        # The limit counts usable contracts, not the first contracts inspected.
        # Unsupported currencies/multipliers must not consume a scanner slot
        # while compatible markets further down the catalogue remain unexamined.
        # Keep the same family rotation and priorities, but bound expensive
        # detail lookups when a provider catalogue contains many unsupported rows.
        pending={};blocked=[]
        for family,rows in grouped.items():
            rows.sort(key=order)
            pending[family]=[row for row in rows if row['reason'] is None]
            blocked.extend(row for row in rows if row['reason'] is not None)
        selected=[];supported=0;checked=0
        check_budget=min(120,max(15,3*limit))
        while supported<limit and checked<check_budget and any(pending.values()):
            for rows in pending.values():
                if not rows or supported>=limit or checked>=check_budget:continue
                entry=rows.pop(0);checked+=1
                try:
                    await self.rules([entry['symbol']])
                    entry['supported']=True
                    supported+=1
                except MarketError as exc:
                    entry['supported']=False
                    entry['reason']=str(exc)
                selected.append(entry)
        # Closed/delayed rows remain visible as blocked diagnostics, without
        # spending metadata requests or crowding out currently tradable rows.
        # The snapshot is intentionally bounded even for very large catalogues.
        selected.extend(blocked[:limit])
        self.last_error=None
        return selected

    async def _detail(self,symbol):
        epic=_epic(symbol)
        if symbol not in self._details or time.monotonic()-self._details_at.get(symbol,0)>300:
            self._details[symbol]=await self._get('/markets/'+epic)
            self._details_at[symbol]=time.monotonic()
        return self._details[symbol]

    async def _quote_row(self,symbol,row,detail=None):
        try:
            if row.get('marketStatus')!='TRADEABLE':raise MarketError('Marché CFD fermé ou suspendu.')
            if row.get('delayTime')!=0:raise MarketError('Cotation CFD retardée ou délai non précisé.')
            stamp=_timestamp(row.get('updateTimeUTC'))
            broker_now=await self.server_time()
            if not -1000<=broker_now-stamp<=15000:raise MarketError('Cotation CFD trop ancienne ou datée dans le futur.')
            bid,ask=_positive(row['bid']),_positive(row['offer'])
            if ask<bid or ask/bid>1.05:raise ValueError()
            detail=await self._detail(symbol) if detail is None else detail
            if detail['instrument'].get('epic')!=_epic(symbol):raise MarketError('Identité du contrat CFD différente de la cotation demandée.')
            if detail['instrument'].get('currency')!='USD':raise MarketError('Conversion de cette devise CFD non prise en charge.')
            # Preserve the observed event age across clock domains. A broker
            # clock offset must not make a valid quote appear future/stale to
            # local execution, and metadata latency must not refresh old data.
            broker_now=await self.server_time()
            event_age=broker_now-stamp
            if not -1000<=event_age<=15000:raise MarketError('Cotation CFD trop ancienne ou datée dans le futur.')
            local_stamp=int(time.time()*1000)-max(event_age,0)
            return Quote(symbol,bid,ask,local_stamp,currency='USD',market_status='TRADEABLE')
        except (KeyError,TypeError,ValueError,OverflowError):
            raise MarketError('Cotation CFD incomplète ; aucun prix de remplacement.') from None

    async def quotes(self,symbols):
        """One actual batch snapshot for up to 50 epics, independently validated."""
        if not 1<=len(symbols)<=50 or len(set(symbols))!=len(symbols):
            raise MarketError('Lot de cotations CFD invalide : 1 à 50 symboles uniques requis.')
        epics=[_epic(s) for s in symbols]
        data=await self._get('/markets',{'epics':','.join(epics)})
        # Capital uses a different documented envelope for epics queries than
        # for catalogue/searchTerm queries: nested instrument + snapshot rows.
        if not isinstance(data.get('marketDetails'),list):
            raise MarketError('Réponse CFD epics inattendue : tableau marketDetails requis.')
        by_epic={};duplicates=set()
        for row in data['marketDetails']:
            if not isinstance(row,dict) or not isinstance(row.get('instrument'),dict):continue
            epic=row['instrument'].get('epic')
            if not isinstance(epic,str) or epic not in epics:continue
            if epic in by_epic:duplicates.add(epic)
            by_epic[epic]=row
        result={}
        for symbol,epic in zip(symbols,epics):
            try:
                if epic in duplicates:raise MarketError('Contrat CFD dupliqué dans la réponse epics.')
                if epic not in by_epic:raise MarketError('Contrat CFD demandé absent de marketDetails.')
                detail=by_epic[epic];snapshot=detail.get('snapshot')
                if not isinstance(snapshot,dict):raise MarketError('Détail CFD incomplet : snapshot requis.')
                if isinstance(detail.get('dealingRules'),dict):
                    self._details[symbol]=detail;self._details_at[symbol]=time.monotonic()
                if snapshot.get('updateTimeUTC') is not None:
                    result[symbol]=await self._quote_row(symbol,snapshot,detail)
                else:
                    # The documented snapshot only supplies updateTime, whose
                    # timezone is unspecified. Never relabel it UTC or stamp a
                    # cached snapshot as fresh. A documented search response
                    # supplies its own bid/ask AND explicit updateTimeUTC.
                    search=await self._get('/markets',{'searchTerm':epic})
                    if not isinstance(search.get('markets'),list):
                        raise MarketError('Réponse CFD searchTerm inattendue : tableau markets requis.')
                    matches=[row for row in search['markets'] if isinstance(row,dict) and row.get('epic')==epic]
                    if not matches:raise MarketError('Cotation CFD exacte absente de la recherche horodatée UTC.')
                    if len(matches)!=1:raise MarketError('Cotation CFD exacte dupliquée dans la recherche UTC.')
                    result[symbol]=await self._quote_row(symbol,matches[0],detail)
            except MarketError as exc:result[symbol]=exc
        return result

    async def quote(self,symbol):
        rows=await self.quotes([symbol])
        result=rows[symbol]
        if isinstance(result,MarketError):raise result
        return result

    @staticmethod
    def _financing_rates(instrument):
        """Observed daily percentage adjustments, not an exact rollover model.

        A negative provider rate is a debit; positive credits are deliberately
        ignored. Missing or unverifiable intervals use the configured fallback.
        """
        result={'financing_long_daily':None,'financing_short_daily':None,'financing_source':None}
        data=instrument.get('overnightFee')
        if not isinstance(data,dict):return result
        try:
            timestamp=data['swapChargeTimestamp'];interval=data['swapChargeInterval']
            if (isinstance(timestamp,bool) or isinstance(interval,bool)
                or not math.isfinite(float(timestamp)) or not 1500000000000<=float(timestamp)<=4102444800000
                or float(interval)!=1440):return result
        except (KeyError,TypeError,ValueError,OverflowError):return result
        for source,target in [('longRate','financing_long_daily'),('shortRate','financing_short_daily')]:
            try:
                value=data[source]
                if isinstance(value,bool):continue
                rate=float(value)
                if not math.isfinite(rate):continue
                result[target]=max(0.,-rate/100.)
            except (KeyError,TypeError,ValueError,OverflowError):continue
        if any(result[key] is not None for key in ('financing_long_daily','financing_short_daily')):
            result['financing_source']='capital_api_overnight_daily_estimate'
        return result

    async def rules(self,symbols):
        result={}
        for symbol in symbols:
            detail=await self._detail(symbol)
            try:
                instrument=detail['instrument'];dealing=detail['dealingRules'];snapshot=detail['snapshot']
                if instrument.get('epic')!=_epic(symbol):raise ValueError()
                asset,raw=self._classify(instrument)
                if asset is None:raise MarketError('Classe de contrat CFD non prise en charge.')
                if instrument.get('currency')!='USD':raise MarketError('Seuls les CFD cotés en USD sont pris en charge pour cette version.')
                if instrument.get('expiry','-') not in ['-',None,'']:raise MarketError('Contrat à échéance non pris en charge.')
                if _positive(instrument['lotSize'])!=1:raise MarketError('Multiplicateur de contrat CFD non pris en charge.')
                if instrument.get('marginFactorUnit')!='PERCENTAGE':raise MarketError('Unité de marge CFD inconnue.')
                margin=_positive(instrument['marginFactor'])
                if margin>100:raise ValueError()
                values={}
                for name in ['minDealSize','maxDealSize','minSizeIncrement']:
                    rule=dealing[name]
                    if rule.get('unit')!='POINTS':raise ValueError()
                    values[name]=_positive(rule['value'])
                if values['maxDealSize']<values['minDealSize']:raise ValueError()
                if snapshot.get('scalingFactor')!=1:raise MarketError('Échelle de prix CFD non prise en charge.')
                # Provider price precision; minStepDistance concerns stop changes.
                precision=snapshot['decimalPlacesFactor']
                if isinstance(precision,bool) or not isinstance(precision,int) or not 0<=precision<=10:
                    raise ValueError()
                price_precision=10.**(-precision)
                result[symbol]=Rules(symbol,values['minDealSize'],values['maxDealSize'],values['minSizeIncrement'],
                    0.,1e30,price_precision,provider='capital_demo',currency='USD',
                    multiplier=1.,min_margin_pct=margin,asset_class=asset,allow_short=True,
                    **self._financing_rates(instrument))
            except (KeyError,TypeError,ValueError,OverflowError):
                raise MarketError('Contraintes CFD absentes ou non vérifiables : taille, précision ou marge manquante.') from None
        return result

    async def candles(self,symbol,interval='5m',limit=150,start_time=None,end_time=None):
        epic=_epic(symbol)
        if interval not in _INTERVALS or not 1<=limit<=1000:
            raise MarketError('Intervalle CFD ou nombre de bougies invalide.')
        resolution,duration=_INTERVALS[interval]
        params={'resolution':resolution,'max':limit}
        for label,value in [('from',start_time),('to',end_time)]:
            if value is not None:params[label]=datetime.fromtimestamp(value/1000,timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
        data=await self._get('/prices/'+epic,params)
        now=await self.server_time();result=[]
        try:
            rows=data['prices']
            if not isinstance(rows,list):raise ValueError()
            for row in rows:
                opened=_timestamp(row['snapshotTimeUTC']);closed=opened+duration-1
                if opened%duration!=0:raise ValueError()
                if closed>=now:continue
                if result and opened<=result[-1].close_time:raise ValueError()
                def mid(key):
                    p=row[key];bid,ask=_positive(p['bid']),_positive(p['ask'])
                    if ask<bid:raise ValueError()
                    return (bid+ask)/2
                op,hi,lo,cl=[mid(k) for k in ['openPrice','highPrice','lowPrice','closePrice']]
                if interval=='1m':
                    # A conservative envelope cannot manufacture a favourable
                    # fill when the bid/ask touched different exit levels.
                    hi=max(_positive(row['highPrice']['bid']),_positive(row['highPrice']['ask']))
                    lo=min(_positive(row['lowPrice']['bid']),_positive(row['lowPrice']['ask']))
                volume_value=row.get('lastTradedVolume')
                volume=_positive(volume_value,True) if volume_value is not None else 0.
                if not lo<=min(op,cl)<=max(op,cl)<=hi:raise ValueError()
                # Natural session gaps are retained; never invent missing candles.
                result.append(Candle(opened,closed,op,hi,lo,cl,volume,volume_available=volume_value is not None))
        except (KeyError,TypeError,ValueError,OverflowError):
            raise MarketError('Bougies CFD invalides, incomplètes ou non chronologiques.') from None
        if start_time is None and end_time is None and (not result or now-result[-1].close_time>duration+15000):
            raise MarketError('Dernière bougie CFD trop ancienne ; marché fermé ou données indisponibles.')
        return result

