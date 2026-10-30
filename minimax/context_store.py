"""Project sources live in seekdb; task state lives in the real PowerContext runtime."""
import contextlib,datetime,hashlib,importlib.util,json,pathlib,subprocess,time
import pymysql
ROOT=pathlib.Path(__file__).resolve().parent
def digest(data):return hashlib.sha256(data).hexdigest()

@contextlib.contextmanager
def open_database(data,port=2895):
    data=pathlib.Path(data).resolve();data.mkdir(parents=True,exist_ok=True);base=data/'seekdb';base.mkdir(exist_ok=True)
    binary=pathlib.Path(importlib.util.find_spec('pylibseekdb').origin).parent/'seekdb'
    args=[str(binary),'--nodaemon','--base-dir',str(base),'--port',str(port),'--log-level','WARN','--parameter','memory_limit=1G','--parameter','datafile_size=512M','--parameter','datafile_maxsize=2G','--parameter','log_disk_size=2G']
    log=(data/'seekdb.log').open('a');process=subprocess.Popen(args,stdout=log,stderr=subprocess.STDOUT);connection=None
    try:
        deadline=time.monotonic()+90
        while True:
            if process.poll() is not None:raise RuntimeError('seekdb startup failed; see '+str(data/'seekdb.log'))
            try:connection=pymysql.connect(host='127.0.0.1',port=port,user='root',password='',charset='utf8mb4',autocommit=True,cursorclass=pymysql.cursors.DictCursor);break
            except pymysql.Error:
                if time.monotonic()>deadline:raise RuntimeError('seekdb readiness timeout')
                time.sleep(.25)
        with connection.cursor() as c:c.execute('CREATE DATABASE IF NOT EXISTS r12_minimax');c.execute('USE r12_minimax')
        yield ContextStore(connection,data/'queries.jsonl')
    finally:
        if connection:connection.close()
        if process.poll() is None:process.terminate()
        try:process.wait(timeout=30)
        except subprocess.TimeoutExpired:process.kill();process.wait()
        log.close()

class ContextStore:
    def __init__(self,connection,audit):self.connection=connection;self.audit=audit
    def execute(self,sql,params=None):
        start=time.perf_counter()
        with self.connection.cursor() as cursor:
            cursor.execute(sql,params);rows=cursor.fetchall() if cursor.description else []
        with self.audit.open('a') as f:f.write(json.dumps({'timestamp_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'sql':sql,'params':params,'rows':rows,'duration_ms':(time.perf_counter()-start)*1000},ensure_ascii=False,default=str)+'\n')
        return rows
    def initialize(self):
        self.execute('CREATE TABLE IF NOT EXISTS project_sources (source_id VARCHAR(191) PRIMARY KEY, task_key VARCHAR(64), path VARCHAR(255), kind VARCHAR(32), symbol VARCHAR(128), source_version VARCHAR(64), revision CHAR(64), is_current BOOL, content LONGTEXT)')
    def put(self,path,kind,symbol,version,content):
        revision=digest(content.encode());identity=path+'::'+symbol+'::'+version
        self.execute('INSERT INTO project_sources(source_id,task_key,path,kind,symbol,source_version,revision,is_current,content) VALUES(%s,%s,%s,%s,%s,%s,%s,1,%s) ON DUPLICATE KEY UPDATE content=VALUES(content),revision=VALUES(revision),is_current=1',(identity,'tag-filter',path,kind,symbol,version,revision,content))
    def import_spec(self,version):
        path='specs/'+version+'.json';doc=json.loads((ROOT/path).read_text())
        self.connection.begin()
        try:
            self.execute("UPDATE project_sources SET is_current=0 WHERE task_key=%s AND kind='spec'",('tag-filter',))
            for item in doc['entries']:self.put(path,'spec',item['id'],version,item['text'])
            self.connection.commit()
        except BaseException:self.connection.rollback();raise
    def import_project(self):
        for path,kind,symbol in [('example/before/reading_list.py','code','select_books,main'),('tests/test_reading_list.py','test','RegressionTests,TargetTests'),('example/books.json','data','BOOKS')]:self.put(path,kind,symbol,'frozen', (ROOT/path).read_text(encoding='utf-8'))
    def retrieve(self,task_key='tag-filter'):
        rows=self.execute("SELECT source_id,path,kind,symbol,source_version,revision,content FROM project_sources WHERE task_key=%s AND is_current=1 AND kind IN ('spec','code','test','data') ORDER BY kind,path,symbol",(task_key,))
        if not rows:raise RuntimeError('No current context for task '+task_key)
        return {'task_key':task_key,'selection':'seekdb structured current-source query','sources':rows}
