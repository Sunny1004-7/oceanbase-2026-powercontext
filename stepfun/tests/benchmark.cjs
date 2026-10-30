const {chromium}=require('playwright');const fs=require('fs'),path=require('path'),assert=require('assert');
const root=path.resolve(__dirname,'..');const corpus=JSON.parse(fs.readFileSync(path.join(root,'data/corpus.json'))),queries=JSON.parse(fs.readFileSync(path.join(root,'data/queries.json')));
const out=path.join(root,'evidence',process.env.RUN_ID||'acceptance-01');fs.mkdirSync(out,{recursive:true});
function save(name,value){fs.writeFileSync(path.join(out,name),JSON.stringify(value,null,2));}
async function launch(disableGPU=false){
 const browser=await chromium.launch({channel:'chrome',headless:false,args:['--no-default-browser-check','--disable-background-timer-throttling','--disable-backgrounding-occluded-windows','--disable-renderer-backgrounding',...(disableGPU?['--disable-gpu','--disable-software-rasterizer']:[])]});
 const context=await browser.newContext({viewport:{width:1440,height:1000}});const network=[],consoleMessages=[],pageErrors=[];
 await context.route('**/*',route=>{const u=new URL(route.request().url());if(!['localhost','127.0.0.1'].includes(u.hostname)){network.push({blocked:true,url:u.origin+u.pathname});return route.abort();}return route.continue();});
 context.on('request',r=>network.push({url:r.url(),method:r.method(),resource:r.resourceType()}));
 const page=await context.newPage();page.on('pageerror',e=>pageErrors.push(e.message));page.on('console',m=>{if(['warning','error'].includes(m.type()))consoleMessages.push({type:m.type(),message:m.text()});});
 await page.goto(process.env.APP_URL||'http://localhost:4312');await page.waitForFunction(()=>window.notebook?.ready,{timeout:60000});
 return {browser,context,page,network,consoleMessages,pageErrors};
}
async function scenario(label,requested,disabled=false){
 const env=await launch(disabled);const {page,browser}=env;const result={label,requested,disableGPU:disabled,browserVersion:browser.version(),headed:true,imports:[],queries:[]};
 try{
  await page.evaluate(()=>window.notebook.setKb('eval'));
  result.initialized=await page.evaluate(r=>window.notebook.setProfile(r),requested);
  if(disabled){assert.equal(result.initialized.profile,'wasm_q8');assert(result.initialized.fallback);assert((await page.locator('#backend').innerText()).includes('自动降级'));}
  if(requested==='auto'&&!disabled){assert.equal(result.initialized.profile,'gpu_fp16');assert.equal(result.initialized.adapterInfo.isFallbackAdapter,false);assert.equal(result.initialized.adapterInfo.vendor,'nvidia');}
  for(let repeat=0;repeat<3;repeat++){const r=await page.evaluate(d=>window.notebook.importDocs(d.docs,{batchSize:d.batchSize}),{docs:corpus,batchSize:4});result.imports.push({...r,repeat});save(label+'-partial.json',result);console.log(label+' import '+(repeat+1)+'/3');}
  result.warmup=await page.evaluate(t=>window.notebook.search(t),queries[0].text);
  for(let repeat=0;repeat<5;repeat++){for(const q of queries){const r=await page.evaluate(t=>window.notebook.search(t),q.text);const ids=r.result.ids[0];result.queries.push({...r,queryId:q.id,repeat,recallAt5:q.relevant_ids.filter(id=>ids.includes(id)).length/q.relevant_ids.length});}save(label+'-partial.json',result);console.log(label+' queries '+(repeat+1)+'/5');}
  if(label==='gpu-fp16'){
   result.batchOneImports=[];for(let i=0;i<3;i++)result.batchOneImports.push(await page.evaluate(d=>window.notebook.importDocs(d,{batchSize:1}),corpus));
   await page.evaluate(()=>window.notebook.setKb('work'));assert.equal((await page.evaluate(()=>window.notebook.refreshCount())).count,0);
   const workdoc={id:'increment-01',title:'增量实验',text:'蓝鲸书签计划：周五整理海洋读书笔记，负责人小林。'};
   result.increment=await page.evaluate(d=>window.notebook.importDocs([d]),workdoc);assert.equal(result.increment.count,1);
   result.incrementQuery=await page.evaluate(()=>window.notebook.search('蓝鲸书签计划负责人'));assert(result.incrementQuery.result.ids[0].includes(workdoc.id));
   const updated={...workdoc,text:'蓝鲸书签计划已经更新：周六整理海洋读书笔记，负责人小周。'};
   result.updated=await page.evaluate(d=>window.notebook.importDocs([d]),updated);assert.equal(result.updated.count,1);
   result.updatedQuery=await page.evaluate(()=>window.notebook.search('蓝鲸书签计划负责人'));assert(result.updatedQuery.result.documents[0].includes(updated.text));
   await page.evaluate(()=>window.notebook.setKb('eval'));result.switched=await page.evaluate(()=>window.notebook.refreshCount());assert.equal(result.switched.count,50);
   result.isolated=await page.evaluate(()=>window.notebook.search('蓝鲸书签计划负责人'));assert(!result.isolated.result.ids[0].includes(workdoc.id));
   await page.reload();await page.waitForFunction(()=>window.notebook?.ready);await page.evaluate(()=>window.notebook.initialize());result.afterReload=await page.evaluate(()=>window.notebook.refreshCount());assert.equal(result.afterReload.count,50);
   await page.fill('#query',queries[0].text);await page.click('#search');await page.waitForFunction(()=>document.querySelector('#notice').textContent.startsWith('找到'));
   await page.screenshot({path:path.join(out,'desktop.png'),fullPage:true});
   await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(out,'mobile.png'),fullPage:true});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   result.keyboardFocus=await page.evaluate(()=>{document.querySelector('#query').focus();return document.activeElement.id;});assert.equal(result.keyboardFocus,'query');
  }
  result.backendText=await page.locator('#backend').innerText();result.pageErrors=env.pageErrors;result.consoleMessages=env.consoleMessages;result.network=env.network;
  result.resources=await page.evaluate(()=>performance.getEntriesByType('resource').map(r=>({name:r.name,duration:r.duration,transferSize:r.transferSize,encodedBodySize:r.encodedBodySize,decodedBodySize:r.decodedBodySize})));
  assert.deepEqual(env.pageErrors,[]);assert(!env.network.some(r=>r.blocked));
  save(label+'.json',result);console.log(label+' done, Recall@5='+result.queries.reduce((s,q)=>s+q.recallAt5,0)/result.queries.length);
  return result;
 }catch(e){save(label+'-failure.json',{...result,error:e.stack,network:env.network,console:env.consoleMessages,pageErrors:env.pageErrors});throw e;}finally{await browser.close();}
}
(async()=>{
 if(process.env.RESTART_ONLY){const e=await launch();try{await e.page.evaluate(()=>window.notebook.setKb('eval'));await e.page.evaluate(()=>window.notebook.initialize());const count=await e.page.evaluate(()=>window.notebook.refreshCount());const q=await e.page.evaluate(t=>window.notebook.search(t),queries[0].text);assert.equal(count.count,50);assert(queries[0].relevant_ids.every(id=>q.result.ids[0].includes(id)));save('after-server-restart.json',{count,query:q,browserVersion:e.browser.version(),pageErrors:e.pageErrors});console.log('server restart persistence passed');}finally{await e.browser.close();}return;}
 const results=[];for(const [name,profile,disabled] of [['gpu-fp16','auto',false],['wasm-fp32','wasm_fp32',false],['wasm-int8','wasm_q8',false],['automatic-fallback','auto',true]])results.push(await scenario(name,profile,disabled));
 save('complete.json',{scenarios:results.map(r=>({label:r.label,profile:r.initialized.profile,recallAt5:r.queries.reduce((s,q)=>s+q.recallAt5,0)/r.queries.length,imports:r.imports.length,querySamples:r.queries.length})),completed:true});
})().catch(e=>{console.error(e);process.exit(1);});
