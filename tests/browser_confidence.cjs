const assert = require('node:assert/strict');
const {mkdirSync} = require('node:fs');
const {join} = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const artifacts = process.env.BROWSER_ARTIFACTS || '/tmp/erc8004-confidence-browser';
mkdirSync(artifacts, {recursive: true});

(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.BROWSER_EXECUTABLE});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1100}, reducedMotion: 'reduce'});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const base = (process.env.PREVIEW_URL || 'http://127.0.0.1:8001/').replace(/\/?$/, '/');
    await page.goto(base);
    await page.waitForSelector('#scoreValue');
    assert.equal(await page.locator('#scoreValue').innerText(), '87.6');
    assert.match(await page.locator('#assessment').innerText(), /30 source groups/);
    assert.match(await page.locator('#assessment').innerText(), /Payment matches do not change the score/);
    assert.equal(await page.locator('a:has-text("Technical report")').first().getAttribute('target'), '_blank');
    await page.locator('#evidenceDetails summary').click();
    await page.waitForSelector('#evidenceContent table');
    assert.equal(await page.locator('#evidenceContent tbody tr').count(), 30);
    await page.locator('#evidenceDetails summary').click();
    await page.screenshot({path: join(artifacts, 'desktop.png'), fullPage: true});
    await page.locator('[data-agent="47215"]').click();
    assert.equal(await page.locator('#scoreValue').innerText(), '16');
    await page.locator('#evidenceDetails summary').click();
    await page.waitForSelector('#evidenceContent table');
    assert.match(await page.locator('#evidenceContent').innerText(), /0\.02/);
    await page.locator('[data-agent="25975"]').click();
    assert.equal(await page.locator('#scoreValue').innerText(), '0');
    assert.match(await page.locator('#assessment').innerText(), /305,509/);
    assert.match(await page.locator('#assessment').innerText(), /no usable starred/);
    for (const [query, expected] of [['51120', '65.5'], ['Surf AI', '87.6']]) {
      await page.locator('#query').fill(query);
      await page.locator('#query').press('Enter');
      if (query === 'Surf AI') await page.locator('[data-hit="51085"]').click();
      await page.waitForFunction(value => document.querySelector('#scoreValue')?.textContent === value, expected);
    }
    await page.locator('#query').fill('0x0000000000000000000000000000000000000000');
    await page.locator('#query').press('Enter');
    await page.waitForSelector('#assessment h2:has-text("No matching agent")');
    assert.equal(await page.locator('#scoreValue').count(), 0);
    const address = await page.evaluate(async () => {
      const s = await fetch('./static/data/snapshot.json').then(r => r.json());
      return s.examples.find(row => row[0] === 51085)[2][0];
    });
    await page.locator('#query').fill(address.toUpperCase());
    await page.locator('#query').press('Enter');
    await page.waitForFunction(() => !document.querySelector('#searchStatus').textContent.includes('Searching'));
    assert.doesNotMatch(await page.locator('#searchStatus').innerText(), /does not identify/);
    await page.goto(base + '?agent=51120');
    await page.waitForFunction(() => document.querySelector('#scoreValue')?.textContent === '65.5');
    for (const width of [1440, 768, 390, 320]) {
      await page.setViewportSize({width, height: 1000});
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `horizontal overflow at ${width}`);
      await page.screenshot({path: join(artifacts, `landing-${width}.png`), fullPage: true});
    }
    await page.goto(base + 'technical-report/');
    await page.waitForSelector('#experimentControls:not([hidden])');
    await page.locator('#experimentCase').selectOption('free_reviews');
    assert.match(await page.locator('#experimentCaption').innerText(), /Failure/);
    assert.match(await page.locator('#experimentBars').innerText(), /99\.50/);
    assert.equal(await page.locator('.header a[href="../"]').count(), 2);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({path: join(artifacts, 'report-mobile.png'), fullPage: true});
    assert.deepEqual(errors, []);
    console.log('Browser checks passed: examples, scoring evidence, names/IDs/addresses, unknown addresses, deep links, report, and responsive layouts.');
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exit(1);});
