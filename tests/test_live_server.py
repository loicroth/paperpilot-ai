"""Opt-in REAL public-data server smoke. Never authenticates to OpenAI or Binance."""
import json, os, socket, subprocess, sys, time
from pathlib import Path
import httpx, pytest

@pytest.mark.skipif(os.getenv('PAPER_LIVE_TEST')!='1',reason='Accès réseau Binance explicite : PAPER_LIVE_TEST=1')
def test_server_with_real_binance_and_single_instance_lock(tmp_path):
    root=Path(__file__).resolve().parent.parent
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    env=dict(os.environ,DATA_DIR=str(tmp_path),PORT=str(port),HOST='127.0.0.1',OPENAI_API_KEY='',DASHBOARD_PASSWORD='')
    with (tmp_path/'server.log').open('w') as log:
        process=subprocess.Popen([sys.executable,'-m','app.run'],cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT)
        try:
            with httpx.Client(trust_env=False,timeout=5) as client:
                deadline=time.monotonic()+50
                state=None
                while time.monotonic()<deadline:
                    assert process.poll() is None, (tmp_path/'server.log').read_text()
                    try:
                        r=client.get(f'http://127.0.0.1:{port}/api/state')
                        if r.status_code==200:
                            state=r.json()
                            if state['health']['market_ok']: break
                    except httpx.RequestError: pass
                    time.sleep(.3)
                assert state and state['health']['market_ok'],state
                assert len(state['quotes'])==3 and all(q['ask']>=q['bid']>0 for q in state['quotes'])
                assert state['ai_configured'] is False
                assert state['stats']['cash']==900 and not state['position'] and not state['trades']
                assert 'IA non configurée' in state['health']['message']
                assert client.get(f'http://127.0.0.1:{port}/').status_code==200
                token=state['csrf_token']
                assert client.post(f'http://127.0.0.1:{port}/api/control',json={'action':'start'},headers={'X-CSRF-Token':token}).status_code==200
                duplicate=subprocess.run([sys.executable,'-m','app.run'],cwd=root,env=env,capture_output=True,text=True,timeout=10)
                assert duplicate.returncode==1 and 'autre moteur' in duplicate.stderr
                print(json.dumps({'live_server':True,'market_ok':True,'quotes':state['quotes'],'cash':900,'real_openai':False,'singleton_lock':True},ensure_ascii=False))
        finally:
            process.terminate()
            try: process.wait(timeout=15)
            except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
