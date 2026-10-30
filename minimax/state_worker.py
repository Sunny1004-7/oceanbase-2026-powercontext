"""A real, separately launched PowerContext process: persist and consume task state."""
import argparse,asyncio,json,os,pathlib
from powercontext.builtin.runtime import BuiltinConfig,open_builtin_runtime,CaptureWorkingNote
from powercontext.builtin.scope.models import ScopeDraft
ROOT=pathlib.Path(__file__).resolve().parent
async def run(args):
    data=pathlib.Path(args.data).resolve();data.mkdir(parents=True,exist_ok=True)
    config=BuiltinConfig.model_validate({'database':{'kind':'sqlite','url':'sqlite+aiosqlite:///'+str(data/'powercontext.db')},'runtime':{'profile_schedule_enabled':False}})
    async with open_builtin_runtime(config,scheduler_path=data/'scheduler.db') as runtime:
        descriptor=await runtime.scopes.create(ScopeDraft(title='R12 Reading List',summary='Tag filter development state',idempotency_key='r12-reading-list'))
        scope=descriptor.scope_id
        if args.action=='write':
            state=json.loads(pathlib.Path(args.input).read_text())
            receipt=await runtime.sources.for_scope(scope).capture_working_note(CaptureWorkingNote(source_id='state-'+args.phase,content=json.dumps(state,ensure_ascii=False),session_id='reading-task',note_kind='validation',metadata={'task':'tag-filter','phase':args.phase}))
            result={'pid':os.getpid(),'action':'write','phase':args.phase,'state':state,'receipt':receipt.model_dump(mode='json')}
        else:
            page=await runtime.records.for_scope(scope).list_sources(limit=50,cursor=None)
            records=page.model_dump(mode='json')
            result={'pid':os.getpid(),'action':'read','sources':records}
    pathlib.Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'action':args.action,'pid':os.getpid(),'output':args.output}))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['write','read']);p.add_argument('--data',required=True);p.add_argument('--input');p.add_argument('--phase',default='baseline');p.add_argument('--output',required=True)
    asyncio.run(run(p.parse_args()))
