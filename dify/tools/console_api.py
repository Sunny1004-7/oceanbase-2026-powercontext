"""Normal local Dify console API client. Local passwords/tokens never printed."""
from __future__ import annotations
import base64,datetime,json,pathlib,re,secrets,time
import httpx
ROOT=pathlib.Path(__file__).resolve().parents[1]
APP=ROOT
PRIVATE=APP/'.runtime.local.json'
EVIDENCE=ROOT/'evidence'/'runtime';EVIDENCE.mkdir(parents=True,exist_ok=True)

def private(): return json.loads(PRIVATE.read_text()) if PRIVATE.exists() else {}
def save_private(data):
    PRIVATE.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');PRIVATE.chmod(0o600)

def scrub(value):
    if isinstance(value,dict):
        return {k:('[REDACTED]' if k.lower() in ('authorization','password','api_key','token','access_token','refresh_token','csrf_token','cookie') else scrub(v))
                for k,v in value.items() if k.lower() not in ('reasoning_content','thinking','reasoning','signature')}
    if isinstance(value,list): return [scrub(v) for v in value]
    if isinstance(value,str):
        value=re.sub(r'<think>.*?</think>','[private reasoning omitted]',value,flags=re.S)
        secret_values=[v for k,v in private().items() if any(s in k.lower() for s in ('token','password','key','cookie'))]
        from dotenv import dotenv_values
        for file in (APP/'.env.local',):
            if file.exists(): secret_values.extend(v for k,v in dotenv_values(file).items() if any(s in k for s in ('KEY','TOKEN','PASSWORD')) and v)
        for s in secret_values:
            if isinstance(s,str) and len(s)>16:value=value.replace(s,'[REDACTED]')
        return value
    return value

def evidence(name,data):
    (EVIDENCE/(name+'.json')).write_text(json.dumps(scrub(data),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

class Console:
    def __init__(self,bootstrap=False):
        from dotenv import dotenv_values
        env=dotenv_values(APP/'.env.local')
        self.client=httpx.Client(base_url=(env.get('DIFY_CONSOLE_URL') or 'http://127.0.0.1:8383').rstrip('/')+'/console/api',trust_env=False,timeout=90)
        cfg=private()
        if 'email' not in cfg:
            if not bootstrap: raise RuntimeError('Local Dify bootstrap is required')
            cfg.update(email=env.get('DIFY_ADMIN_EMAIL') or 'r13-local@example.com',password=env.get('DIFY_ADMIN_PASSWORD') or 'R13'+secrets.token_urlsafe(24)+'9a')
            save_private(cfg)
        status=self.client.get('/setup');status.raise_for_status()
        if status.json().get('step')!='finished':
            if not bootstrap: raise RuntimeError('Local Dify setup not finished')
            result=self.client.post('/setup',json={'email':cfg['email'],'password':cfg['password'],'name':'R13 本地作品','language':'zh-Hans'})
            if result.status_code not in (200,201):raise RuntimeError('Setup '+str(result.status_code)+' '+result.text[:400])
            evidence('setup',{'status_code':result.status_code,'result':result.json(),'synthetic_local_account':True})
        result=self.client.post('/login',json={'email':cfg['email'],'password':base64.b64encode(cfg['password'].encode()).decode(),'remember_me':True})
        if result.status_code!=200 or result.json().get('result')!='success':raise RuntimeError('Login '+str(result.status_code)+' '+result.text[:300])
        for c in self.client.cookies.jar:
            if 'csrf' in c.name:self.client.headers['X-CSRF-Token']=c.value
        self.cfg=cfg
    def request(self,method,path,**kwargs):
        r=self.client.request(method,path,**kwargs)
        if r.status_code>=400:
            message=scrub(r.text)
            raise RuntimeError(method+' '+path+' '+str(r.status_code)+' '+message[:900])
        return r
    def json(self,method,path,**kwargs):return self.request(method,path,**kwargs).json()

if __name__=='__main__':
    c=Console(bootstrap=True)
    print(json.dumps({'local_setup':True,'normal_login':True,'console_status':c.request('GET','/apps',params={'page':1,'limit':20}).status_code,'secrets_printed':False}))
