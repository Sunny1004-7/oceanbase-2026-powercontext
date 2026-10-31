"""Create ignored local adapter config, without revealing its token."""
import json, pathlib, secrets, os
root=pathlib.Path(__file__).resolve().parents[1]
config=root/'.env.local'
if not config.exists():
    config.write_text('E05_DATA_DIR='+str(root/'.data')+'\nE05_ADAPTER_TOKEN='+secrets.token_hex(32)+'\nMINIMAX_API_KEY=\nMINIMAX_BASE_URL=https://api.minimax.cn/v1\nMINIMAX_MODEL=MiniMax-M2.5\n',encoding='utf-8')
    config.chmod(0o600)
print(json.dumps({'config':str(config),'created_or_present':True,'secret_printed':False}))
