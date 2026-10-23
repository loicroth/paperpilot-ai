"""Always run a single uvicorn process; hold an OS lock for the entire engine lifetime."""
import argparse, logging, sys
from logging.handlers import RotatingFileHandler
from filelock import FileLock, Timeout
from .config import Environment, Settings

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--check',action='store_true'); args=parser.parse_args()
    try:
        env=Environment(); Settings(model=env.model)
        env.data_dir.mkdir(parents=True,exist_ok=True)
        if args.check:
            print('Configuration valide. PAPER uniquement. IA '+('configurée (authentification à vérifier au premier appel).' if env.api_key else 'non configurée : page de configuration disponible, aucun trade.'))
            return 0
        logs=env.data_dir/'logs'; logs.mkdir(parents=True,exist_ok=True)
        file_handler=RotatingFileHandler(logs/'paperpilot.log',maxBytes=2000000,backupCount=4,encoding='utf-8')
        logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s',handlers=[file_handler,logging.StreamHandler()])
        logging.getLogger('httpx').setLevel(logging.WARNING)
        logging.getLogger('httpcore').setLevel(logging.WARNING)
        lock=FileLock(str(env.data_dir/'engine.lock'))
        with lock.acquire(timeout=0):
            from .storage import Store
            from .web import create_app
            import uvicorn
            app=create_app(env)
            print(f'\nTableau de bord : http://localhost:{env.port}\n900 USDT virtuels • aucun ordre réel possible. Ctrl+C pour fermer le serveur.\n')
            uvicorn.run(app,host=env.host,port=env.port,workers=1,log_level='info',proxy_headers=True,forwarded_allow_ips='127.0.0.1,::1')
        return 0
    except Timeout:
        print('ERREUR : un autre moteur utilise déjà ce dossier de données. Fermez-le avant de relancer.',file=sys.stderr); return 1
    except Exception as exc:
        print(f'ERREUR : {type(exc).__name__} : {exc}',file=sys.stderr); return 1
if __name__=='__main__': raise SystemExit(main())
