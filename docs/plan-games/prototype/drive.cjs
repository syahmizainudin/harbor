// Drive the prototype game through title, New Game, walk, Save, reload, Continue and Load.
// usage: node drive.cjs <page port> <mode A|B>
// PLAYWRIGHT: path to a playwright package; CHROME: a Chromium binary (branded Chrome won't do).
const { chromium } = require(process.env.PLAYWRIGHT || '/app/tools/juris-attendance-bot/node_modules/playwright');
const [port, mode] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const b = await chromium.launch({
    executablePath: process.env.CHROME || process.env.HOME + '/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome',
    args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'],
  });
  const p = await b.newPage();
  const errs = [];
  p.on('pageerror', e => errs.push('page: ' + e.message));
  p.on('console', m => { if (m.type() === 'error' || m.type() === 'warning') errs.push(m.type() + ': ' + m.text()); });
  const out = {};
  const game = async () => p.frames().find(f => f.url().includes('/play/'));
  const scene = async () => (await game()).evaluate(() => SceneManager._scene && SceneManager._scene.constructor.name);
  const waitScene = async (name, ms = 20000) => {
    for (let t = 0; t < ms; t += 250) { try { if (await scene() === name) return true; } catch (e) {} await sleep(250); }
    throw new Error('never reached ' + name + ', at ' + await scene().catch(e => e.message));
  };
  const key = async (k, n = 1) => { const f = await game(); for (let i = 0; i < n; i++) { await f.press('body', k, { delay: 80 }); await sleep(300); } };
  try {
    await p.goto(`http://127.0.0.1:${port}/`);
    await p.waitForTimeout(500);
    await p.click('#g');
    await waitScene('Scene_Title');
    const g = await game();
    out.webgl = await g.evaluate(() => Graphics.isWebGL());
    out.textColor = await g.evaluate(() => SceneManager._scene._commandWindow.textColor(1));
    await p.screenshot({ path: `${__dirname}/shot-${mode}-title.png` });
    await sleep(1500);
    await g.evaluate(() => window.focus());
    out.titleIndex = await g.evaluate(() => SceneManager._scene._commandWindow.index());
    await key('Enter');                                  // New Game
    await waitScene('Scene_Map');
    await sleep(1500);
    await key('ArrowRight', 2); await sleep(800);
    out.pos = await g.evaluate(() => [$gamePlayer.x, $gamePlayer.y]);
    await p.screenshot({ path: `${__dirname}/shot-${mode}-map.png` });
    await key('Escape'); await waitScene('Scene_Menu');
    // menu: Item Skill Equip Status Formation Options Save Game End -> Save is index 6
    await sleep(800);
    out.menu = await g.evaluate(() => { const w = SceneManager._scene._commandWindow; w.selectSymbol('save'); return [w.index(), w.isCurrentItemEnabled()]; });
    await key('Enter'); await waitScene('Scene_Save'); await sleep(800);
    await key('Enter'); await sleep(1500);             // slot 1
    out.savesAfterSave = await (await fetch(`http://127.0.0.1:${port}/__log`)).json();
    // reload: a fresh page must Continue from the server save
    await p.goto(`http://127.0.0.1:${port}/`);
    await p.waitForTimeout(500);
    await p.click('#g');
    await waitScene('Scene_Title');
    await sleep(1500);
    const g2 = await game();
    await g2.evaluate(() => window.focus());
    out.continueEnabled = await g2.evaluate(() => DataManager.isAnySavefileExists());
    await g2.evaluate(() => SceneManager._scene._commandWindow.selectSymbol('continue')); await key('Enter'); await waitScene('Scene_Load'); await sleep(800);
    out.loadWindow = await g2.evaluate(() => { const w = SceneManager._scene._listWindow; return [w.index(), w.active, w.isCurrentItemEnabled(), DataManager.isThisGameFile(1)]; });
    await g2.evaluate(() => SceneManager._scene.onSavefileOk()); await waitScene('Scene_Map');
    await sleep(1000);
    out.loadedPos = await g2.evaluate(() => [$gamePlayer.x, $gamePlayer.y]);
    out.gameOrigin = await g2.evaluate(() => { try { return String(window.origin); } catch (e) { return e.message; } });
    out.gameReadsHarborState = await g2.evaluate(async (port) => {
      try { const r = await fetch(`http://127.0.0.1:${port}/__log`, { credentials: 'include' }); return 'status ' + r.status; }
      catch (e) { return 'blocked: ' + e.message; }
    }, port);
  } catch (e) {
    out.failed = e.message;
    await p.screenshot({ path: `${__dirname}/shot-${mode}-fail.png` }).catch(() => {});
  }
  out.errors = errs.slice(0, 15);
  console.log(JSON.stringify(out, null, 1));
  await b.close();
})();
