import {pipeline,env} from '@huggingface/transformers';
env.allowRemoteModels=false;env.allowLocalModels=true;env.localModelPath='/models/';env.useBrowserCache=true;
env.backends.onnx.wasm.numThreads=1;
env.backends.onnx.wasm.wasmPaths='/ort/';
let extractor=null,active=null,config=null;
const tell=(id,kind,value)=>self.postMessage({id,kind,...value});
async function initialize(id,requested){
  if(extractor&&active.requested===requested)return active;
  if(extractor){await extractor.dispose();extractor=null;}
  const loadStart=performance.now();const events=[];let adapterInfo=null,fallback=null,profile=requested;
  const progressSeen=new Map();
  const options={progress_callback:e=>{const key=e.file||e.status;const step=e.status==='progress'?Math.floor(e.progress||0):e.status;if(progressSeen.get(key)===step)return;progressSeen.set(key,step);events.push({...e,time:performance.now()});tell(id,'progress',{event:e});}};
  if(requested==='auto'){
    try{
      if(!navigator.gpu)throw Error('当前浏览器未提供 WebGPU');
      const adapter=await navigator.gpu.requestAdapter({powerPreference:'high-performance'});
      if(!adapter)throw Error('未找到可用 GPU');
      adapterInfo=adapter.info?{vendor:adapter.info.vendor,architecture:adapter.info.architecture,device:adapter.info.device,description:adapter.info.description,isFallbackAdapter:adapter.info.isFallbackAdapter}:null;
      extractor=await pipeline('feature-extraction',config.manifest.model,{...options,device:'webgpu',dtype:'fp16'});profile='gpu_fp16';
    }catch(e){fallback=String(e.message||e);profile='wasm_q8';tell(id,'fallback',{reason:fallback});}
  }
  if(!extractor){const scheme=config.profiles[profile];extractor=await pipeline('feature-extraction',config.manifest.model,{...options,device:scheme.device,dtype:scheme.dtype});}
  active={requested,profile,...config.profiles[profile],adapterInfo,fallback,loadMs:performance.now()-loadStart,loadEvents:events,location:'DedicatedWorkerGlobalScope',userAgent:navigator.userAgent,workerUrl:self.location.href};
  return active;
}
let chain=Promise.resolve();
self.onmessage=({data:m})=>{chain=chain.then(async()=>{
  try{
    if(m.type==='configure'){config=m.config;tell(m.id,'done',{value:true});return;}
    if(m.type==='initialize'){tell(m.id,'done',{value:await initialize(m.id,m.requested)});return;}
    if(m.type==='embed'){
      if(!extractor)throw Error('请先初始化模型');
      const times=[],vectors=[];const start=performance.now();
      for(let i=0;i<m.texts.length;i+=m.batchSize){const batch=m.texts.slice(i,i+m.batchSize).map(text=>m.query?config.manifest.query_prefix+text:text);const t=performance.now();const tensor=await extractor(batch,{pooling:'cls',normalize:true,truncation:true,max_length:512});vectors.push(...tensor.tolist());times.push({offset:i,count:batch.length,ms:performance.now()-t});tell(m.id,'embeddingProgress',{completed:vectors.length,total:m.texts.length});}
      tell(m.id,'done',{value:{vectors,ms:performance.now()-start,batches:times,profile:active.profile,execution:active.location}});return;
    }
    throw Error('未知模型操作');
  }catch(e){tell(m.id,'error',{error:String(e.message||e)});}
});};
