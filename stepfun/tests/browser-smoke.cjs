const {chromium}=require('playwright');const fs=require('fs'),path=require('path');
(async()=>{
  const root=path.resolve(__dirname,'..');const out=path.join(root,'evidence','smoke');fs.mkdirSync(out,{recursive:true});
  const errors=[],requests=[];
  const browser=await chromium.launch({channel:'chrome',headless:false,args:['--no-default-browser-check']});
  const context=await browser.newContext({viewport:{width:1440,height:1000}});
  await context.route('**/*',route=>{const u=new URL(route.request().url());if(!['localhost','127.0.0.1'].includes(u.hostname)){requests.push({blocked:true,url:u.origin+u.pathname});return route.abort();}return route.continue();});
  const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(m.type()==='error')errors.push(m.text());});page.on('request',r=>requests.push({url:r.url(),method:r.method(),resource:r.resourceType()}));
  try{
    await page.goto(process.env.APP_URL||'http://localhost:4312');await page.waitForFunction(()=>window.notebook?.ready,{timeout:60000});
    const initialized=await page.evaluate(()=>window.notebook.initialize('auto'));console.log(JSON.stringify({profile:initialized.profile,adapter:initialized.adapterInfo,loadMs:initialized.loadMs}));
    const imported=await page.evaluate(async()=>{const docs=(await(await fetch('/data/corpus.json')).json()).slice(0,3);return window.notebook.importDocs(docs);});
    const result=await page.evaluate(()=>window.notebook.search('手机找不到了，怎样保护账号？'));
    await page.screenshot({path:path.join(out,'app.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(out,'mobile.png'),fullPage:true});
    fs.writeFileSync(path.join(out,'result.json'),JSON.stringify({version:browser.version(),initialized,imported,result,errors,requests},null,2));
    console.log(JSON.stringify({ids:result.result.ids,errors}));
  }catch(e){fs.writeFileSync(path.join(out,'failure.json'),JSON.stringify({error:e.stack,errors,requests},null,2));throw e;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
