"""Deploy the pinned official Dify subset in a Linux data directory."""
import argparse,hashlib,json,pathlib,secrets,shutil,subprocess,tarfile,tempfile
ROOT=pathlib.Path(__file__).resolve().parents[1]
LOCK=json.loads((ROOT/'deploy'/'images.lock.json').read_text())

def run(cmd,**kwargs): return subprocess.run(cmd,check=True,**kwargs)

def main():
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=pathlib.Path,default=pathlib.Path.home()/'.local/share/stellar-lost-found/dify');p.add_argument('--port',type=int,default=8383);a=p.parse_args()
    runtime=a.runtime.resolve();runtime.mkdir(parents=True,exist_ok=True)
    if not (runtime/'docker-compose.yaml').exists():
        archive=runtime/'dify-1.18.0.tar.gz'
        if not archive.exists():run(['curl','-fL','--retry','3',LOCK['source_url'],'-o',str(archive)])
        digest=hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest!=LOCK['archive_sha256']:raise RuntimeError('The fixed Dify archive checksum differs; no files extracted')
        with tempfile.TemporaryDirectory(dir=runtime) as temp:
            with tarfile.open(archive) as tar:
                members=[m for m in tar.getmembers() if m.name.startswith('dify-1.18.0/docker/')]
                tar.extractall(temp,members=members,filter='data')
            shutil.copytree(pathlib.Path(temp)/'dify-1.18.0'/'docker',runtime,dirs_exist_ok=True)
    envfile=runtime/'.env'
    if not envfile.exists():
        values={line.split('=',1)[0]:line.split('=',1)[1] for line in (runtime/'.env.example').read_text().splitlines() if line and not line.startswith('#') and '=' in line}
        values.update({'COMPOSE_PROJECT_NAME':'r13-dify','COMPOSE_PROFILES':'postgresql','EXPOSE_NGINX_PORT':str(a.port),
            'EXPOSE_PLUGIN_DEBUGGING_PORT':'5333','ENABLE_COLLABORATION_MODE':'false','NEXT_PUBLIC_ENABLE_AGENT_V2':'false',
            'NEXT_PUBLIC_SOCKET_URL':'ws://localhost:'+str(a.port),'NGINX_SOCKET_IO_UPSTREAM':'api:5001','NEXT_TELEMETRY_DISABLED':'1',
            'CELERY_WORKER_AMOUNT':'1','SERVER_WORKER_AMOUNT':'1','SQLALCHEMY_POOL_SIZE':'5','SQLALCHEMY_MAX_OVERFLOW':'5',
            'POSTGRES_MAX_CONNECTIONS':'100','POSTGRES_EFFECTIVE_CACHE_SIZE':'512MB','PLUGIN_PYTHON_ENV_INIT_TIMEOUT':'600',
            'PYTHON_ENV_INIT_TIMEOUT':'600','SSRF_PROXY_ALLOW_PRIVATE_IPS':'172.30.13.1/32','CHECK_UPDATE_URL':'',
            'PIP_MIRROR_URL':'https://pypi.org/simple','SECRET_KEY':secrets.token_urlsafe(42),
            **{name:secrets.token_hex(28) for name in ('DB_PASSWORD','REDIS_PASSWORD','PLUGIN_DAEMON_KEY','PLUGIN_DIFY_INNER_API_KEY','SANDBOX_API_KEY','DIFY_AGENT_LOCAL_SANDBOX_AUTH_TOKEN','DIFY_AGENT_API_TOKEN')}})
        values['CELERY_BROKER_URL']='redis://:'+values['REDIS_PASSWORD']+'@redis:6379/1'
        values['CODE_EXECUTION_API_KEY']=values['SANDBOX_API_KEY']
        envfile.write_text(''.join(k+'='+v+'\n' for k,v in values.items()));envfile.chmod(0o600)
    override=(ROOT/'deploy'/'compose.override.yaml').read_text().replace('127.0.0.1:8383:80','127.0.0.1:'+str(a.port)+':80')
    (runtime/'r13.override.yaml').write_text(override)
    shutil.copy2(ROOT/'deploy'/'images.lock.yaml',runtime/'r13.images.yaml')
    cmd=['docker','compose','-f','docker-compose.yaml','-f','r13.override.yaml','-f','r13.images.yaml']
    services=list(LOCK['services'])
    run(cmd+['pull']+services,cwd=runtime)
    run(cmd+['up','-d','--pull','never']+services,cwd=runtime)
    run(cmd+['ps'],cwd=runtime)
    print(json.dumps({'local_url':'http://localhost:'+str(a.port),'runtime':str(runtime),'source_commit':LOCK['dify_commit'],'pinned_services':len(services),'secrets_printed':False}))

if __name__=='__main__':main()
