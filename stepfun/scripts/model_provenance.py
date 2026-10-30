import hashlib,json,pathlib,urllib.request
root=pathlib.Path(__file__).resolve().parents[1]
def get(url):
    with urllib.request.urlopen(url,timeout=45) as response:return response.read()
upstream=json.loads(get('https://huggingface.co/api/models/BAAI/bge-small-zh-v1.5'))
revision=upstream['sha'];readme_url=f'https://huggingface.co/BAAI/bge-small-zh-v1.5/resolve/{revision}/README.md'
readme=get(readme_url);out=root/'licenses';out.mkdir(exist_ok=True)
(out/'BAAI-bge-small-zh-v1.5-README.md').write_bytes(readme)
xmanifest=json.loads((root/'model-manifest.json').read_text())
xreadme_url=f"https://huggingface.co/{xmanifest['model']}/resolve/{xmanifest['revision']}/README.md"
xreadme=get(xreadme_url);(out/'Xenova-bge-small-zh-v1.5-README.md').write_bytes(xreadme)
card=upstream.get('cardData') or {}
license_value=card.get('license')
if not license_value:
    for line in readme.decode().splitlines():
        if line.startswith('license:'):license_value=line.split(':',1)[1].strip()
assert license_value=='mit',(license_value,card)
provenance={'upstream_model':'BAAI/bge-small-zh-v1.5','upstream_revision':revision,'upstream_license':license_value,'upstream_card_url':readme_url,'upstream_card_sha256':hashlib.sha256(readme).hexdigest(),'onnx_conversion_model':xmanifest['model'],'onnx_revision':xmanifest['revision'],'onnx_card_url':xreadme_url,'onnx_card_sha256':hashlib.sha256(xreadme).hexdigest(),'pooling_source':'BAAI model card: use CLS token of last_hidden_state and L2 normalization','query_prefix':'为这个句子生成表示以用于检索相关文章：'}
(root/'model-provenance.json').write_text(json.dumps(provenance,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
xmanifest['license']='MIT (upstream BAAI model; pinned cards in licenses/)';(root/'model-manifest.json').write_text(json.dumps(xmanifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'upstream_license':license_value,'upstream_revision':revision}))
