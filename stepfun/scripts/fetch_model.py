"""Fetch an immutable public model revision; verify actual bytes, never run inference."""
import hashlib, json, pathlib, urllib.request, concurrent.futures, time
ROOT=pathlib.Path(__file__).resolve().parents[1]
MODEL='Xenova/bge-small-zh-v1.5'
MANIFEST=ROOT/'model-manifest.json'
locked=json.loads(MANIFEST.read_text()) if MANIFEST.exists() else None
api=None if locked else json.load(urllib.request.urlopen('https://huggingface.co/api/models/'+MODEL,timeout=45))
revision=locked['revision'] if locked else api['sha']
names=['config.json','tokenizer.json','tokenizer_config.json','special_tokens_map.json','vocab.txt','onnx/model.onnx','onnx/model_fp16.onnx','onnx/model_quantized.onnx']
dest=ROOT/'.models'/MODEL
def download(name):
    out=dest/name; out.parent.mkdir(parents=True,exist_ok=True)
    url=f'https://huggingface.co/{MODEL}/resolve/{revision}/{name}'
    if not out.exists():
        for attempt in range(3):
            try:
                tmp=out.with_suffix(out.suffix+'.part')
                with urllib.request.urlopen(url,timeout=90) as response, tmp.open('wb') as f:
                    while chunk:=response.read(1024*1024): f.write(chunk)
                tmp.replace(out); break
            except Exception:
                if attempt==2: raise
                time.sleep(2)
    sha=hashlib.sha256(out.read_bytes()).hexdigest()
    if locked:
        expected=next(f for f in locked['files'] if f['file']==name)
        if sha!=expected['sha256'] or out.stat().st_size!=expected['bytes']:raise ValueError('Model file hash mismatch: '+name)
    print(json.dumps({'file':name,'bytes':out.stat().st_size,'sha256':sha}),flush=True)
    return {'file':name,'bytes':out.stat().st_size,'sha256':sha,'url':url}
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool: files=list(pool.map(download,names))
manifest={'model':MODEL,'revision':revision,'license':locked['license'] if locked else api.get('cardData',{}).get('license'),'source':'https://huggingface.co/'+MODEL,'runtime':'@huggingface/transformers@3.8.1','pooling':'cls','normalize':True,'dimension':512,'query_prefix':'为这个句子生成表示以用于检索相关文章：','quantization':'Public Xenova ONNX FP16 and INT8 variants; no local conversion','files':files}
shared=sum(f['bytes'] for f in files if not f['file'].startswith('onnx/'))
manifest['totals_bytes']={k:shared+next(f['bytes'] for f in files if f['file']==v) for k,v in {'fp32':'onnx/model.onnx','fp16':'onnx/model_fp16.onnx','q8':'onnx/model_quantized.onnx'}.items()}
manifest['compression_ratio']={k:manifest['totals_bytes']['fp32']/manifest['totals_bytes'][k] for k in ['fp16','q8']}
MANIFEST.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
