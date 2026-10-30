"""Own an official prebuilt seekdb server and the Node app; no source build."""
import importlib.util,json,os,pathlib,signal,subprocess,sys,time
import pymysql
ROOT=pathlib.Path(__file__).resolve().parents[1]
DATA=pathlib.Path(os.environ.get('APP_DATA_DIR',str(ROOT/'.data'))).resolve();DATA.mkdir(parents=True,exist_ok=True)
binary=pathlib.Path(importlib.util.find_spec('pylibseekdb').origin).parent/'seekdb'
port=int(os.environ.get('SEEKDB_PORT','2894'))
args=[str(binary),'--nodaemon','--base-dir',str(DATA/'database'),'--port',str(port),'--log-level','INFO','--parameter','memory_limit=1G','--parameter','datafile_size=512M','--parameter','datafile_maxsize=2G','--parameter','log_disk_size=2G']
(DATA/'database').mkdir(exist_ok=True)
log=(DATA/'seekdb-stdout.log').open('a');db=subprocess.Popen(args,stdout=log,stderr=subprocess.STDOUT)
app=None;stopping=False
def stop(*_):
    global stopping
    stopping=True
    if app and app.poll() is None:app.terminate()
    if db.poll() is None:db.terminate()
signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
try:
    deadline=time.monotonic()+90
    while True:
        if db.poll() is not None:raise RuntimeError('seekdb exited; read '+str(DATA/'seekdb-stdout.log'))
        try:
            connection=pymysql.connect(host='127.0.0.1',port=port,user='root',password='',connect_timeout=2)
            with connection.cursor() as c:c.execute('CREATE DATABASE IF NOT EXISTS test');c.execute('SELECT VERSION()');version=c.fetchone()[0]
            connection.close();break
        except pymysql.Error:
            if time.monotonic()>deadline:raise RuntimeError('seekdb readiness timed out')
            time.sleep(.25)
    print(json.dumps({'database_ready':True,'version':version,'binary':str(binary),'port':port,'pid':db.pid}),flush=True)
    app=subprocess.Popen(['node','server.mjs'],cwd=ROOT,env={**os.environ,'APP_DATA_DIR':str(DATA),'SEEKDB_PORT':str(port)})
    code=app.wait()
    sys.exit(0 if stopping else code)
finally:
    for process in [app,db]:
        if process:
            if process.poll() is None:process.terminate()
            try:process.wait(timeout=30)
            except subprocess.TimeoutExpired:process.kill();process.wait()
