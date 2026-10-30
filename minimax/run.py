"""Reproduce the causal development sample; replay never claims a new model call."""
import argparse,difflib,hashlib,json,os,pathlib,re,shutil,subprocess,sys,time
from context_store import ROOT,open_database,digest
from minimax_client import invoke

def write_json(path,value):path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def command(args,output,env=None):
    start=time.perf_counter();p=subprocess.run(args,cwd=ROOT,env={**os.environ,**(env or {})},capture_output=True,text=True,encoding='utf-8')
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text('$ '+' '.join(map(str,args))+'\n'+p.stdout+p.stderr+f'\nexit_code={p.returncode}\n',encoding='utf-8')
    return {'command':list(map(str,args)),'exit_code':p.returncode,'log':str(output.relative_to(ROOT)) if output.is_relative_to(ROOT) else str(output),'duration_ms':(time.perf_counter()-start)*1000,'stdout':p.stdout,'stderr':p.stderr}
def test(snapshot,group,out):return command([sys.executable,'-m','unittest','discover','-s','tests','-p','test_reading_list.py','-v'] if group=='all' else [sys.executable,'-m','unittest','test_reading_list.'+group,'-v'],out,{'READING_MODULE':str(snapshot),'PYTHONPATH':str(ROOT/'tests')})
def state(action,data,out,input=None,phase='baseline'):
    args=[sys.executable,str(ROOT/'state_worker.py'),action,'--data',str(data),'--output',str(out),'--phase',phase]
    if input:args+=['--input',str(input)]
    p=command(args,out.with_suffix('.log'))
    if p['exit_code']:raise RuntimeError('PowerContext subprocess failed: '+p['log'])
    return json.loads(out.read_text())
def consume(read):
    items=read['sources']['items']
    if not items:return {'resume':False,'next_action':'run_baseline_and_import'}
    newest=max(items,key=lambda r:r['position']);value=newest['content']
    # PowerContext SourceRecord content is the actual persisted ContentSource capture.
    if isinstance(value,dict):value=value.get('content',value)
    if isinstance(value,str):value=json.loads(value)
    return {'resume':True,'source_id':newest['source_id'],'source_position':newest['position'],'state':value,'next_action':value['next_action']}

def workflow(args):
    data=pathlib.Path(args.data).resolve();data.mkdir(parents=True,exist_ok=True)
    out=ROOT/'evidence'/args.run_id
    if out.exists():raise RuntimeError('Run id already exists; choose a new id to preserve evidence')
    out.mkdir(parents=True)
    before=ROOT/'example/before/reading_list.py';frozen=digest((ROOT/'tests/test_reading_list.py').read_bytes())
    write_json(out/'frozen.json',{'tests_sha256':frozen,'before_sha256':digest(before.read_bytes()),'spec_v1_sha256':digest((ROOT/'specs/v1.json').read_bytes()),'spec_v2_sha256':digest((ROOT/'specs/v2.json').read_bytes()),'data_sha256':digest((ROOT/'example/books.json').read_bytes())})
    regression=test(before,'RegressionTests',out/'before-regression.log');baseline=test(before,'all',out/'before-all.log')
    if regression['exit_code']!=0 or baseline['exit_code']!=1 or 'FAILED (failures=10)' not in baseline['stderr']:raise RuntimeError('Baseline must fail only expected business assertions')
    initial=state('read',data/'powercontext',out/'state-empty.json');assert not initial['sources']['items'],'Start from an empty PowerContext state directory'
    with open_database(data/'project',args.port) as db:
        db.initialize();db.import_project();db.import_spec('v1');v1=db.retrieve();write_json(out/'context-v1.json',v1)
        db.import_spec('v2');v2=db.retrieve();write_json(out/'context-v2.json',v2)
        assert all(s['source_version']=='v2' for s in v2['sources'] if s['kind']=='spec')
        assert any(s['symbol']=='TAG-03' for s in v2['sources'])
        history=db.execute("SELECT source_id,source_version,is_current,revision FROM project_sources WHERE kind='spec' ORDER BY source_version,source_id")
        write_json(out/'spec-history.json',history)
        saved={'task':'tag-filter','completed':['baseline_verified','sources_imported','spec_updated_v2'],'tests_sha256':frozen,'failed_behavior':'tag filtering absent','next_action':'modify_with_current_v2_context'}
        write_json(out/'baseline-state-input.json',saved)
        written=state('write',data/'powercontext',out/'state-write.json',out/'baseline-state-input.json')
        loaded=state('read',data/'powercontext',out/'state-read.json');assert written['pid']!=loaded['pid']
        decision=consume(loaded);write_json(out/'resume-decision.json',decision)
        assert decision['next_action']=='modify_with_current_v2_context'
        assert decision['state']['tests_sha256']==frozen
        # A resumed state changes processing: no repeated imports/baseline; retrieve current DB context and enter modification.
        context=db.retrieve();context['resumed_task_state']=decision
        context['resume_effect']={'skipped_steps':['run_baseline_and_import'],'selected_next_step':decision['next_action']}
        write_json(out/'context-used.json',context)
        candidate_dir=out/'candidate';candidate_dir.mkdir()
        if args.mode=='develop':
            messages=[{'role':'system','content':'你是开发助手。基于提供的真实工程上下文完成小范围代码修改。不要输出思维链。只输出一个 JSON 对象，格式为 {"reading_list.py": "修改后完整Python源码"}。仅修改业务文件，禁止更改测试、规格或数据。保持未知关键字兼容。'}, {'role':'user','content':'任务：为 Python 阅读清单新增按标签筛选，与 status 取交集。必须满足有效 v2 规格以及全部冻结测试。提供的上下文来自 seekdb 查询；PowerContext 恢复的进展已经决定进入修改阶段。\n'+json.dumps(context,ensure_ascii=False)}]
            for round_number in range(1,4):
                round_dir=out/f'minimax-round-{round_number}'
                reply=invoke(messages,round_dir)
                content=reply['content'].strip();content=re.sub(r'^```(?:json)?\s*|\s*```$','',content)
                proposed=json.loads(content)['reading_list.py']
                if not isinstance(proposed,str):raise RuntimeError('Model code is not text')
                candidate=candidate_dir/'reading_list.py';candidate.write_text(proposed,encoding='utf-8')
                write_json(round_dir/'application.json',{'source':'MiniMax ordinary output','target':str(candidate.relative_to(ROOT)),'before_sha256':digest(before.read_bytes()),'candidate_sha256':digest(candidate.read_bytes())})
                result=test(candidate,'all',round_dir/'tests.log')
                if result['exit_code']==0:break
                messages.append({'role':'assistant','content':reply['content']});messages.append({'role':'user','content':'以下是真实失败测试，请仅修复业务文件，仍只返回同样JSON。\n'+result['stdout']+result['stderr']})
            else:raise RuntimeError('Three finite MiniMax rounds failed; evidence preserved')
            after=ROOT/'example/after/reading_list.py';after.parent.mkdir(exist_ok=True);shutil.copyfile(candidate,after)
        else:
            after=ROOT/'example/after/reading_list.py'
            if not after.exists():raise RuntimeError('No delivered MiniMax patch; develop first')
            candidate=candidate_dir/'reading_list.py';shutil.copyfile(after,candidate)
            write_json(out/'replay.json',{'mode':'offline replay','new_model_call':False,'delivered_patch_sha256':digest(after.read_bytes())})
        final=test(candidate,'all',out/'after-all.log')
        if final['exit_code']!=0:raise RuntimeError('Candidate tests did not pass')
        assert frozen==digest((ROOT/'tests/test_reading_list.py').read_bytes())
        patch=''.join(difflib.unified_diff(before.read_text().splitlines(True),candidate.read_text().splitlines(True),fromfile='before/reading_list.py',tofile='after/reading_list.py'))
        (out/'business.patch').write_text(patch,encoding='utf-8')
        state_input={'task':'tag-filter','completed':['baseline_verified','spec_v2_imported','patch_applied','all_21_tests_passed'],'tests_sha256':frozen,'candidate_sha256':digest(candidate.read_bytes()),'next_action':'deliver_verified_patch'}
        write_json(out/'final-state-input.json',state_input)
        state('write',data/'powercontext',out/'state-final-write.json',out/'final-state-input.json',phase='verified')
        read=state('read',data/'powercontext',out/'state-final-read.json');next_decision=consume(read);write_json(out/'final-resume-decision.json',next_decision);assert next_decision['next_action']=='deliver_verified_patch'
        write_json(out/'database-version.json',db.execute('SELECT VERSION() AS version'))
    shutil.copyfile(data/'project/queries.jsonl',out/'database-queries.jsonl')
    summary={'mode':args.mode,'new_model_call':args.mode=='develop','tests_sha256':frozen,'before_sha256':digest(before.read_bytes()),'after_sha256':digest(candidate.read_bytes()),'baseline_regression_exit':regression['exit_code'],'baseline_all_exit':baseline['exit_code'],'baseline_failures':10,'after_all_exit':final['exit_code'],'tests_total':21,'PowerContext':'1.4.1: sources.capture_working_note + records.list_sources; separate write/read processes','state_reuse_effect':decision['next_action'],'final_next_action':next_decision['next_action'],'all_six_local_acceptance':True}
    write_json(out/'summary.json',summary);print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['develop','replay']);p.add_argument('--data',required=True);p.add_argument('--run-id',required=True);p.add_argument('--port',type=int,default=2895);workflow(p.parse_args())
