"""Finite MiniMax development calls; credentials and reasoning are never persisted."""
import datetime, hashlib, json, pathlib, re, time, urllib.parse
import httpx
from dotenv import dotenv_values
ROOT=pathlib.Path(__file__).resolve().parent

def configuration():
    values=dotenv_values(ROOT/'.env.local')
    required=['MINIMAX_API_KEY','MINIMAX_BASE_URL','MINIMAX_MODEL']
    if not all(values.get(k) for k in required):raise RuntimeError('MiniMax configuration incomplete')
    base=values['MINIMAX_BASE_URL'].rstrip('/')
    parsed=urllib.parse.urlparse(base)
    if parsed.scheme!='https' or parsed.hostname not in ['api.minimax.cn','api.minimax.io','api.minimaxi.com'] or parsed.username or parsed.query:
        raise RuntimeError('Use the official HTTPS MiniMax base URL without credentials/query')
    return values['MINIMAX_API_KEY'],base,values['MINIMAX_MODEL']

def invoke(messages, out_dir, max_tokens=8192):
    key,base,model=configuration()
    out=pathlib.Path(out_dir);out.mkdir(parents=True,exist_ok=True)
    request={'model':model,'messages':messages,'temperature':0.1,'max_tokens':max_tokens}
    (out/'request.json').write_text(json.dumps(request,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    start=time.perf_counter(); timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat()
    with httpx.Client(timeout=180) as client:
        response=client.post(base+'/chat/completions',headers={'Authorization':'Bearer '+key},json=request)
    if response.status_code!=200:raise RuntimeError(f'MiniMax HTTP {response.status_code}; response body omitted to protect credentials')
    raw=response.json()
    if not raw.get('choices'):raise RuntimeError('MiniMax did not return choices; check account entitlement')
    message=raw['choices'][0]['message']; content=message.get('content') or ''
    content=re.sub(r'<think>.*?</think>','',content,flags=re.S).strip()
    # Deliberately exclude reasoning_content and all auth headers from evidence.
    public={'timestamp_utc':timestamp,'duration_ms':(time.perf_counter()-start)*1000,'base_url':base,'requested_model':model,'returned_model':raw.get('model'),'id':raw.get('id'),'request_id':response.headers.get('x-request-id') or response.headers.get('trace-id'),'usage':raw.get('usage'),'finish_reason':raw['choices'][0].get('finish_reason'),'content':content,'request_sha256':hashlib.sha256((out/'request.json').read_bytes()).hexdigest(),'reasoning_saved':False}
    (out/'response.json').write_text(json.dumps(public,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return public

if __name__=='__main__':
    _,base,model=configuration()
    print(json.dumps({'configured':True,'base_url':base,'model':model}),flush=True)
    result=invoke([{'role':'user','content':'Reply with the single word OK.'}],ROOT/'evidence'/'connectivity',1024)
    print(json.dumps({k:result[k] for k in ['id','returned_model','usage','duration_ms','finish_reason']},ensure_ascii=False))
