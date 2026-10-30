import {contentQuery,QUERY_RULE} from './query.js';
const $=s=>document.querySelector(s);
const worker=new Worker(new URL('./embedding.worker.js',import.meta.url),{type:'module'});
const pending=new Map();let seq=0,active=null,busy=false,config=null;
let kb=localStorage.getItem('zh-page-kb')||'notes';if(![...$('#kb').options].some(o=>o.value===kb))kb='notes';$('#kb').value=kb;
$('#current').textContent=$('#kb').selectedOptions[0].textContent;$('#count').textContent='加载模型后显示文档数';
const logs=[];const status=s=>{$('#notice').textContent=s;$('#notice').classList.remove('error');};
const telemetry=e=>{logs.push(e);fetch('/api/telemetry',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(e)}).catch(()=>{});};
worker.onmessage=({data:m})=>{const call=pending.get(m.id);if(!call)return;if(m.kind==='done'){pending.delete(m.id);call.resolve(m.value);}else if(m.kind==='error'){pending.delete(m.id);call.reject(Error(m.error));}else{if(m.kind==='embeddingProgress'&&!call.query){$('#progress').value=m.completed/m.total*100;$('#import-status').textContent=`正在生成向量 ${m.completed} / ${m.total}`;}if(m.kind==='progress'&&m.event.status==='progress')$('#import-status').textContent=`正在加载模型 ${Math.round(m.event.progress||0)}%`;call.onProgress?.(m);}};
worker.onerror=e=>{for(const p of pending.values())p.reject(Error(e.message));pending.clear();};
const call=(type,value={},onProgress)=>new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject,onProgress,query:value.query});worker.postMessage({id,type,...value});});
async function api(route,data){const r=await fetch('/api/'+route,data?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}:undefined);const out=await r.json();if(!r.ok)throw Error(out.error);return out;}
async function initialize(requested=$('#precision').value){
  if(active?.requested===requested)return active;
  $('#backend').textContent='正在加载本地模型…';
  active=await call('initialize',{requested});
  $('#import-status').textContent='模型已就绪。';
  $('#backend').textContent=active.device==='webgpu'?'WebGPU / FP16':`WASM / ${active.dtype==='q8'?'INT8':active.dtype.toUpperCase()}${active.fallback?'（已自动降级）':''}`;
  telemetry({type:'initialized',...active});await refreshCount();return active;
}
async function refreshCount(){if(!active)return;const selectedKb=kb,profile=active.profile;const r=await api('status?'+new URLSearchParams({kb:selectedKb,profile}));if(kb===selectedKb&&active?.profile===profile){$('#count').textContent=r.count+' 篇文档';$('#current').textContent=$('#kb').selectedOptions[0].textContent;}return r;}
function setBusy(value){busy=value;for(const s of ['#import','#sample','#search','#kb','#precision'])$(s).disabled=value;$('#progress').hidden=!value;}
async function guarded(fn){if(busy)return;setBusy(true);try{return await fn();}catch(e){$('#notice').textContent=e.message;$('#notice').classList.add('error');throw e;}finally{setBusy(false);}}
async function importDocs(docs,{batchSize=4,requested}={}){
  if(!Array.isArray(docs)||!docs.length||docs.length>100)throw Error('每次请导入 1–100 篇文本；JSON 文件需为文档数组。');
  if(docs.some(d=>!d||typeof d.id!=='string'||!d.id.trim()||typeof d.text!=='string'||!d.text.trim()||d.text.length>10000))throw Error('每篇笔记需要非空 ID 与 1–10000 字正文。');
  if(new Set(docs.map(d=>d.id)).size!==docs.length)throw Error('同一批次的文档 ID 不可重复。');
  const start=performance.now();await initialize(requested||$('#precision').value);const selectedKb=kb,profile=active.profile;
  status(`正在向「${$('#kb').selectedOptions[0].textContent}」导入 ${docs.length} 篇笔记…`);
  const embedded=await call('embed',{texts:docs.map(d=>d.text),batchSize,query:false});
  $('#import-status').textContent='正在写入并刷新索引…';
  const saved=await api('import',{kb:selectedKb,profile,docs,embeddings:embedded.vectors});
  const record={type:'import',kb:selectedKb,profile,count:docs.length,batchSize,embeddingMs:embedded.ms,batches:embedded.batches,...saved,endToEndMs:performance.now()-start};telemetry(record);
  await refreshCount();$('#import-status').textContent=`已导入 ${docs.length} 篇，可立即检索。`;status('导入完成，输入问题开始搜索。');return record;
}
function render(result){const root=$('#results');root.replaceChildren();const ids=result.ids?.[0]||[];if(!ids.length){const p=document.createElement('p');p.textContent='没有找到匹配的笔记。试试更具体的表达，或先导入资料。';root.append(p);return;}
  ids.forEach((id,i)=>{const card=document.createElement('article'),rank=document.createElement('span'),content=document.createElement('div'),title=document.createElement('h2'),text=document.createElement('p'),meta=document.createElement('small');rank.className='rank';rank.textContent=String(i+1).padStart(2,'0');title.textContent=result.metadatas?.[0]?.[i]?.title||id;text.textContent=result.documents?.[0]?.[i]||'';meta.textContent='文档 ID：'+id;content.append(title,text,meta);card.append(rank,content);root.append(card);});
}
async function search(text,{requested}={}){
  if(!text.trim())throw Error('请输入搜索内容');const start=performance.now();await initialize(requested||$('#precision').value);const selectedKb=kb,profile=active.profile;
  const normalized=contentQuery(text);const embedded=await call('embed',{texts:[text],batchSize:1,query:true});const response=await api('search',{kb:selectedKb,profile,text,embedding:embedded.vectors[0]});render(response.result);
  $('#results').getBoundingClientRect();
  const record={type:'search',kb:selectedKb,profile,text,normalized,queryRule:QUERY_RULE,queryEmbeddingMs:embedded.ms,searchMs:response.searchMs,endToEndMs:performance.now()-start,result:response.result};telemetry(record);status(`找到 ${response.result.ids?.[0]?.length||0} 条结果`);return record;
}
function textDocs(s,name='笔记'){return s.split(/\r?\n\s*\r?\n/).map(s=>s.trim()).filter(Boolean).map((text,i)=>({id:`${name}-${i+1}-${hash(text)}`,title:text.split('\n')[0].slice(0,40),text}));}
function hash(s){let h=2166136261;for(const c of s)h=Math.imul(h^c.charCodeAt(0),16777619);return (h>>>0).toString(16);}
$('#search-form').addEventListener('submit',e=>{e.preventDefault();guarded(()=>search($('#query').value)).catch(()=>{});});
$('#import').onclick=()=>guarded(async()=>{let docs=textDocs($('#text').value);for(const file of $('#files').files){const text=await file.text();if(file.name.endsWith('.json')){let parsed;try{parsed=JSON.parse(text);}catch{throw Error('JSON 文件无法读取，请检查文档数组格式。');}if(!Array.isArray(parsed))throw Error('JSON 文件需为包含 id、text 的文档数组。');docs.push(...parsed);}else docs.push(...textDocs(text,file.name));}if(!docs.length)throw Error('请粘贴文本或选择文件');return importDocs(docs);}).catch(()=>{});
$('#sample').onclick=()=>guarded(async()=>importDocs(await (await fetch('/data/corpus.json')).json())).catch(()=>{});
$('#kb').onchange=async()=>{kb=$('#kb').value;localStorage.setItem('zh-page-kb',kb);$('#results').replaceChildren();$('#current').textContent=$('#kb').selectedOptions[0].textContent;try{await refreshCount();status('已切换知识库。');}catch(e){status(e.message);}};
$('#precision').onchange=()=>guarded(async()=>{await initialize();status('已切换到匹配模型方案的独立集合；请导入资料或搜索已有数据。');}).catch(()=>{});
config=await api('config');await call('configure',{config});
// The verification harness invokes the same Worker/API/render functions as user actions.
window.notebook={ready:true,initialize,importDocs,search,logs,get active(){return active;},get kb(){return kb;},async setKb(name){$('#kb').value=name;await $('#kb').onchange();},async setProfile(name){$('#precision').value=name;return initialize(name);},async encodeQuery(text){await initialize();return call('embed',{texts:[text],batchSize:1,query:true});},refreshCount};
