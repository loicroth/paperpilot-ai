"""Deterministic references extracted only from observed, closed M5/H1 candles.

These constraints test the coherence of a proposal; they do not estimate win odds.
No level is moved to manufacture a requested reward/risk ratio.
"""
from __future__ import annotations
import math
import time
from .indicators import pullback_reference


class StructureRejected(ValueError): pass


def structural_levels(context, entry=None, direction='LONG', now_ms=None, setup=None, reference_price=None):
    if not isinstance(context,dict) or direction not in {'LONG','SHORT'}:
        raise StructureRejected('Contexte structurel M5/H1 absent ou invalide.')
    now_ms=int(time.time()*1000) if now_ms is None else now_ms
    result={'supports':[],'resistances':[],'direction':direction}
    for timeframe in ('m5','h1'):
        data=context.get(timeframe)
        if not isinstance(data,dict) or not isinstance(data.get('candles'),list) or len(data['candles'])<5:
            raise StructureRejected('Bougies clôturées M5/H1 insuffisantes pour valider la structure.')
        bars=(context.get('h1_structure_candles') or data['candles']) if timeframe=='h1' and setup=='pullback' else data['candles']
        if not isinstance(bars,list) or len(bars)<5:
            raise StructureRejected('Historique structurel H1 absent ou invalide.')
        previous_end=-1
        for bar in bars:
            try:
                values=[float(bar[k]) for k in ('open','high','low','close')]
                start,end=int(bar['open_time']),int(bar['close_time'])
                op,hi,lo,cl=values
                if (any(not math.isfinite(v) or v<=0 for v in values) or not lo<=min(op,cl)<=max(op,cl)<=hi
                    or start<=previous_end or end<start or end>now_ms):raise ValueError()
                previous_end=end
            except (KeyError,TypeError,ValueError,OverflowError):
                raise StructureRejected('Données de structure invalides, futures ou non chronologiques.') from None
        for index in range(2,len(bars)-2):
            bar=bars[index];neighbours=bars[index-2:index]+bars[index+1:index+3]
            common={'timeframe':timeframe.upper(),'pivot_at':bar['close_time'],
                'confirmed_at':bars[index+2]['close_time'],'source':'pivot confirmé (2 bougies de chaque côté)'}
            if bar['low']<min(b['low'] for b in neighbours):
                result['supports'].append({'price':float(bar['low']),**common})
            if bar['high']>max(b['high'] for b in neighbours):
                result['resistances'].append({'price':float(bar['high']),**common})
        # A breakout/consolidation has an observed range even without a local
        # fractal pivot. Exclude the newest signal candle from this setup range.
        range_bars=bars[-7:-1]
        for key,field,fn in [('supports','low',min),('resistances','high',max)]:
            bar=fn(range_bars,key=lambda b:b[field])
            result[key].append({'price':float(bar[field]),'timeframe':timeframe.upper(),
                'pivot_at':bar['close_time'],'confirmed_at':range_bars[-1]['close_time'],
                'source':'borne observée de la consolidation précédente'})
    try:
        atr=float(context['m5']['atr14'])
        price=float(entry if entry is not None else context['m5']['last']['close'])
        market_price=float(reference_price) if reference_price is not None else price
        if not all(math.isfinite(v) and v>0 for v in (atr,price,market_price)):raise ValueError()
    except (KeyError,TypeError,ValueError):
        raise StructureRejected('Prix ou ATR de structure indisponible.') from None
    # The latest CLOSED M5 candle can confirm a retest before its low/high
    # becomes a two-sided fractal. Keep the same geometric test as the scanner;
    # this adds an observed stop reference, never removes a target obstacle.
    bars=context['m5']['candles']
    if len(bars)>=22:
        previous,last=bars[-2:]
        old_resistance=max(bar['high'] for bar in bars[-22:-2])
        old_support=min(bar['low'] for bar in bars[-22:-2])
        common={'timeframe':'M5','pivot_at':last['close_time'],
            'confirmed_at':last['close_time'],'breakout_at':previous['close_time'],
            'source':'retest clôturé après cassure'}
        if (direction=='LONG' and previous['close']>old_resistance and
            old_resistance-.5*atr<=last['low']<=old_resistance+.3*atr and
            last['close']>old_resistance):
            result['supports'].append({'price':float(min(old_resistance,last['low'])),
                'broken_level':float(old_resistance),'observed_extreme':float(last['low']),**common})
        if (direction=='SHORT' and previous['close']<old_support and
            old_support-.3*atr<=last['high']<=old_support+.5*atr and
            last['close']<old_support):
            result['resistances'].append({'price':float(max(old_support,last['high'])),
                'broken_level':float(old_support),'observed_extreme':float(last['high']),**common})
    pullback=None
    if setup=='pullback':
        instrument=context.get('instrument') or {}
        symbol=(context.get('quote') or {}).get('symbol','')
        cfd=str(symbol).startswith('CAP:') or str(instrument.get('asset_class','')).endswith('_cfd')
        pullback=pullback_reference(context['m5'],context['h1'],short=direction=='SHORT',
            require_volume=not(cfd or direction=='SHORT'),now_ms=now_ms)
        if pullback is None:
            raise StructureRejected('Reprise après repli non confirmée sur les bougies clôturées.')
        result['supports' if direction=='LONG' else 'resistances'].append(pullback)
    for key in ('supports','resistances'):
        result[key].sort(key=lambda row:(row['price'],row['confirmed_at']))
    below=[r for r in result['supports'] if r['price']<market_price]
    above=[r for r in result['resistances'] if r['price']>market_price]
    if pullback is not None:
        # This continuation strategy trades toward the next observed H1 level.
        # M5 levels remain explicit intermediate obstacles, not hidden data.
        minor=above if direction=='LONG' else below
        result['minor_obstacles']=sorted((r for r in minor if r['timeframe']=='M5'),key=lambda r:abs(r['price']-market_price))
        result['target_basis']='structure H1 observée'
        below=[r for r in below if r['timeframe']=='H1']
        above=[r for r in above if r['timeframe']=='H1']
    if direction=='LONG':
        stop=max(below,key=lambda r:(r['price'],r['confirmed_at']),default=None)
        target=min(above,key=lambda r:(r['price'],-r['confirmed_at']),default=None)
    else:
        stop=min(above,key=lambda r:(r['price'],-r['confirmed_at']),default=None)
        target=max(below,key=lambda r:(r['price'],r['confirmed_at']),default=None)
    if pullback is not None:
        # This setup protects the entire observed pullback zone, including the
        # signal wick. A nearer old pivot must not silently tighten that stop.
        stop=pullback if (pullback['price']<market_price if direction=='LONG' else pullback['price']>market_price) else None
    result.update(entry=price,atr=atr,stop_reference=stop,target_obstacle=target,
        stop_buffer=.15*atr,target_buffer=.1*atr)
    return result


def validate_structural_plan(context,entry,stop,target,*,short=False,now_ms=None,setup=None,reference_price=None):
    levels=structural_levels(context,entry,'SHORT' if short else 'LONG',now_ms,setup=setup,reference_price=reference_price)
    anchor=levels['stop_reference'];obstacle=levels['target_obstacle'];atr=levels['atr']
    if anchor is None:raise StructureRejected('Aucun pivot/support/résistance exploitable pour justifier le stop.')
    if obstacle is None:raise StructureRejected('Aucun obstacle connu pour justifier un objectif crédible : WAIT.')
    direction=-1 if short else 1
    beyond=direction*(anchor['price']-stop)
    if beyond+1e-10<levels['stop_buffer']:
        raise StructureRejected('Stop placé dans le bruit du pivot : dépassement du niveau +0,15 ATR requis.')
    if beyond>max(atr,abs(entry-anchor['price'])*.35)+1e-10:
        raise StructureRejected('Stop éloigné du pivot pertinent sans justification structurelle.')
    if direction*(obstacle['price']-target)+1e-10<levels['target_buffer']:
        raise StructureRejected('Objectif au-delà ou trop près du premier obstacle connu (marge 0,1 ATR requise).')
    if abs(entry-stop)>entry*.25:
        raise StructureRejected('Distance du stop extrême (>25 % du prix), même avec une référence structurelle.')
    levels.update(stop_loss=stop,take_profit=target,validated=True)
    return levels

