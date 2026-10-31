"""Select a new empty E05 data directory; keep every previous data directory."""
import datetime,json,pathlib
from dotenv import dotenv_values
ROOT=pathlib.Path(__file__).resolve().parents[1];config=ROOT/'.env.local'
if not config.exists():raise SystemExit('Run scripts/setup_adapter.py first')
values=dotenv_values(config);old=pathlib.Path(values.get('E05_DATA_DIR') or ROOT/'.data').resolve()
lock_path=old/'adapter.lock'
if lock_path.exists():
    import fcntl
    with lock_path.open('a') as handle:
        try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise SystemExit('Stop this E05 adapter process first, then rerun reset_samples.py')
target=(ROOT/'.sample-data'/datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')).resolve()
if ROOT.resolve() not in target.parents:raise RuntimeError('Target must stay inside this application')
target.mkdir(parents=True,exist_ok=False);values['E05_DATA_DIR']=str(target)
config.write_text(''.join(k+'='+str(v)+'\n' for k,v in values.items() if v is not None),encoding='utf-8')
print(json.dumps({'new_empty_E05_data':str(target),'previous_data_preserved':str(old),'next':'Restart this adapter and open a new Dify conversation.'}))
