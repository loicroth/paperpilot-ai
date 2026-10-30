"""Catalog market-derived entry levels; no order, sizing or AI dependency."""
from __future__ import annotations
import math


_SETUPS = frozenset({'breakout', 'retest', 'consolidation', 'reversal', 'pullback'})


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _plan_levels(symbol, action, setup, data, quote, bound, minimum):
    if not isinstance(bound,dict) or bound.get('status')!='feasible':return None
    if 'max_net_rr' in bound and (not _positive(bound['max_net_rr']) or bound['max_net_rr']+1e-10<minimum):return None
    costs=bound.get('costs')
    if not isinstance(costs,dict) or not _positive(costs.get('entry_fill')):return None
    stop,target=bound.get('reference_stop'),bound.get('reference_target')
    if not _positive(stop) or not _positive(target):return None
    bid,ask=quote['bid'],quote['ask'];short=action=='SHORT';entry=bid if short else ask
    if not (target<entry<stop if short else stop<entry<target):return None
    structure=data.get('structure') or {}
    side=structure.get(action,{}) if isinstance(structure,dict) else {}
    anchor=bound.get('stop_reference') or (side.get('stop_reference') if isinstance(side,dict) else {}) or {}
    pivot=anchor.get('price') if isinstance(anchor,dict) else None
    def coherent(level):
        return _positive(level) and (entry<level<=stop and ask<level if short else stop<=level<entry and bid>level)
    invalidation=pivot if coherent(pivot) else stop if coherent(stop) else None
    if invalidation is None:return None
    return dict(id=f'{symbol}:{action}:{setup}',symbol=symbol,action=action,setup=setup,
        entry=entry,stop_loss=stop,take_profit=target,invalidation=invalidation)


def build_entry_plans(results, cfg):
    """Keep only coherent plans already marked feasible by market geometry.

    Stop/target values are copied unchanged. The proposed entry is the raw
    executable quote; RiskEngine applies slippage and rechecks every limit.
    Callers supply only entry candidates: held-position filtering is external.
    """
    minimum = getattr(cfg, 'min_rr', 2.)
    if not isinstance(results, (list, tuple)) or not _positive(minimum) or minimum < 2:
        return []
    plans, seen = [], set()
    for item in results:
        if not isinstance(item, (tuple, list)) or len(item) != 3:
            continue
        symbol, data, admitted = item
        if not isinstance(symbol, str) or not symbol or not isinstance(data, dict):
            continue
        if not isinstance(admitted, (list, tuple, set)):
            continue
        admitted = {s for s in admitted if isinstance(s, str)} & _SETUPS
        quote = data.get('quote')
        directions = data.get('direction_candidates')
        bounds = data.get('feasibility')
        if not all(isinstance(v, dict) for v in (quote, directions, bounds)):
            continue
        allowed_sides = data.get('directions')
        if 'directions' in data and not isinstance(allowed_sides, (list, tuple, set)):
            continue
        bid, ask = quote.get('bid'), quote.get('ask')
        if not _positive(bid) or not _positive(ask) or bid > ask:
            continue
        if quote.get('symbol', symbol) != symbol:
            continue
        cfd = symbol.startswith('CAP:')
        if cfd and not getattr(cfg, 'cfd_enabled', False):
            continue
        for action in ('BUY', 'SHORT'):
            if action == 'SHORT' and (not cfd or not getattr(cfg, 'allow_short', False)):
                continue
            if allowed_sides is not None and action not in allowed_sides:
                continue
            setups = directions.get(action)
            if not isinstance(setups, (list, tuple, set)):
                continue
            setup_bounds=(data.get('setup_feasibility') or {}).get(action,{})
            for setup in sorted(admitted & {s for s in setups if isinstance(s, str)}):
                identifier = f'{symbol}:{action}:{setup}'
                if identifier in seen:
                    continue
                bound=setup_bounds.get(setup,bounds.get(action)) if isinstance(setup_bounds,dict) else bounds.get(action)
                # A pullback must have its own zone-stop calculation, never a
                # generic bound borrowed from a nearer classic pivot.
                if setup=='pullback' and (not isinstance(setup_bounds,dict) or setup not in setup_bounds):continue
                plan=_plan_levels(symbol,action,setup,data,quote,bound,minimum)
                if plan is not None:
                    seen.add(identifier);plans.append(plan)
    return plans
