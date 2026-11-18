// Real Chromium smoke tests over its debugging pipe (no npm dependencies).
// Starts only tests/browser_fixture.py, whose database and uploads are temporary.
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');
const net = require('node:net');
const cwd = path.resolve(__dirname, '..');
const delay = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const port = await new Promise((resolve, reject) => { const s = net.createServer(); s.on('error', reject); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); }); });
  const base = `http://127.0.0.1:${port}`;
  const server = spawn(path.join(cwd, '.venv', 'Scripts', 'python.exe'), ['-B', 'tests/browser_fixture.py', String(port)], { cwd, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
  let serverLog = '';
  server.stderr.on('data', (b) => { serverLog += b; });
  server.stdout.on('data', (b) => { serverLog += b; });
  let browser;
  const tempRoot = fs.realpathSync(os.tmpdir());
  const profile = fs.mkdtempSync(path.join(tempRoot, 'codd-accounting-browser-'));
  try {
    let ready = false;
    for (let i = 0; i < 150; i++) { try { if ((await fetch(base + '/api/auth/me')).ok) { ready = true; break; } } catch {} await delay(100); }
    assert(ready, serverLog);
    const chrome = ['C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe', 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'].find(fs.existsSync);
    assert(chrome, 'Chromium browser not found');
    browser = spawn(chrome, ['--headless=new', '--no-first-run', '--no-default-browser-check', '--disable-background-networking', '--disable-sync', '--remote-debugging-pipe', `--user-data-dir=${profile}`, 'about:blank'], { windowsHide: true, stdio: ['ignore', 'ignore', 'pipe', 'pipe', 'pipe'] });
    browser.stderr.resume();
    const pending = new Map(); let seq = 0, buffer = '', session;
    const exceptions = [], requests = [], exports = [];
    const downloads = path.join(profile, 'downloads'); fs.mkdirSync(downloads);
    browser.stdio[4].on('data', (chunk) => {
      buffer += chunk.toString(); let end;
      while ((end = buffer.indexOf('\0')) >= 0) {
        const msg = JSON.parse(buffer.slice(0, end)); buffer = buffer.slice(end + 1);
        if (msg.id) { const p = pending.get(msg.id); if (p) { clearTimeout(p.timer); pending.delete(msg.id); msg.error ? p.reject(new Error(JSON.stringify(msg.error))) : p.resolve(msg.result); } }
        if (msg.method === 'Runtime.exceptionThrown') exceptions.push(msg.params.exceptionDetails.exception?.description || msg.params.exceptionDetails.text);
        if (msg.method === 'Network.requestWillBeSent') {
          requests.push(msg.params.request.url);
          if (msg.params.request.url.endsWith('/api/accounting/export')) exports.push(JSON.parse(msg.params.request.postData));
        }
        if (msg.method === 'Page.javascriptDialogOpening') send('Page.handleJavaScriptDialog', { accept: true }).catch(() => {});
      }
    });
    function send(method, params = {}, target = session) {
      return new Promise((resolve, reject) => { const id = ++seq; const timer = setTimeout(() => { pending.delete(id); reject(new Error('CDP timeout: ' + method)); }, 15000); pending.set(id, { resolve, reject, timer }); browser.stdio[3].write(JSON.stringify({ id, method, params, ...(target ? { sessionId: target } : {}) }) + '\0'); });
    }
    const { targetId } = await send('Target.createTarget', { url: 'about:blank' }, null);
    session = (await send('Target.attachToTarget', { targetId, flatten: true }, null)).sessionId;
    await send('Browser.setDownloadBehavior', { behavior: 'allow', downloadPath: downloads }, null);
    await send('Runtime.enable'); await send('Page.enable'); await send('Network.enable');
    async function evaluate(expression) { const result = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true, userGesture: true }); if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text); return result.result.value; }
    async function until(expression) { for (let i = 0; i < 100; i++) { if (await evaluate(expression)) return; await delay(100); } throw new Error('UI timeout: ' + expression + '\n' + await evaluate('document.body.innerText')); }
    const click = (selector) => evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
    const type = (selector, value) => evaluate(`{ const el = document.querySelector(${JSON.stringify(selector)}); el.value = ${JSON.stringify(value)}; el.dispatchEvent(new Event('input', {bubbles:true})); }`);
    const navigate = async (url) => { await send('Page.navigate', { url: base + url }); };
    const fields = await (await fetch(base + '/__test/fields')).json();
    const field = (name) => `[data-category-field="${fields[name]}"]`;

    const screenshot = async (name) => {
      if (!process.env.ACCOUNTING_SCREENSHOT_DIR) return;
      const shot = await send('Page.captureScreenshot', { format: 'png' });
      fs.writeFileSync(path.join(process.env.ACCOUNTING_SCREENSHOT_DIR, name + '.png'), Buffer.from(shot.data, 'base64'));
    };
    await send('Emulation.setDeviceMetricsOverride', { width: 1600, height: 1100, deviceScaleFactor: 1, mobile: false });
    await navigate('/apk'); await until('!!document.querySelector("[data-acc-add]")');
    await evaluate("localStorage.setItem('theme','light'); document.documentElement.setAttribute('data-theme','light')");
    assert.equal(await evaluate('document.querySelectorAll("[data-brick]").length'), 0);
    assert.equal(await evaluate('!!document.querySelector("#apkSortFilter, #apkCertificateFilter")'), false);
    await click('[data-acc-add]'); await type('#accName', 'Камера М-4, 1120 км');
    assert.equal(await evaluate('document.querySelectorAll("#accFind input").length'), 2);
    await type('#accInventory', 'NOT-FOUND'); await evaluate('document.querySelector("#accFind").requestSubmit()');
    await until('document.querySelector("#accFindState").textContent.includes("АПК не найден")');
    await type('#accInventory', '00123'); await evaluate('document.querySelector("#accFind").requestSubmit()');
    await until('document.querySelectorAll("[data-candidate]").length === 2');
    assert(await evaluate('document.querySelector("#accAddSave").disabled'), 'No automatic selection of duplicate');
    await click('[data-candidate]'); await until('!document.querySelector("#accAddSave").disabled');
    await type('#accInventory', '00999');
    assert(await evaluate('document.querySelector("#accAddSave").disabled'), 'Changing inventory invalidates selection');
    await evaluate('document.querySelector("#accFind").requestSubmit()');
    await until('document.querySelectorAll("[data-candidate]").length === 1');
    assert(await evaluate('document.querySelector("#accAddSave").disabled'), 'Even one match needs confirmation');
    await click('[data-candidate]'); await until('!document.querySelector("#accAddSave").disabled');
    await screenshot('accounting-add');
    await click('#accAddSave'); await until('!document.querySelector("#accFind") && document.querySelectorAll("[data-brick]").length === 1');
    assert.equal((await (await fetch(base + '/__test/count')).json()).count, 3);
    const initial = await (await fetch(base + '/api/accounting/point?point_id=B')).json();
    assert.equal(initial.accounting_name, 'Камера М-4, 1120 км');
    assert.equal(initial.accounting_inventory, '00999');
    assert.equal(initial.accounting_fields.filter((f) => f.value).length, 2, 'Initial add saves only two fields');
    await screenshot('accounting-list');
    await click('[data-acc-open]'); await until('!!document.querySelector("#pcCategoryFields")');
    assert.equal(await evaluate('document.querySelectorAll("[data-category-field]").length'), 10);
    assert.equal(await evaluate('document.querySelector("#modal").innerHTML'), '', 'Uses existing full-screen card, not a modal editor');
    assert.equal(await evaluate('document.querySelectorAll("#pcOverlay .pc-panel").length'), 4);
    assert(!await evaluate('!!document.querySelector("#pcEdit, #pcHistory, [data-pc-del]")'));
    assert(!await evaluate('/Доступ к устройству|Номер SIM|Подгруппы и план|Неисправность/.test(document.querySelector("#pcOverlay").innerText)'));
    const values = {
      'Заводской номер': 'SN-123', 'Наименование в бухгалтерском учете': 'Камера М-4, 1120 км',
      'Инвентарный номер': '0000042', 'Наименование в паспорте': 'Камера контроля',
      'Балансовая стоимость': '12345678901234567890,12', 'Местонахождение': 'Краснодарский край',
      'Год выпуска': '2020', 'Дата ввода в эксплуатацию': '2024-01-02',
      'Контракт': 'Контракт №1', 'Ремонтные работы': 'Плановое обслуживание',
    };
    for (const [name, value] of Object.entries(values)) await type(field(name), value);
    await evaluate('document.querySelector("#pcCategoryFields").requestSubmit()');
    await until('document.querySelector("#pcFieldsStatus")?.textContent === "Сохранено"');
    assert.equal(await evaluate(`document.querySelector(${JSON.stringify(field('Балансовая стоимость'))}).value`), '12345678901234567890.12');
    assert.equal(await evaluate(`document.querySelector(${JSON.stringify(field('Год выпуска'))}).value`), '2020');
    await click('#pcClose'); await until('!document.querySelector("#pcOverlay")');
    await click('[data-brick]'); await until('!!document.querySelector("#pcCategoryFields")');
    for (const [name, value] of Object.entries(values)) assert.equal(await evaluate(`document.querySelector(${JSON.stringify(field(name))}).value`), name === 'Балансовая стоимость' ? value.replace(',', '.') : value);
    await click('#pcClose'); await click('[data-acc-add]');
    await type('#accName', 'Камера М-4, 1120 км'); await type('#accInventory', '0000042');
    await evaluate('document.querySelector("#accFind").requestSubmit()');
    await until('document.querySelectorAll("[data-candidate]").length === 1');
    await click('[data-candidate]'); await until('!document.querySelector("#accAddSave").disabled');
    await click('#accAddSave'); await until('!document.querySelector("#accFind")');
    const existing = await (await fetch(base + '/api/accounting/point?point_id=B')).json();
    assert.equal(existing.accounting_fields.filter((f) => f.value).length, 10, 'Adding an existing accounting card preserves the other fields');
    assert.equal((await (await fetch(base + '/__test/count')).json()).count, 3);
    await evaluate('Promise.all([openPointCard("A"), openPointCard("B")])');
    await until('document.querySelector("#pcTop")?.textContent.includes("АПК B")');
    assert.equal(await evaluate('document.querySelectorAll("#pcOverlay").length'), 1, 'Rapid opens leave only the latest card');
    await fetch(base + '/__test/conflict', { method: 'POST' });
    await type(field('Контракт'), 'Мой несохранённый ввод'); await evaluate('document.querySelector("#pcCategoryFields").requestSubmit()');
    await until('!document.querySelector("#pcFieldsConflict").classList.contains("hidden")');
    assert.equal(await evaluate(`document.querySelector(${JSON.stringify(field('Контракт'))}).value`), 'Мой несохранённый ввод');
    await click('#pcCompare'); await until('document.querySelector("#pcLatest")?.textContent.includes("Изменено другим бухгалтером")');
    await click('#pcUseLatest');
    // Reused administrator file dropzone and photo dialog, including comments and viewers.
    await type('#pcFileComment', 'Бухгалтерский акт');
    await evaluate(`{ const input=document.querySelector('#pcFileInput'); const files=new DataTransfer(); files.items.add(new File(['document'], 'акт.txt', {type:'text/plain'})); input.files=files.files; input.dispatchEvent(new Event('change')); }`);
    await until('document.querySelector("#pcFileList")?.textContent.includes("акт.txt")');
    assert(await evaluate('document.querySelector("#pcFileList").textContent.includes("Бухгалтерский акт")'));
    await click('[data-pc-open]'); await until('document.querySelector("#pcvBody")?.textContent.includes("document")');
    await click('#pcvClose');
    await click('#pcPhotoAdd'); await until('!!document.querySelector("#ppUpFiles")');
    await evaluate(`{ const input=document.querySelector('#ppUpFiles'); const photos=new DataTransfer(); const bytes=Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aQ1sAAAAASUVORK5CYII='), c=>c.charCodeAt(0)); photos.items.add(new File([bytes], 'photo.png', {type:'image/png'})); input.files=photos.files; input.dispatchEvent(new Event('change')); document.querySelector('#ppUpDate').value='2024-01-01'; }`);
    await click('#ppUpGo'); await until('document.querySelector("#ppUpStatus")?.textContent === "✓ Готово"');
    assert(!await evaluate('!!document.querySelector("[data-pp-del], [data-pp-zip]")'));
    await click('.cancel'); await until('document.querySelectorAll("#pcPhotosBox img").length === 1');
    await click('#pcPhotosBox [data-pv]'); await until('!!document.querySelector("#pvBox")');
    await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
    await until('!document.querySelector("#pvBox")');
    await screenshot('accounting-card');
    await type(field('Наименование в бухгалтерском учете'), 'длинноеназвание'.repeat(30));
    await send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
    assert(await evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1 && document.querySelector("#pcOverlay").scrollWidth <= window.innerWidth + 1'), 'No mobile horizontal overflow');
    await screenshot('accounting-mobile');
    assert.equal(exceptions.length, 0, exceptions.join('\n'));
    assert(!requests.some((url) => /\/api\/(points(?:[/?]|$)|equipment|point-card|point-statuses|point-plans|work-types|apk-groups|check|visit-history)/.test(url)), 'Accounting UI must never request technical data');
    console.log('PASS /apk: two-field add, explicit selection, shared card, all 10 fields, exact money, year, conflict, file/photo uploads and viewers, mobile, no technical requests');

    await navigate('/accounting'); await until('document.querySelectorAll("[data-brick]").length === 1');
    await click('[data-acc-open]'); await until('!!document.querySelector("#pcCategoryFields")');
    await type(field('Местонахождение'), 'Из старой страницы'); await evaluate('document.querySelector("#pcCategoryFields").requestSubmit()');
    await until('document.querySelector("#pcFieldsStatus")?.textContent === "Сохранено"');
    assert.equal(exceptions.length, 0, exceptions.join('\n'));
    console.log('PASS retained /accounting: shared card, versioned save');
    await send('Emulation.setDeviceMetricsOverride', { width: 1600, height: 1100, deviceScaleFactor: 1, mobile: false });
    await evaluate(`Api.put('/api/point-category-fields/point/A', { values: { '${fields['Наименование в бухгалтерском учете']}': 'Камера для экспорта' }, expected_revision: 0 })`);
    await navigate('/apk'); await until('document.querySelectorAll("[data-acc-select]").length === 2');
    assert(await evaluate('document.querySelector("[data-acc-export]").disabled'));
    await click('[data-acc-select-all]');
    assert.equal(await evaluate('document.querySelector("[data-acc-selected-count]").textContent'), 'Выбрано: 2');
    assert(!await evaluate('!!document.querySelector("#pcOverlay")'), 'Selection never opens the card');
    await click('[data-acc-select="B"]');
    assert(await evaluate('document.querySelector("[data-acc-select-all]").indeterminate'));
    await click('[data-acc-clear]');
    assert(await evaluate('document.querySelector("[data-acc-export]").disabled'));
    await click('[data-acc-select="A"]');
    await type('#accSearch', '0000042');
    await until('document.querySelectorAll("[data-acc-select]").length === 1 && !!document.querySelector(`[data-acc-select="B"]`)');
    await click('[data-acc-select-all]');
    assert.equal(await evaluate('document.querySelector("[data-acc-selected-count]").textContent'), 'Выбрано: 2');
    await screenshot('accounting-export-selection');
    await click('[data-acc-export]');
    const downloaded = path.join(downloads, 'accounting-apk.xlsx');
    for (let i = 0; i < 100 && !fs.existsSync(downloaded); i++) await delay(100);
    assert(fs.existsSync(downloaded), 'Excel file must actually download');
    assert.equal(fs.readFileSync(downloaded).subarray(0, 2).toString(), 'PK');
    assert.deepEqual(exports[0].point_ids.sort(), ['A', 'B']);
    assert(!requests.some((url) => /\/api\/(points(?:[/?]|$)|equipment|point-card|point-statuses|point-plans|work-types|apk-groups|check|visit-history)/.test(url)), 'Export must use only accounting data');
    await click('[data-acc-select-all]');
    assert.equal(await evaluate('document.querySelector("[data-acc-selected-count]").textContent'), 'Выбрано: 1');
    await type('#accSearch', ''); await until('document.querySelectorAll("[data-acc-select]").length === 2');
    assert(await evaluate('document.querySelector(`[data-acc-select="A"]`).checked'));
    assert(!await evaluate('document.querySelector(`[data-acc-select="B"]`).checked'));
    console.log('PASS Excel: selection, select all, clear, search persistence, actual XLSX download with exact selected IDs');
    await fetch(base + '/__test/role?key=user');
    await navigate('/apk'); await until('!!document.querySelector("#apkSortFilter") && document.querySelectorAll("[data-brick]").length === 3');
    assert(await evaluate('!!document.querySelector("#apkCertificateFilter")'));
    await evaluate('openPointCard("B")'); await until('!!document.querySelector("#pcCategoryFields")');
    assert.equal(await evaluate(`document.querySelector('#pcCategoryFields [data-category-field="${fields['Контракт']}"]').disabled`), true);
    assert(await evaluate('document.querySelector("#pcCategoryFields").textContent.includes("Балансовая стоимость")'));
    console.log('PASS operator /apk: original list/filters/card; accounting values read-only');
    await fetch(base + '/__test/role?key=admin');
    await navigate('/apk'); await until('!!document.querySelector("#apkSortFilter")');
    await screenshot('admin-list');
    await evaluate('openPointCard("B")'); await until('!!document.querySelector("#pcCategoryFields")');
    assert.equal(await evaluate(`document.querySelector('#pcCategoryFields [data-category-field="${fields['Контракт']}"]').disabled`), false);
    assert(await evaluate('!!document.querySelector("#pcEdit, #pcHistory")'));
    await type(field('Контракт'), 'Изменено администратором'); await evaluate('document.querySelector("#pcCategoryFields").requestSubmit()');
    await until('document.querySelector("#pcFieldsStatus")?.textContent === "Сохранено"');
    await screenshot('admin-card');
    await click('#pcEdit'); await until('!!document.querySelector("#pcForm")');
    await type('#pcForm [name="address"]', 'Адрес после проверки администратора');
    await click('#pcSave'); await until('!!document.querySelector("#pcEdit")');
    assert(await evaluate('document.querySelector("#pcLeft").textContent.includes("Адрес после проверки администратора")'));

    assert.equal(exceptions.length, 0, exceptions.join('\n'));
    console.log('PASS administrator /apk: accounting edit remains enabled');
    await send('Browser.close', {}, null).catch(() => {});
  } finally {
    try { await fetch(base + '/__test/stop', { method: 'POST' }); } catch {}
    for (let i=0; i<50 && server.exitCode === null; i++) await delay(100);
    if (server.exitCode === null) server.kill();
    if (browser && browser.exitCode === null) browser.kill();
    // Only remove the exact temporary profile created above, inside the OS temp root.
    if (path.dirname(path.resolve(profile)) === tempRoot && path.basename(profile).startsWith('codd-accounting-browser-')) {
      for (let i=0; i<10; i++) { try { fs.rmSync(profile, {recursive:true, force:true}); break; } catch { await delay(200); } }
    }
  }
})().catch((e) => { console.error(e); process.exitCode = 1; });
