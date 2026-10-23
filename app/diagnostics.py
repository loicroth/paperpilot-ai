"""Read-only market smoke test; never places even a virtual order."""
import argparse, asyncio, json
from dataclasses import asdict
from .config import Environment
from .market import BinanceClient
from .indicators import snapshot

async def diagnose():
    env=Environment(); market=BinanceClient(env.binance_url)
    output={'paper_only':True,'openai_live_tested':False,'markets':{}}
    try:
        output['server_time_ms']=await market.server_time()
        rules=await market.rules(['BTCUSDT','ETHUSDT','SOLUSDT'])
        for s in rules:
            m5,h1,q=await asyncio.gather(market.candles(s,'5m',150),market.candles(s,'1h',150),market.quote(s))
            output['markets'][s]={'closed_m5':len(m5),'closed_h1':len(h1),'last_m5_close_ms':m5[-1].close_time,
                'quote':asdict(q),'rules':asdict(rules[s]),'m5_indicators':{k:v for k,v in snapshot(m5).items() if k not in {'candles','last','previous'}},
                'h1_indicators':{k:v for k,v in snapshot(h1).items() if k not in {'candles','last','previous'}}}
        print(json.dumps(output,ensure_ascii=False,indent=2,allow_nan=False)); return 0
    except Exception as exc:
        print(json.dumps({'error':str(exc),'live_verified':False},ensure_ascii=False)); return 1
    finally: await market.close()
def main():
    p=argparse.ArgumentParser(); p.add_argument('--live',action='store_true'); args=p.parse_args()
    if not args.live:
        p.error('Utilisez --live pour vérifier les données publiques réelles. Aucun appel OpenAI.')
    return asyncio.run(diagnose())
if __name__=='__main__': raise SystemExit(main())
