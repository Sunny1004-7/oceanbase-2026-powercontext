"""Independent acceptance written from the R13 requirements by Codex.

Run against a real PowerContext runtime and HTTP server; never mock the SDK.
The Qoder implementation must satisfy this file without weakening its checks.
"""
import concurrent.futures,json,os,pathlib,socket,subprocess,sys,tempfile,time,uuid
import httpx
ROOT=pathlib.Path(__file__).resolve().parents[1]
OUT=ROOT/'evidence'/'independent';OUT.mkdir(parents=True,exist_ok=True)
checks=[];requests=[];process=None
data=pathlib.Path(tempfile.mkdtemp(prefix='group-relay-acceptance-'))
with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
base='http://127.0.0.1:'+str(port)
log=(OUT/'server.log').open('w')

def start():
    p=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(port),'--app-dir',str(ROOT)],env={**os.environ,'DATA_DIR':str(data)},stdout=log,stderr=subprocess.STDOUT)
    for _ in range(120):
        try:
            with httpx.Client(trust_env=False,timeout=1) as c:r=c.get(base+'/api/projects')
            if r.status_code==200:return p
        except httpx.HTTPError:pass
        if p.poll() is not None:raise RuntimeError('Server exited; inspect evidence/independent/server.log')
        time.sleep(.1)
    p.terminate();p.wait(timeout=20);raise RuntimeError('Server startup timed out')

def call(method,path,**kw):
    with httpx.Client(trust_env=False,timeout=30) as c:r=c.request(method,base+path,**kw)
    try:value=r.json()
    except ValueError:value={'body':r.text[:500]}
    requests.append({'method':method,'path':path,'input':kw,'status':r.status_code,'response':value})
    return r.status_code,value

def require(ok,message):
    if not ok:raise AssertionError(message)

def test(name,fn):
    try:fn();checks.append({'test':name,'passed':True})
    except Exception as e:checks.append({'test':name,'passed':False,'error':str(e)})

def state(project):
    code,value=call('GET',f'/api/projects/{project}/state');require(code==200,'State read failed');return value

def action(project,method,path,payload=None,form=None,action_id=None,revision=None):
    rev=state(project)['revision'] if revision is None else revision
    fields={'action_id':action_id or 'action-'+uuid.uuid4().hex,'expected_revision':rev}
    if form is not None:return call(method,f'/api/projects/{project}'+path,data={**form,**fields})
    return call(method,f'/api/projects/{project}'+path,json={**(payload or {}),**fields})

def main():
    global process
    process=start();first_pid=process.pid
    _,a=call('POST','/api/projects',json={'name':'海藻验收项目','summary':'独立样例 A'})
    _,b=call('POST','/api/projects',json={'name':'课程验收项目','summary':'独立样例 B'})
    A=a['scope_id'];B=b['scope_id'];require(A!=B,'Projects share a scope')
    review={'record_id':'review-1','date':'2026-10-31','completed':'配制三组培养基','blockers':'蓝光灯待标定','decisions':'先固定盐度','next_steps':'记录七天生长曲线'}
    def review_flow():
        old=state(A)['revision'];aid='review-stable-action'
        code,receipt=action(A,'POST','/reviews',review,action_id=aid,revision=old);require(code==200,'Review save failed')
        current=state(A);require(current['reviews'][0]['completed']==review['completed'],'Review missing')
        code,retry=action(A,'POST','/reviews',review,action_id=aid,revision=old);require(code==200 and retry['source_id']==receipt['source_id'],'Same action old-revision retry failed')
        require(state(A)['revision']==current['revision'],'Retry created another event')
        code,_=action(A,'POST','/reviews',{**review,'decisions':'changed'},action_id=aid,revision=old);require(code==409,'Same action different payload accepted')
    test('review idempotency before revision check',review_flow)
    md=(ROOT/'samples'/'algae-experiment.md').read_text(encoding='utf-8')
    def imports():
        code,_=action(A,'POST','/import',form={'format':'markdown','content':md});require(code==200,'Markdown import failed')
        s=state(A);require(s['tasks'] and s['reviews'],'Import has no effective records')
        code,_=action(A,'POST','/import',form={'format':'markdown','content':md});require(code in (200,409),'Duplicate import failed unexpectedly')
        s2=state(A);require(s2['tasks']==s['tasks'] and s2['reviews']==s['reviews'],'Duplicate changed effective records')
        before=state(B)
        bad='record_id,date,completed,blockers,decisions,next_steps,task_id,title,status,owner,due_date\nx1,2026-10-31,a,,,b,t1,Valid,todo,p,2026-11-01\nx2,2026-99-99,a,,,b,t2,Invalid,BAD,p,2026-11-01\n'
        code,_=action(B,'POST','/import',form={'format':'csv','content':bad});require(code in (400,422),'Malformed CSV was accepted');require(state(B)==before,'Bad import partially wrote')
        csv='record_id,date,completed,blockers,decisions,next_steps,task_id,title,status,owner,due_date\nc1,2026-10-31,"完成接口,补齐说明",网络待检查,先做离线版,测试导出,course-task,验证导出,todo,小叶,2026-11-04\n'
        code,_=action(B,'POST','/import',form={'format':'csv','content':csv});require(code==200,'Quoted CSV import failed')
        require('完成接口,补齐说明' in json.dumps(state(B),ensure_ascii=False),'Quoted CSV content lost')
        require(not any(t['id']=='course-task' for t in state(A)['tasks']),'Project tasks leaked')
    test('MD CSV duplicate and atomic validation',imports)
    def edit_task():
        task=state(A)['tasks'][0];tid=task['id'];changed='完成后引用修改事件'
        code,receipt=action(A,'PUT','/tasks/'+tid,{'title':changed,'status':'done'});require(code==200,'Task update failed')
        task=next(t for t in state(A)['tasks'] if t['id']==tid)
        require(task['title']==changed and task['status']=='done' and task['source_id']==receipt['source_id'],'Current task cites stale source')
        require(task.get('created_at') or task.get('updated_at'),'Task source time absent')
        code,source=call('GET',f'/api/projects/{A}/sources/'+task['source_id']);require(code==200 and changed in json.dumps(source,ensure_ascii=False) and 'done' in json.dumps(source),'Source does not contain actual update')
        code,_=action(A,'PUT','/tasks/'+tid,{'title':'第二次编辑','status':'doing'});require(code==200,'Second task edit failed')
        code,_=action(A,'PUT','/tasks/'+tid,{'status':'done'});require(code==200,'Mark done failed')
        before=state(A);code,_=action(A,'PUT','/tasks/'+tid,{'title':'','status':'BAD','due_date':'2026-99-99'});require(code in (400,422),'Invalid task update accepted');require(state(A)==before,'Invalid task update wrote')
        action(A,'POST','/import',form={'format':'markdown','content':md})
        require(next(t for t in state(A)['tasks'] if t['id']==tid)['status']=='done','Old import overwrote done task')
    test('task edits provenance and old import',edit_task)
    def corrections():
        for text in ('第一次修正后的事实','第二次修正后的事实'):
            code,_=action(A,'POST','/reviews/review-1/correct',{**review,'completed':text,'decisions':text+'决定'});require(code==200,'Correction failed')
            require(any(r['record_id']=='review-1' and r['completed']==text for r in state(A)['reviews']),'Correction not effective')
        _,report=call('GET',f'/api/projects/{A}/report');require('第二次修正后的事实' in report['report'] and '第一次修正后的事实' not in report['report'],'Report uses stale correction')
        code,_=action(A,'POST','/reviews/review-1/withdraw');require(code==200,'Withdraw failed')
        require(not any(r['record_id']=='review-1' for r in state(A)['reviews']),'Withdrawn review still effective')
        _,report=call('GET',f'/api/projects/{A}/report');require('第二次修正后的事实' not in report['report'],'Withdrawn fact remains in report')
    test('two corrections and withdrawal',corrections)
    def prefs_and_race():
        code,_=action(A,'POST','/preferences',{'preferences':{'report_title':'海藻接力周报','focus':'next_steps'}});require(code==200,'Preferences save failed')
        _,report=call('GET',f'/api/projects/{A}/report');require(report['report'].splitlines()[0]=='# 海藻接力周报','Report title preference not used')
        revision=state(A)['revision']
        def one(n):return action(A,'POST','/preferences',{'preferences':{'report_title':'并发-'+str(n)}},action_id='race-'+str(n),revision=revision)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:outcomes=list(pool.map(one,[1,2]))
        require(sorted(x[0] for x in outcomes)==[200,409],'Concurrent stale writers both succeeded or failed')
    test('preferences affect report and optimistic concurrency',prefs_and_race)
    def pagination():
        for i in range(55):
            code,_=action(B,'POST','/preferences',{'preferences':{'report_title':'分页偏好-'+str(i)}});require(code==200,'Paginated preference write failed at '+str(i))
        require(state(B)['preferences']['report_title']=='分页偏好-54','Latest value lost after more than one SDK page')
    test('55 updates across SDK pages',pagination)
    def source_isolation():
        src=state(A)['tasks'][0]['source_id'];code,_=call('GET',f'/api/projects/{B}/sources/{src}');require(code==404,'Cross-project source exposed')
        code,_=call('GET','/api/projects/scp_00000000000000000000000000/state');require(code==404,'Unknown project should return 404')
    test('source and unknown project isolation',source_isolation)
    def missing_controls():
        before=state(A)
        for endpoint,payload in [('/preferences',{'preferences':{'report_title':'x'}}),('/reviews',review)]:
            code,_=call('POST',f'/api/projects/{A}'+endpoint,json=payload);require(code in (400,422),'Missing action/revision accepted')
        require(state(A)==before,'Missing controls changed state')
    test('required action and revision on all writes',missing_controls)
    before_A=state(A);before_B=state(B)
    process.terminate();process.wait(timeout=30);process=start();second_pid=process.pid
    test('new process reads same effective state',lambda:require(first_pid!=second_pid and state(A)==before_A and state(B)==before_B,'Restart changed persistent state'))
    result={'passed':all(t['passed'] for t in checks),'checks':checks,'first_pid':first_pid,'second_pid':second_pid,'data':str(data),'requests':requests}
    (OUT/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'passed':result['passed'],'checks':checks,'evidence':str(OUT/'results.json')},ensure_ascii=False),flush=True)
    return 0 if result['passed'] else 1

if __name__=='__main__':
    try:code=main()
    finally:
        if process is not None and process.poll() is None:process.terminate();process.wait(timeout=30)
        log.close()
    raise SystemExit(code)
