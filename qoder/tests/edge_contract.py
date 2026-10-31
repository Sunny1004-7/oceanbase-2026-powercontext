"""Codex independent checks for provenance, field errors, and data ownership.

All checks use a real PowerContext SQLite runtime behind an HTTP process.
"""
import json,os,pathlib,socket,subprocess,sys,tempfile,time,uuid
import httpx
ROOT=pathlib.Path(__file__).resolve().parents[1]
class Harness:
    os=os
    def __init__(self):
        self.OUT=ROOT/'evidence/edge';self.OUT.mkdir(exist_ok=True,parents=True)
        self.log=(self.OUT/'server.log').open('w')
        self.data=pathlib.Path(tempfile.mkdtemp(prefix='group-relay-edge-'))
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        self.base='http://127.0.0.1:'+str(port);self.port=port
        self.checks=[];self.requests=[];self.process=None
    def start(self):
        p=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(self.port),'--app-dir',str(ROOT)],env={**os.environ,'DATA_DIR':str(self.data)},stdout=self.log,stderr=subprocess.STDOUT)
        for _ in range(150):
            try:
                with httpx.Client(trust_env=False,timeout=1) as c:r=c.get(self.base+'/api/projects')
                if r.status_code==200:return p
            except httpx.HTTPError:pass
            if p.poll() is not None:raise RuntimeError('HTTP process exited')
            time.sleep(.1)
        p.terminate();p.wait(timeout=20);raise RuntimeError('Startup timed out')
    def call(self,method,path,**kw):
        with httpx.Client(trust_env=False,timeout=30) as c:r=c.request(method,self.base+path,**kw)
        try:value=r.json()
        except ValueError:value={'body':r.text[:500]}
        self.requests.append({'method':method,'path':path,'input':kw,'status':r.status_code,'response':value})
        return r.status_code,value
    def require(self,ok,message):
        if not ok:raise AssertionError(message)
    def state(self,scope):
        code,value=self.call('GET',f'/api/projects/{scope}/state');self.require(code==200,'State read failed');return value
    def action(self,scope,method,path,payload=None,form=None,action_id=None):
        fields={'action_id':action_id or 'edge-'+uuid.uuid4().hex,'expected_revision':self.state(scope)['revision']}
        if form is not None:return self.call(method,f'/api/projects/{scope}'+path,data={**form,**fields})
        return self.call(method,f'/api/projects/{scope}'+path,json={**(payload or {}),**fields})
    def test(self,name,fn):
        try:fn();self.checks.append({'test':name,'passed':True})
        except Exception as e:self.checks.append({'test':name,'passed':False,'error':str(e)})
t=Harness()

def main():
    t.process=t.start()
    _,a=t.call('POST','/api/projects',json={'name':'来源边界 A'})
    _,b=t.call('POST','/api/projects',json={'name':'来源边界 B'})
    A=a['scope_id'];B=b['scope_id']
    def same_action_two_projects():
        for scope,label in [(A,'项目甲'),(B,'项目乙')]:
            code,_=t.action(scope,'POST','/tasks',{'task_id':'same-task','title':label,'status':'todo'},action_id='same-action-id')
            t.require(code==200,'Same client action ID failed across separate projects')
        t.require(t.state(A)['tasks'][0]['title']=='项目甲' and t.state(B)['tasks'][0]['title']=='项目乙','Same action crossed project state')
    t.test('same action ID remains isolated across projects',same_action_two_projects)
    def report_sources():
        review={'record_id':'trace-review','date':'2026-10-31','completed':'真实配制记录','blockers':'等待标定','decisions':'固定盐度','next_steps':'补齐重复组'}
        code,_=t.action(A,'POST','/reviews',review);t.require(code==200,'Review save failed')
        state=t.state(A);_,report=t.call('GET',f'/api/projects/{A}/report')
        for item in state['tasks']+state['reviews']:
            code,source=t.call('GET',f'/api/projects/{A}/sources/'+item['source_id'])
            t.require(code==200,'Effective source not readable')
            t.require(source['created_at'] in report['report'],'Report omits actual source timestamp')
            t.require(f'/api/projects/{A}/sources/'+item['source_id'] in report['report'],'Report omits original source entry link')
    t.test('report includes effective source ID time and raw entry',report_sources)
    def invalid_field_types():
        before=t.state(B)
        bad=[('/tasks',{'task_id':'bad-number','title':123,'status':'todo'}),
             ('/tasks',{'task_id':'bad-date','title':'类型错误','status':'todo','due_date':[]}),
             ('/reviews',{'record_id':'bad-review','date':'2026-10-31','completed':['不应保存数组']})]
        for route,payload in bad:
            code,_=t.action(B,'POST',route,payload)
            t.require(code in (400,422),'Malformed field should produce 400/422, got '+str(code))
        t.require(t.state(B)==before,'Malformed fields changed persistent state')
    t.test('invalid field types are controlled and atomic',invalid_field_types)
    def invalid_encoding():
        before=t.state(B);fields={'format':'csv','action_id':'bad-encoding','expected_revision':str(before['revision'])}
        code,_=t.call('POST',f'/api/projects/{B}/import',data=fields,files={'file':('bad.csv',b'\xff\xfe\x80','text/csv')})
        t.require(code in (400,422),'Non-UTF8 upload returned '+str(code))
        t.require(t.state(B)==before,'Bad encoding changed state')
    t.test('unsupported file encoding has a clear client error',invalid_encoding)
    def complete_import_source():
        original='长原文核验'+('真实字段内容'*300)
        content=f'# 固定样例\n记录编号: long-source\n日期: 2026-10-31\n已完成: {original}\n困难: 无\n决定: 保留原始输入\n下一步: 校核\n\n|任务ID|标题|状态|负责人|截止日期|\n|---|---|---|---|---|\n|long-task|完整原文|todo|小禾|2026-11-02|\n'
        code,receipt=t.action(B,'POST','/import',form={'format':'markdown','content':content});t.require(code==200,'Long valid import failed')
        _,source=t.call('GET',f'/api/projects/{B}/sources/'+receipt['source_id'])
        t.require(source['content'].get('raw_content')==content,'Original supported input was silently truncated')
    t.test('source preserves the full accepted import',complete_import_source)
    def exclusive_data_lock():
        before=t.state(A)
        second=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port','0','--app-dir',str(ROOT)],env={**t.os.environ,'DATA_DIR':str(t.data)},stdout=t.log,stderr=subprocess.STDOUT)
        try:
            code=second.wait(timeout=20)
            t.require(code!=0,'Second writer started on the same data directory')
            t.require(t.process.poll() is None and t.state(A)==before,'Second process disturbed the original service')
        finally:
            if second.poll() is None:second.terminate();second.wait(timeout=20)
    t.test('a second process cannot own the same data directory',exclusive_data_lock)
    result={'passed':all(x['passed'] for x in t.checks),'checks':t.checks,'data':str(t.data),'requests':t.requests}
    (t.OUT/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=lambda x:{'bytes_hex':x.hex()} if isinstance(x,bytes) else str(x))+'\n',encoding='utf8')
    print(json.dumps({'passed':result['passed'],'checks':t.checks},ensure_ascii=False))
    return 0 if result['passed'] else 1

if __name__=='__main__':
    try:code=main()
    finally:
        if t.process is not None and t.process.poll() is None:t.process.terminate();t.process.wait(timeout=30)
        t.log.close()
    raise SystemExit(code)
