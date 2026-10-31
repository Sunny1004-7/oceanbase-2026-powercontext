"""PowerContext state adapter. Game branching and model calls live in Dify."""
from __future__ import annotations
import asyncio, contextlib, datetime, hashlib, json, os, pathlib, re, secrets
from contextlib import asynccontextmanager
from typing import Any
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_runtime, CaptureWorkingNote
from powercontext.builtin.scope.models import ScopeDraft

DATA=pathlib.Path(os.environ.get('E05_DATA_DIR','.data')).resolve()
TOKEN=os.environ.get('E05_ADAPTER_TOKEN','')
PAGE_SIZE=20
MAX_EVENTS=500
locks:dict[str,asyncio.Lock]={}

def player_id(value:str)->str:
    if not re.fullmatch(r'[A-Za-z0-9_-]{3,24}',value):
        raise HTTPException(422,'玩家代号需为3–24位英文字母、数字、下划线或短横线')
    return value

def auth(authorization:str|None):
    if not TOKEN or not secrets.compare_digest(authorization or '', 'Bearer '+TOKEN):
        raise HTTPException(401,'状态服务认证失败，请检查本地配置')

def audit(kind:str,**fields):
    DATA.mkdir(parents=True,exist_ok=True)
    with (DATA/'powercontext-audit.jsonl').open('a',encoding='utf-8') as f:
        f.write(json.dumps({'kind':kind,'pid':os.getpid(),'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),**fields},ensure_ascii=False)+'\n')

@asynccontextmanager
async def lifespan(app:FastAPI):
    if not TOKEN:
        raise RuntimeError('E05_ADAPTER_TOKEN is required; use scripts/setup_adapter.py')
    DATA.mkdir(parents=True,exist_ok=True)
    # A single process owns the database: this application documents one worker.
    import fcntl
    lock=(DATA/'adapter.lock').open('a')
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError('This E05 data directory is already in use') from None
    config=BuiltinConfig.model_validate({'database':{'kind':'sqlite','url':'sqlite+aiosqlite:///'+str(DATA/'powercontext.db')},'runtime':{'profile_schedule_enabled':False}})
    async with open_builtin_runtime(config,scheduler_path=DATA/'scheduler.db') as runtime:
        app.state.pc=runtime
        audit('startup',sdk='powercontext==1.4.1',page_size=PAGE_SIZE)
        yield
    fcntl.flock(lock,fcntl.LOCK_UN);lock.close()

app=FastAPI(title='星际失物招领局 · PowerContext 状态服务',lifespan=lifespan)

async def scope(player:str):
    descriptor=await app.state.pc.scopes.create(ScopeDraft(title='失物玩家 '+player,summary='R13 E05 isolated player state',idempotency_key='r13-e05-player-'+player))
    return descriptor.scope_id

async def load(player:str):
    sid=await scope(player)
    cursor=None;events=[];pages=0
    while True:
        page=await app.state.pc.records.for_scope(sid).list_sources(limit=PAGE_SIZE,cursor=cursor)
        pages+=1
        for record in page.items:
            value=json.loads(record.content) if isinstance(record.content,str) else record.content
            if not isinstance(value,dict) or value.get('app')!='lost-found-v1' or value.get('player')!=player:
                raise RuntimeError('Unexpected source in the isolated player scope')
            events.append({**value,'source_id':record.source_id,'position':record.position,'content_digest':record.content_digest})
        cursor=page.next_cursor
        if not cursor: break
        if len(events)>MAX_EVENTS: raise HTTPException(409,'样例已达到500个动作上限，请使用新玩家代号或按说明重置本题数据')
    events.sort(key=lambda e:(e['revision'],e['position']))
    if events and [e['revision'] for e in events]!=list(range(1,len(events)+1)):
        raise RuntimeError('Non-contiguous stored state revisions')
    last=events[-1] if events else None
    audit('read',player=player,scope_id=sid,pages=pages,source_ids=[e['source_id'] for e in events],revision=last['revision'] if last else 0)
    return sid,events,last

@app.get('/health')
async def health():
    return {'status':'ok','sdk':'powercontext==1.4.1','pid':os.getpid(),'page_size':PAGE_SIZE}

@app.get('/state/{player}')
async def read_state(player:str,lookup_action:str|None=None,authorization:str|None=Header(None)):
    auth(authorization);player_id(player)
    async with locks.setdefault(player,asyncio.Lock()):
        sid,events,last=await load(player)
        return {'player':player,'scope_id':sid,'revision':last['revision'] if last else 0,
                'state':last['state'] if last else {'stage':0,'completed':[],'visits':0},
                'last_source':{'source_id':last['source_id'],'at':last['created_at'],'position':last['position']} if last else None,
                'action_count':len(events),
                'actions':{e['action_id']:{'request_hash':e['request_hash'],'reply':e['reply'],'revision':e['revision'],'source_id':e['source_id']} for e in events if lookup_action is None or e['action_id']==lookup_action}}

class Commit(BaseModel):
    model_config=ConfigDict(extra='forbid')
    player:str
    action_id:str=Field(pattern=r'^[A-Za-z0-9_-]{1,32}$')
    expected_revision:int=Field(ge=0,le=MAX_EVENTS)
    request_hash:str=Field(pattern=r'^[a-f0-9]{64}$')
    state:dict[str,Any]
    reply:str=Field(min_length=1,max_length=8000)
    action:str=Field(min_length=1,max_length=30)
    input_text:str=Field(min_length=1,max_length=600)

@app.post('/commit')
async def commit(body:Commit,authorization:str|None=Header(None)):
    auth(authorization);player_id(body.player)
    if len(json.dumps(body.state,ensure_ascii=False))>20000 or body.state.get('stage') not in (0,1,2,3):
        raise HTTPException(422,'状态格式无效；没有保存')
    async with locks.setdefault(body.player,asyncio.Lock()):
        sid,events,last=await load(body.player)
        old=next((e for e in events if e['action_id']==body.action_id),None)
        if old:
            if old['request_hash']!=body.request_hash:
                raise HTTPException(409,'动作编号已用于其他输入；没有重复推进')
            return {'saved':True,'duplicate':True,'revision':old['revision'],'source_id':old['source_id'],'reply':old['reply']}
        revision=last['revision'] if last else 0
        if revision!=body.expected_revision:
            raise HTTPException(409,'玩家状态已更新；请用同一动作编号重试')
        if len(events)>=MAX_EVENTS:
            raise HTTPException(409,'本玩家达到500个动作上限，请使用新代号或重置本题数据')
        source_id='action-'+hashlib.sha256((body.player+'\0'+body.action_id).encode()).hexdigest()[:32]
        event={'app':'lost-found-v1','schema':1,'player':body.player,'revision':revision+1,
               'action_id':body.action_id,'action':body.action,'request_hash':body.request_hash,
               'input_text':body.input_text,'state':body.state,'reply':body.reply,
               'created_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
               'previous_source':last['source_id'] if last else None}
        receipt=await app.state.pc.sources.for_scope(sid).capture_working_note(CaptureWorkingNote(
            source_id=source_id,content=json.dumps(event,ensure_ascii=False),session_id='player-'+body.player,
            note_kind='validation',metadata={'app':'lost-found-v1','player':body.player,'revision':revision+1}))
        audit('write',player=body.player,scope_id=sid,event=event,receipt=receipt.model_dump(mode='json'))
        return {'saved':True,'duplicate':False,'revision':revision+1,'source_id':source_id,'reply':body.reply}

@app.get('/sources/{player}/{source_id}')
async def source_detail(player:str,source_id:str,authorization:str|None=Header(None)):
    auth(authorization);player_id(player)
    sid,events,last=await load(player)
    event=next((e for e in events if e['source_id']==source_id),None)
    if not event: raise HTTPException(404,'该玩家没有此来源')
    return {'scope_id':sid,'source':event}
