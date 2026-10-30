import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {SeekdbClient} from 'seekdb';
import {createServer as createVite} from 'vite';
import {contentQuery,QUERY_RULE} from './src/query.js';
const ROOT=path.dirname(fileURLToPath(import.meta.url));
const DATA=path.resolve(process.env.APP_DATA_DIR||path.join(ROOT,'.data'));
fs.mkdirSync(DATA,{recursive:true});
const manifest=JSON.parse(fs.readFileSync(path.join(ROOT,'model-manifest.json')));
const client=new SeekdbClient({host:'127.0.0.1',port:Number(process.env.SEEKDB_PORT||2894),user:'root',password:'',database:'test'});
const audit=(kind,data)=>fs.appendFileSync(path.join(DATA,'database.jsonl'),JSON.stringify({time:new Date().toISOString(),kind,...data})+'\n');
// The SDK sends GET_SQL and executes database-generated fusion SQL. Persist both.
const internal=client._delegate._internal;
const originalExecute=internal.execute.bind(internal);
internal.execute=async(sql,params)=>{const start=performance.now();try{const r=await originalExecute(sql,params);audit('sql',{sql,params,ms:performance.now()-start,rows:r});return r;}catch(e){audit('sql-error',{sql,error:e.message});throw e;}};
const collections=new Map();
const profiles={gpu_fp16:{dtype:'fp16',device:'webgpu'},wasm_q8:{dtype:'q8',device:'wasm'},wasm_fp32:{dtype:'fp32',device:'wasm'},wasm_fp16:{dtype:'fp16',device:'wasm'}};
function validate(kb,profile){if(!/^[a-zA-Z0-9_-]{1,32}$/.test(kb||'')||!profiles[profile])throw Error('知识库名称需为 1–32 位字母、数字、短横线或下划线');}
async function collection(kb,profile){
  validate(kb,profile);
  const identity=JSON.stringify({kb,revision:manifest.revision,pooling:'cls',normalize:true,dimension:512,queryRule:QUERY_RULE,...profiles[profile]});
  const name='kb_'+crypto.createHash('sha256').update(identity).digest('hex').slice(0,24);
  if(!collections.has(name))collections.set(name,await client.getOrCreateCollection({name,configuration:{dimension:512,distance:'cosine'},embeddingFunction:null,metadata:{kb,profile,identity}}));
  return collections.get(name);
}
function vector(v){if(!Array.isArray(v)||v.length!==512||v.some(n=>!Number.isFinite(n)))throw Error('向量格式不正确，请重新初始化模型');}
function json(res,status,data){res.writeHead(status,{'Content-Type':'application/json; charset=utf-8'});res.end(JSON.stringify(data));}
async function body(req){let s='';for await(const chunk of req){s+=chunk;if(s.length>12_000_000)throw Error('本次导入过大，请分批导入');}return JSON.parse(s||'{}');}
const vite=await createVite({root:ROOT,server:{middlewareMode:true},appType:'spa'});
const server=http.createServer(async(req,res)=>{
  const url=new URL(req.url,'http://localhost');
  res.setHeader('Cross-Origin-Opener-Policy','same-origin');res.setHeader('Cross-Origin-Embedder-Policy','require-corp');
  try{
    if(url.pathname.startsWith('/models/')||url.pathname.startsWith('/ort/')){
      const ort=url.pathname.startsWith('/ort/');const base=path.join(ROOT,ort?'node_modules/onnxruntime-web/dist':'.models');
      const file=path.resolve(base,decodeURIComponent(url.pathname.slice(ort?5:8)));
      if(!file.startsWith(base+path.sep)||!fs.existsSync(file)){res.writeHead(404);res.end();return;}
      res.setHeader('Content-Type',file.endsWith('.json')?'application/json':file.endsWith('.mjs')?'application/javascript':file.endsWith('.wasm')?'application/wasm':'application/octet-stream');res.setHeader('Content-Length',fs.statSync(file).size);res.setHeader('Cache-Control','public,max-age=31536000,immutable');fs.createReadStream(file).pipe(res);return;
    }
    if(url.pathname==='/api/config'){json(res,200,{manifest,profiles});return;}
    if(url.pathname==='/api/status'){
      const c=await collection(url.searchParams.get('kb'),url.searchParams.get('profile'));json(res,200,{count:await c.count(),collection:c.name});return;
    }
    if(url.pathname==='/api/import'&&req.method==='POST'){
      const input=await body(req);const c=await collection(input.kb,input.profile);
      const docs=input.docs;if(!Array.isArray(docs)||!docs.length||docs.length>100)throw Error('每次请导入 1–100 篇文本');
      if(!Array.isArray(input.embeddings)||input.embeddings.length!==docs.length)throw Error('文本与向量数量不一致');
      input.embeddings.forEach(vector);
      for(const d of docs)if(!d.id||typeof d.text!=='string'||!d.text.trim()||d.text.length>10000)throw Error('每篇文本需要 ID 和 1–10000 字正文');
      if(new Set(docs.map(d=>d.id)).size!==docs.length)throw Error('同一批次的文档 ID 不可重复');
      const t=performance.now();await c.upsert({ids:docs.map(d=>d.id),documents:docs.map(d=>d.text),embeddings:input.embeddings,metadatas:docs.map(d=>({title:d.title||d.id,source:d.source||'用户导入',kb:input.kb,profile:input.profile,model_revision:manifest.revision}))});
      const writeMs=performance.now()-t;const refreshStart=performance.now();
      await c.refresh_index();
      // Bound visibility verification; no fixed long sleep. Fail rather than claim ready.
      const deadline=performance.now()+12000;let visible=false;
      do{const r=await c.get({ids:docs.map(d=>d.id),include:['documents']});visible=docs.every((d,i)=>r.ids.includes(d.id)&&r.documents[r.ids.indexOf(d.id)]===d.text);if(!visible)await new Promise(r=>setTimeout(r,120));}while(!visible&&performance.now()<deadline);
      if(!visible)throw Error('索引可见性检查超时，请稍后重试');
      const timings={writeMs,refreshMs:performance.now()-refreshStart};audit('import',{kb:input.kb,profile:input.profile,ids:docs.map(d=>d.id),...timings});json(res,200,{count:await c.count(),...timings,visible:true});return;
    }
    if(url.pathname==='/api/search'&&req.method==='POST'){
      const input=await body(req);vector(input.embedding);if(typeof input.text!=='string'||!input.text.trim())throw Error('请输入搜索内容');
      const c=await collection(input.kb,input.profile),t=performance.now();
      const result=await c.hybridSearch({query:{whereDocument:{$contains:contentQuery(input.text)},nResults:30},knn:{queryEmbeddings:[input.embedding],nResults:30},rank:{rrf:{rankWindowSize:30,rankConstant:60}},nResults:5,include:['documents','metadatas','distances']});
      const searchMs=performance.now()-t;audit('hybridSearch',{kb:input.kb,profile:input.profile,text:input.text,result,searchMs});json(res,200,{result,searchMs});return;
    }
    if(url.pathname==='/api/telemetry'&&req.method==='POST'){const input=await body(req);audit('browser',{event:input});json(res,200,{saved:true});return;}
    vite.middlewares(req,res,()=>{res.writeHead(404);res.end();});
  }catch(e){json(res,400,{error:e.message});}
});
server.listen(Number(process.env.PORT||4312),'0.0.0.0',()=>console.log(JSON.stringify({url:'http://localhost:'+(process.env.PORT||4312),data:DATA,node:process.version,arch:process.arch,seekdb:'1.4.0',embedding:'browser Worker only'})));
async function shutdown(){server.close();await vite.close();await client.close();process.exit(0);}
process.on('SIGTERM',shutdown);process.on('SIGINT',shutdown);
