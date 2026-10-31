"""Independent HTTP regression for withdrawn IDs and truthful import receipts."""
import json,os,pathlib,socket,subprocess,sys,tempfile,time,uuid
import httpx
ROOT=pathlib.Path(__file__).resolve().parents[1];OUT=ROOT/'evidence/lifecycle';OUT.mkdir(parents=True,exist_ok=True)
checks=[];requests=[]
def require(ok,msg):
    if not ok:raise AssertionError(msg)
def check(name,fn):
    try:fn();checks.append({'test':name,'passed':True})
    except Exception as e:checks.append({'test':name,'passed':False,'error':str(e)})
def main():
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    data=tempfile.mkdtemp(prefix='relay-lifecycle-');base='http://127.0.0.1:'+str(port)
    log=(OUT/'server.log').open('w');proc=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--app-dir',str(ROOT),'--host','127.0.0.1','--port',str(port)],env={**os.environ,'DATA_DIR':data},stdout=log,stderr=subprocess.STDOUT)
    def call(method,path,**kw):
        with httpx.Client(trust_env=False,timeout=30) as c:r=c.request(method,base+path,**kw)
        try:v=r.json()
        except ValueError:v={'body':r.text[:500]}
        requests.append({'method':method,'path':path,'input':kw,'status':r.status_code,'response':v});return r.status_code,v
    try:
        for _ in range(150):
            try:
                with httpx.Client(trust_env=False,timeout=1) as c:r=c.get(base+'/api/projects')
                if r.status_code==200:break
            except httpx.HTTPError:pass
            if proc.poll() is not None:raise RuntimeError('Lifecycle HTTP server exited')
            time.sleep(.1)
        _,project=call('POST','/api/projects',json={'name':'撤回生命周期独立样例'});scope=project['scope_id'];prefix=f'/api/projects/{scope}'
        def state():return call('GET',prefix+'/state')[1]
        def write(route,payload=None,form=None,action_id=None,revision=None):
            controls={'action_id':action_id or 'life-'+uuid.uuid4().hex,'expected_revision':state()['revision'] if revision is None else revision}
            return call('POST',prefix+route,**({'data':{**form,**controls}} if form is not None else {'json':{**(payload or {}),**controls}}))
        review={'record_id':'withdrawn-1','date':'2026-10-31','completed':'应被撤回的旧事实','blockers':'','decisions':'旧决定','next_steps':'旧下一步'}
        require(write('/reviews',review)[0]==200,'Initial review write failed');require(write('/reviews/withdrawn-1/withdraw')[0]==200,'Withdrawal failed')
        def reuse():
            before=state();code,_=write('/reviews',{**review,'completed':'新输入但复用了撤回编号'});require(code in (400,409,422),'Reusing withdrawn ID falsely reports save success');require(state()==before,'Rejected withdrawn ID appended an event')
        check('reusing a withdrawn record ID gives a clear atomic refusal',reuse)
        md='# 旧样例\n记录编号: withdrawn-1\n日期: 2026-10-31\n已完成: 应被撤回的旧事实\n困难: \n决定: 旧决定\n下一步: 旧下一步\n\n| ID | 标题 | 状态 | 负责人 | 截止日期 |\n|---|---|---|---|---|\n|safe-task|仍可导入的独立任务|todo|小禾|2026-11-02|\n'
        original={}
        def old_import():
            aid='old-import';rev=state()['revision'];code,receipt=write('/import',form={'format':'markdown','content':md},action_id=aid,revision=rev);require(code==200,'Old valid sample import failed');require(receipt['imported_tasks']==1 and receipt['imported_reviews']==0,'Import receipt counts a withdrawn review as new');require(not state()['reviews'] and len(state()['tasks'])==1,'Old import restored withdrawn facts');original.update(aid=aid,revision=rev,receipt=receipt)
        check('old import preserves withdrawal and reports only effective additions',old_import)
        def repeat_receipts():
            code,new=write('/import',form={'format':'markdown','content':md});require(code==200 and new['imported_tasks']==0 and new['imported_reviews']==0,'Repeated import receipt reports nonexistent additions')
            require(original,'Original import receipt not available')
            code,retry=write('/import',form={'format':'markdown','content':md},action_id=original['aid'],revision=original['revision']);require(code==200 and retry['source_id']==original['receipt']['source_id'] and retry['imported_tasks']==1 and retry['imported_reviews']==0,'Idempotent retry did not preserve the original addition counts')
        check('new-action duplicate and same-action retry have truthful receipts',repeat_receipts)
        def new_id():
            code,_=write('/reviews',{**review,'record_id':'replacement-2','completed':'使用新编号的新事实'});require(code==200,'New review ID could not be saved');_,report=call('GET',prefix+'/report');require('使用新编号的新事实' in report['report'] and '应被撤回的旧事实' not in report['report'],'Report mixes withdrawn facts with replacement')
        check('new record IDs can continue without restoring withdrawn facts',new_id)
        result={'passed':all(x['passed'] for x in checks),'checks':checks,'data':data,'requests':requests};(OUT/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8');print(json.dumps({'passed':result['passed'],'checks':checks},ensure_ascii=False));return 0 if result['passed'] else 1
    finally:
        if proc.poll() is None:proc.terminate();proc.wait(timeout=30)
        log.close()
if __name__=='__main__':raise SystemExit(main())
