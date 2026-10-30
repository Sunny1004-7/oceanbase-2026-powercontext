"""Summarize unmodified browser samples; report every sample, never the fastest only."""
import csv,hashlib,json,pathlib,statistics,sys
root=pathlib.Path(__file__).resolve().parents[1];folder=root/'evidence/final-acceptance'
manifest=json.loads((root/'model-manifest.json').read_text());frozen=json.loads((root/'data/frozen.json').read_text())
for name,expected in frozen['files'].items():assert hashlib.sha256((root/'data'/name).read_bytes()).hexdigest()==expected
summary={'device':{'os':'Windows 11','browser':'Chrome 156.0.8078.74 (headed)','gpu':'NVIDIA GeForce RTX 4060 Laptop GPU','driver':'32.0.16.2101','linux_service':'WSL2 Ubuntu 24.04 x86_64; Node 22.14.0; seekdb 1.4.0.0'},'runtime':'@huggingface/transformers 3.8.1','dataset':frozen,'model':{'id':manifest['model'],'revision':manifest['revision'],'totals_bytes':manifest['totals_bytes'],'compression_ratio':manifest['compression_ratio']},'end_to_end_definition':'Browser submission until result DOM and synchronous layout are ready; includes inference, HTTP, seekdb query and rendering; excludes prior model initialization in warm samples','timing_units':'milliseconds','rows':[]}
samples=[]
for label in ['gpu-fp16','wasm-fp32','wasm-int8','automatic-fallback']:
    d=json.loads((folder/(label+'.json')).read_text());imports=d['imports'];queries=d['queries'];init=d['initialized']
    files={}
    for event in init['loadEvents']:
        if 'file' not in event:continue
        state=files.setdefault(event['file'],{})
        if event['status']=='download':state['start']=event['time']
        if event['status']=='done':state['end']=event['time']
        if event['status']=='progress':state['bytes']=event.get('loaded')
    for v in files.values():
        if 'end' in v and 'start' in v:v['fetch_ms']=v['end']-v['start']
    onnx=next(v for k,v in files.items() if k.endswith('.onnx'))
    row={'configuration':label,'profile':init['profile'],'adapter':init['adapterInfo'],'fallback_reason':init['fallback'],'model_cold_load_ms':init['loadMs'],'onnx_fetch_ms':onnx.get('fetch_ms'),'onnx_download_bytes':onnx.get('bytes'),'model_fetch_events':files,'documents':50,'full_import_repeats':len(imports),'warm_query_samples':len(queries),'document_inference_ms_median':statistics.median(x['embeddingMs'] for x in imports),'write_ms_median':statistics.median(x['writeMs'] for x in imports),'refresh_ms_median':statistics.median(x['refreshMs'] for x in imports),'query_inference_ms_median':statistics.median(x['queryEmbeddingMs'] for x in queries),'seekdb_ms_median':statistics.median(x['searchMs'] for x in queries),'end_to_end_ms_median':statistics.median(x['endToEndMs'] for x in queries),'recall_at_5':statistics.mean(x['recallAt5'] for x in queries),'page_error_count':len(d['pageErrors']),'external_requests':len([x for x in d['network'] if x.get('blocked')]),'backend_ui':d['backendText']}
    row['documents_per_second']=50/(row['document_inference_ms_median']/1000)
    if 'batchOneImports' in d:
        one=statistics.median(x['embeddingMs'] for x in d['batchOneImports']);row['batch_comparison']={'batch1_median_ms':one,'batch4_median_ms':row['document_inference_ms_median'],'ratio_batch1_over_batch4':one/row['document_inference_ms_median'],'repeats_each':3,'constant':'same GPU, fp16, model, 50 docs, backend and query configuration','limitation':'batch4 measured before batch1; medians include all three full imports, no random order'}
    summary['rows'].append(row)
    for q in queries:samples.append({'configuration':label,'query_id':q['queryId'],'repeat':q['repeat'],'query_inference_ms':q['queryEmbeddingMs'],'seekdb_ms':q['searchMs'],'end_to_end_ms':q['endToEndMs'],'recall_at_5':q['recallAt5'],'ids':'|'.join(q['result']['ids'][0])})
baseline=next(r for r in summary['rows'] if r['configuration']=='wasm-fp32')['recall_at_5']
for r in summary['rows']:r['absolute_quality_drop_from_fp32']=baseline-r['recall_at_5']
summary['engineering_target_passed']=all(r['recall_at_5']>=.85 and r['absolute_quality_drop_from_fp32']<=.05 for r in summary['rows'])
summary['comparison_limits']=['FP32 vs INT8 on WASM isolates precision; GPU/FP16 vs WASM/INT8 changes both backend and precision','Fixed small hand-labeled development set; query cleanup was refined on these queries, so this is not an independent generalization estimate','Early direct-question configuration had Recall@5=0.75; preserved in preliminary evidence. No corpus/query labels were removed or changed','WebGPU uses a real non-fallback NVIDIA adapter; controlled fallback uses Chrome --disable-gpu --disable-software-rasterizer, automatic request remains auto','Earlier two-requestAnimationFrame presentation barrier inflated background-window latency; final app measures synchronous result layout readiness']
(root/'evidence/summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
with (root/'evidence/query-samples.csv').open('w',newline='',encoding='utf-8') as f:w=csv.DictWriter(f,fieldnames=list(samples[0]));w.writeheader();w.writerows(samples)
print(json.dumps({'target_passed':summary['engineering_target_passed'],'rows':[{k:r[k] for k in ['configuration','recall_at_5','model_cold_load_ms','document_inference_ms_median','query_inference_ms_median','seekdb_ms_median','end_to_end_ms_median']} for r in summary['rows']],'sizes':summary['model'],'batch':summary['rows'][0].get('batch_comparison')},ensure_ascii=False,indent=2))
