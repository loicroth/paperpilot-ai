"""Bounded, credential-free scanner diagnostics for the dashboard and private logs."""
from collections import Counter


def scan_summary(scan):
    rows=scan.get('rows',[]) if isinstance(scan,dict) else []
    groups={name:dict(seen=0,valid=0,candidates=0,unavailable=0,evaluated=0,feasible=0) for name in ('crypto','cfd')}
    reasons={name:Counter() for name in groups};families={};examples=[]
    for row in rows:
        group='cfd' if str(row.get('symbol','')).startswith('CAP:') else 'crypto'
        status=str(row.get('status','INDISPONIBLE'))
        bucket=groups[group];bucket['seen']+=1
        if status=='INDISPONIBLE':bucket['unavailable']+=1
        else:bucket['valid']+=1
        if status=='CANDIDAT':bucket['candidates']+=1
        bounds=row.get('feasibility') or {}
        if bounds:
            bucket['evaluated']+=1
            bucket['feasible']+=int(any(b.get('status')=='feasible' for b in bounds.values()))
        family=str(row.get('asset_class') or group)
        families.setdefault(family,dict(seen=0,valid=0,candidates=0))
        families[family]['seen']+=1;families[family]['valid']+=int(status!='INDISPONIBLE')
        families[family]['candidates']+=int(status=='CANDIDAT')
        if status not in {'CANDIDAT','POSITION OUVERTE'}:
            reason=str(row.get('reason') or 'Motif non précisé')[:500]
            # Timestamp details remain in examples; keep aggregate labels stable.
            for part in reason.split('; '):reasons[group][part.split(' (reçu=')[0]]+=1
        if group=='cfd' and sum(x['provider']=='cfd' for x in examples)<8:
            examples.append({'provider':group,'symbol':str(row.get('symbol',''))[:80],
                'status':status,'reason':str(row.get('reason') or '')[:500]})
    for name in groups:groups[name]['reasons']=[{'reason':r,'count':n} for r,n in reasons[name].most_common(8)]
    return {'at':scan.get('at') if isinstance(scan,dict) else None,'total':len(rows),
        'valid':sum(g['valid'] for g in groups.values()),'groups':groups,'families':families,'cfd_examples':examples}
