"""Install the signed model plugin, bind local services, import and publish YAML."""
import argparse,hashlib,json,pathlib,sys,time,yaml
from dotenv import dotenv_values
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.console_api import Console,private,save_private,evidence
PROVIDER='langgenius/openai_api_compatible/openai_api_compatible'
IDENTIFIER='langgenius/openai_api_compatible:0.0.73@4cf123ffaa4556f079bed6c47f3a462cc825504b859b183239e74eb19136a4b5'

def ensure_plugin(c):
    installed=c.json('GET','/workspaces/current/plugin/list',params={'page':1,'page_size':100})
    if any(p.get('plugin_unique_identifier')==IDENTIFIER for p in installed.get('plugins',[])):
        evidence('plugin-ready',{'identifier':IDENTIFIER,'already_installed':True});return
    pkg=ROOT/'vendor'/'openai_api_compatible-0.0.73.difypkg'
    lock=json.loads((ROOT/'vendor'/'plugin-lock.json').read_text())
    if hashlib.sha256(pkg.read_bytes()).hexdigest()!=lock['sha256']:raise RuntimeError('Plugin package checksum differs')
    with pkg.open('rb') as f:uploaded=c.json('POST','/workspaces/current/plugin/upload/pkg',files={'pkg':(pkg.name,f,'application/octet-stream')})
    if uploaded['unique_identifier']!=IDENTIFIER:raise RuntimeError('Unexpected signed plugin identity')
    evidence('plugin-upload',{'identifier':uploaded['unique_identifier'],'verification':uploaded.get('verification')})
    installed=c.json('POST','/workspaces/current/plugin/install/pkg',json={'plugin_unique_identifiers':[IDENTIFIER]})
    if installed.get('all_installed'):return
    task=installed['task_id']
    for i in range(150):
        result=c.json('GET','/workspaces/current/plugin/tasks/'+task)
        data=result.get('task') or result
        evidence('plugin-install-task',result)
        if data.get('status')=='success':return
        if data.get('status') in ('failed','error'):raise RuntimeError('Plugin installation failed; inspect evidence/runtime/plugin-install-task.json')
        time.sleep(2)
    raise RuntimeError('Plugin installation pending; inspect task before retrying')

def ensure_model(c,env,reuse=False):
    if reuse:
        result=c.json('GET','/workspaces/current/model-providers/'+PROVIDER+'/models')
        evidence('model-binding',{'provider':PROVIDER,'model':'MiniMax-M2.5','existing_workspace_binding':True,'models':result})
        if 'MiniMax-M2.5' not in json.dumps(result):raise RuntimeError('MiniMax-M2.5 is not configured in this workspace')
        return
    cfg=private()
    if cfg.get('model_configured'):return
    if not env.get('MINIMAX_API_KEY'):raise RuntimeError('Fill MINIMAX_API_KEY in ignored .env.local, then rerun')
    if env.get('MINIMAX_BASE_URL','https://api.minimax.cn/v1')!='https://api.minimax.cn/v1':raise RuntimeError('This tested deployment uses the official mainland MiniMax endpoint')
    credentials={'api_key':env['MINIMAX_API_KEY'],'endpoint_url':'https://api.minimax.cn/v1','endpoint_model_name':'MiniMax-M2.5',
                 'display_name':'MiniMax-M2.5 大陆','mode':'chat','context_size':'16384','max_tokens_to_sample':'1800',
                 'vision_support':'no_support','function_calling_type':'no_call','agent_thought_support':'not_supported',
                 'compatibility_mode':'strict','api_type':'chat_completions','token_param_name':'max_tokens','stream_include_usage':'enabled','user_identity_support':'no_support'}
    result=c.json('POST','/workspaces/current/model-providers/'+PROVIDER+'/models/credentials',json={'model':'MiniMax-M2.5','model_type':'llm','name':'招领局大陆模型','credentials':credentials})
    cfg['model_configured']=True;save_private(cfg)
    evidence('model-bound',{'provider':PROVIDER,'model':'MiniMax-M2.5','endpoint':'https://api.minimax.cn/v1','result':result})

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--dsl',type=pathlib.Path,default=ROOT/'dsl'/'星际失物招领局.yml')
    parser.add_argument('--name',default='星际失物招领局');parser.add_argument('--adapter-url',default='http://pc-host.internal:4335')
    parser.add_argument('--reuse-model',action='store_true');args=parser.parse_args()
    env=dotenv_values(ROOT/'.env.local');token=env.get('E05_ADAPTER_TOKEN')
    if not token or token=='GENERATE_WITH_SETUP_ADAPTER':raise RuntimeError('Run scripts/setup_adapter.py first')
    c=Console(bootstrap=True);ensure_plugin(c);ensure_model(c,env,args.reuse_model)
    cfg=private()
    if not cfg.get('app_id'):
        source=args.dsl.read_text(encoding='utf-8')
        result=c.json('POST','/apps/imports',json={'mode':'yaml-content','yaml_content':source,'name':args.name})
        evidence('yaml-import',{'result':result,'file':args.dsl.name,'sha256':hashlib.sha256(source.encode()).hexdigest()})
        if result.get('status')=='pending':result=c.json('POST','/apps/imports/'+result['id']+'/confirm',json={})
        if result.get('status') not in ('completed','completed-with-warnings') or not result.get('app_id'):raise RuntimeError('DSL import did not complete')
        cfg['app_id']=result['app_id'];save_private(cfg)
    appid=cfg['app_id'];draft=c.json('GET','/apps/'+appid+'/workflows/draft')
    envs=draft['environment_variables']
    for value in envs:
        if value['name']=='PC_TOKEN':value['value']=token
        if value['name']=='PC_BASE_URL':value['value']=args.adapter_url.rstrip('/')
    saved=c.json('POST','/apps/'+appid+'/workflows/draft',json={'graph':draft['graph'],'features':draft['features'],'hash':draft['hash'],
                  'environment_variable_patch':{'environment_variables':envs,'deleted_environment_variable_ids':[]},'conversation_variables':draft.get('conversation_variables',[])})
    published=c.json('POST','/apps/'+appid+'/workflows/publish',json={'marked_name':'星际失物招领局','marked_comment':'三幕委托与跨会话档案'})
    if not cfg.get('app_id_api_key'):
        key=c.json('POST','/apps/'+appid+'/api-keys',json={});cfg['app_id_api_key']=key['token'];save_private(cfg)
    details=c.json('GET','/apps/'+appid);site_code=details.get('site',{}).get('code')
    evidence('bound-and-published',{'app_id':appid,'adapter_url':args.adapter_url,'token_bound':True,'draft_hash':saved.get('hash'),'published':published,
                                    'site_code':site_code,'model_provider':PROVIDER,'model':'MiniMax-M2.5'})
    print(json.dumps({'app_id':appid,'local_app_url':(env.get('DIFY_CONSOLE_URL') or 'http://127.0.0.1:8383').rstrip('/')+'/chat/'+str(site_code),'credentials_saved_locally':True},ensure_ascii=False))

if __name__=='__main__':main()
