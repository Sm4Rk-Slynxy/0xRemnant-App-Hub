/* Capture the real dashboard with synthetic app records and modeled Docker.
 * Requires Playwright + Chromium. Never point this at a production registry.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const {spawn} = require('node:child_process');
const {chromium} = require(process.env.APPHUB_PLAYWRIGHT || 'playwright');

(async () => {
  const root = path.resolve(__dirname, '..');
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'apphub-screenshots-'));
  const destination = path.join(root, 'docs', 'screenshots');
  fs.mkdirSync(destination, {recursive:true});
  const port = process.env.APPHUB_SCREENSHOT_PORT || '18766';
  const base = 'http://127.0.0.1:'+port;
  const server = spawn(process.env.PYTHON || 'python3', ['-m','tests.browser_server'], {
    cwd:root, env:{...process.env, APPHUB_TEST_DATA:directory, APPHUB_TEST_PORT:port}, stdio:'ignore'
  });
  let browser;
  try {
    for (let i=0; i<100; i++) {
      try {if ((await fetch(base+'/healthz')).ok) break;} catch {}
      if (server.exitCode !== null) throw new Error('Screenshot fixture failed to start');
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    browser = await chromium.launch({headless:true,args:['--no-sandbox']});
    const page = await browser.newPage({viewport:{width:1440,height:1000},deviceScaleFactor:1});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base);
    await page.locator('#auth:not([hidden])').waitFor();
    await page.locator('#setup-token').fill(fs.readFileSync(path.join(directory,'.setup-token'),'utf8').trim());
    await page.locator('#password').fill(crypto.randomBytes(24).toString('hex'));
    await page.locator('#auth-submit').click();
    await page.locator('#shell:not([hidden])').waitFor();
    const token = await page.evaluate(() => sessionStorage.getItem('apphub-token'));
    async function api(url, body, headers={}) {
      const response = await fetch(base+url, {method:body===undefined?'GET':'POST', headers:{Authorization:'Bearer '+token,'Content-Type':'application/json',...headers},body:body===undefined?undefined:Buffer.isBuffer(body)?body:JSON.stringify(body)});
      const data = await response.json();
      assert.ok(response.ok, data.error);
      return data;
    }
    async function deploy(name, filename) {
      const settings = await api('/api/upload-settings',{env:''});
      const query = new URLSearchParams({name,filename:path.basename(filename)});
      const result = await api('/api/deploy?'+query, fs.readFileSync(filename), {'X-AppHub-Settings':settings.id,'Content-Type':'application/octet-stream'});
      for (let i=0; i<100; i++) {
        const job = await api('/api/jobs/'+result.job_id);
        if (job.status==='succeeded') return result.app_id;
        assert.notEqual(job.status,'failed',job.log);
        await new Promise(resolve => setTimeout(resolve,50));
      }
      throw new Error('Demo deployment timed out');
    }
    await deploy('Little Notes', path.join(root,'examples','hello-app.html'));
    await deploy('Focus Timer', path.join(root,'examples','hello-app.zip'));
    const habit = await deploy('Habit Board', path.join(root,'examples','hello-app.html'));
    await deploy('Persistent Counter', path.join(root,'examples','python-counter.zip'));
    await api('/api/apps/'+habit+'/stop',{});
    await page.reload();
    await page.locator('.app-card').first().waitFor();
    assert.equal(await page.locator('.app-card').count(),4);
    await page.evaluate(() => {
      const label = document.createElement('p');
      label.className='notice';label.id='screenshot-demo-label';
      label.textContent='DEMO DATA · Interface preview · Docker operations simulated';
      document.getElementById('home').prepend(label);
    });
    await page.screenshot({path:path.join(destination,'dashboard.png'),fullPage:true});
    await page.locator('#upload-button').click();
    await page.locator('#zip-file').setInputFiles(path.join(root,'examples','hello-app.html'));
    await page.locator('#app-name').fill('My next little app');
    await page.screenshot({path:path.join(destination,'upload.png'),fullPage:true});
    await page.locator('#upload-dialog [data-close]').first().click();
    const apps = (await api('/api/apps')).apps;
    const notes = apps.find(app=>app.name==='Little Notes');
    const html = fs.readFileSync(path.join(root,'examples','hello-app.html'),'utf8');
    await page.route('http://127.0.0.1:'+notes.port+'/**', route=>route.fulfill({status:200,contentType:'text/html',body:html}));
    await page.locator('.app-card').filter({has:page.getByRole('heading',{name:'Little Notes',exact:true})}).locator('.card-open').click();
    const note = page.frameLocator('.app-frame').locator('#note');
    await note.fill('A place for the things I want to build.\n\n• A recipe keeper\n• A small garden journal\n• A weekend project dashboard\n\nThis is synthetic example content.');
    await page.locator('.pane-hint').evaluate(element => {element.textContent='DEMO DATA · Little Notes running in a browser-intercepted frame; Docker is simulated.';});
    await page.screenshot({path:path.join(destination,'app-tab.png'),fullPage:true});
    await page.locator('[data-tab="home"]').click();
    await page.setViewportSize({width:390,height:844});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:path.join(destination,'mobile.png'),fullPage:true});
    assert.deepEqual(errors,[]);
    console.log('PASS: four sanitized screenshots captured with synthetic data; no browser errors. Docker modeled.');
  } finally {
    if (browser) await browser.close();
    const exited = new Promise(resolve => server.once('exit',resolve));
    server.kill('SIGTERM');
    if (server.exitCode===null) await exited;
    fs.rmSync(directory,{recursive:true,force:true});
  }
})().catch(error=>{console.error(error);process.exit(1);});
