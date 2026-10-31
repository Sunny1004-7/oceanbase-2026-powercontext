import pathlib,os,argparse
from dotenv import load_dotenv
import uvicorn
p=argparse.ArgumentParser();p.add_argument('--data');p.add_argument('--host',default='0.0.0.0');p.add_argument('--port',type=int,default=4335)
a=p.parse_args();root=pathlib.Path(__file__).resolve().parents[1]
load_dotenv(root/'.env.local')
if a.data: os.environ['E05_DATA_DIR']=str(pathlib.Path(a.data).resolve())
os.chdir(root)
uvicorn.run('adapter:app',host=a.host,port=a.port,workers=1,app_dir=str(root))
