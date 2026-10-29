"""Bounded private CFD decision diagnostics; never serialize full contexts."""


def cfd_decision_record(row, detail):
    if not isinstance(detail, dict):return None
    context=detail.get('context') or {}
    if not isinstance(context,dict):return None
    scanner=context.get('scanner') or detail.get('scanner') or {}
    selected=scanner.get('selected',[]) if isinstance(scanner,dict) else []
    plans=context.get('entry_plans') or []
    plans=[p for p in plans if isinstance(p,dict)] if isinstance(plans,list) else []
    candidates=context.get('candidates') or {}
    names=[*selected,*candidates] if isinstance(selected,list) and isinstance(candidates,dict) else []
    names.extend(p.get('symbol') for p in plans)
    symbols=sorted({s for s in names if isinstance(s,str) and s.startswith('CAP:')})[:12]
    if not symbols:return None
    portfolio=context.get('portfolio') or detail.get('portfolio') or {}
    if not isinstance(portfolio,dict):portfolio={}
    fields=('open_count','max_positions','risk_known','risk_used_usdt','risk_available_usdt',
            'max_trade_risk_pct','max_portfolio_risk_pct')
    safe_portfolio={k:v for k in fields if isinstance((v:=portfolio.get(k)),(int,float,bool)) or v is None}
    return {'at':str(row.get('at') or '')[:50],'mode':context.get('mode'),
            'action':str(row.get('action') or '')[:12],'symbol':str(row.get('symbol') or '')[:80],
            'reason':str(row.get('reason') or '')[:1400],
            'rejection':str(row['rejection'])[:500] if row.get('rejection') else None,
            'selected_cfd':symbols,'offered_cfd_plans':[str(p.get('id') or '')[:180]
                for p in plans if p.get('symbol') in symbols][:24],
            'entries_paused':context.get('entries_paused'),'portfolio':safe_portfolio}
