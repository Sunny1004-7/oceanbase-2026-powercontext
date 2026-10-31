"""Real SDK/HTTP adapter checks; synthetic state actions are not game/model runs."""
import argparse,concurrent.futures,hashlib,json,pathlib,uuid,httpx
from dotenv import dotenv_values
ROOT=pathlib.Path(__file__).resolve().parents[1]
def main():
    p=argparse.ArgumentParser();p.add_argument('--url',default='http://127.0.0.1:4335');p.add_argument('--output',type=pathlib.Path,default=ROOT/'evidence/runtime/adapter-contract.json');a=p.parse_args()
    env=dotenv_values(ROOT/'.env.local');headers={'Authorization':'Bearer '+env['E05_ADAPTER_TOKEN']}
    player='Test-'+uuid.uuid4().hex[:12];other='Other-'+uuid.uuid4().hex[:12];requests=[];checks=[]
    def call(method,path,**kwargs):
        with httpx.Client(trust_env=False,timeout=30) as client:r=client.request(method,a.url+path,headers=headers,**kwargs)
        value=r.json();requests.append({'method':method,'path':path,'status':r.status_code,'response':value});return r.status_code,value
    def body(action,revision):
        return {'player':player,'action_id':action,'expected_revision':revision,'request_hash':hashlib.sha256(action.encode()).hexdigest(),
                'state':{'stage':0,'completed':[],'visits':0},'reply':'适配器一致性测试回执 '+action,'action':'contract-probe','input_text':'probe '+action}
    def check(name,value):
        checks.append({'test':name,'passed':bool(value)})
        if not value:raise AssertionError(name)
    _,empty=call('GET','/state/'+player);check('empty',empty['revision']==0)
    first=body('first',0);code,result=call('POST','/commit',json=first);check('first durable commit',code==200 and result['revision']==1)
    code,retry=call('POST','/commit',json=first);check('old revision same action retry',code==200 and retry['duplicate'] and retry['source_id']==result['source_id'])
    changed=body('first',1);changed['request_hash']=hashlib.sha256(b'changed').hexdigest()
    code,_=call('POST','/commit',json=changed);check('same action different input refused',code==409)
    code,_=call('POST','/commit',json=body('stale',0));check('stale revision refused',code==409)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(lambda action:call('POST','/commit',json=body(action,1)),['race-a','race-b']))
    check('one concurrent winner',sorted(x[0] for x in outcomes)==[200,409])
    for i in range(2,27):
        code,result=call('POST','/commit',json=body('page-'+str(i),i));check('page-write-'+str(i),code==200)
    _,all_state=call('GET','/state/'+player);check('full pagination',all_state['revision']==27 and all_state['action_count']==27 and len(all_state['actions'])==27)
    _,lookup=call('GET','/state/'+player+'?lookup_action=first');check('bounded old action lookup',list(lookup['actions'])==['first'] and lookup['revision']==27)
    _,isolation=call('GET','/state/'+other);check('player isolation',isolation['revision']==0 and not isolation['state']['completed'])
    code,_=call('GET','/sources/'+other+'/'+all_state['last_source']['source_id']);check('cross-player source is hidden',code==404)
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps({'passed':True,'player':player,'kind':'adapter contract only, no model calls','checks':checks,'requests':requests},ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'passed':True,'checks':len(checks),'player':player,'output':str(a.output)}))
if __name__=='__main__':main()
