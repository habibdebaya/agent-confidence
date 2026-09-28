const assert = require('node:assert/strict');
const {mkdirSync} = require('node:fs');
const {join} = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const artifacts = process.env.BROWSER_ARTIFACTS || '/tmp/erc8004-reviewer-map-browser';
mkdirSync(artifacts, {recursive: true});

(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.BROWSER_EXECUTABLE});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1100}, reducedMotion: 'reduce'});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const base = (process.env.PREVIEW_URL || 'http://127.0.0.1:8765/').replace(/\/?$/, '/');
    await page.goto(base);
    await page.waitForSelector('#topList li');
    const meta = await page.evaluate(() => fetch('./static/data/meta.json').then(r => r.json()));
    assert.equal(await page.locator('#topList li').count(), meta.top.length);
    assert.equal(await page.locator('#labelRules li').count(), meta.labels.length);
    assert.equal(await page.locator('#facts div').count(), 4);
    assert.equal(await page.locator('#agent').innerText(), '');
    await page.screenshot({path: join(artifacts, 'landing.png'), fullPage: true});
    const theme = () => page.evaluate(() => document.documentElement.dataset.theme || '');
    const before = await theme();
    await page.locator('#themeToggle').click();
    assert.notEqual(await theme(), before);
    await page.screenshot({path: join(artifacts, 'landing-toggled.png')});
    await page.locator('#themeToggle').click();
    assert.equal(await theme(), before);

    await page.locator('#topList a').nth(1).click();
    await page.waitForSelector('.agent .contrast');
    assert.match(new URL(page.url()).search, new RegExp(`agent=${meta.top[1][0]}`));
    assert.match(await page.locator('.contrast').innerText(), /independent wallet/);
    assert.ok(await page.locator('.breakdown .stack .seg').count() > 0);
    await page.locator('.source summary').first().click();
    await page.waitForSelector('.src-body tbody tr');
    assert.ok(await page.locator('.src-body a[href^="https://basescan.org/tx/"]').count() > 0);
    await page.screenshot({path: join(artifacts, 'agent.png'), fullPage: true});

    await page.locator('#chips button[data-agent="19506"]').click();
    await page.waitForFunction(() => document.querySelector('.eyebrow')?.textContent.includes('#19,506'));
    assert.match(await page.locator('.verdict').innerText(), /the owner funded/);

    await page.locator('#chips button[data-agent="25975"]').click();
    await page.waitForFunction(() => document.querySelector('.eyebrow')?.textContent.includes('#25,975'));
    assert.match(await page.locator('.contrast').innerText(), /305,509/);

    await page.locator('#query').fill('Clawdia');
    await page.waitForSelector('#results li');
    assert.match(await page.locator('#results').innerText(), /#2290/);
    await page.locator('#query').press('Enter');
    await page.waitForFunction(() => document.querySelector('.eyebrow')?.textContent.includes('#2,290'));

    const reviewer = await page.evaluate(() => fetch('./static/data/reviewers.json').then(r => r.json()).then(r => r.records[0][0]));
    await page.locator('#query').fill(reviewer.toUpperCase());
    await page.waitForFunction(() => document.querySelector('#results').textContent.includes('reviewed by this wallet'));

    await page.locator('#query').fill('0x0000000000000000000000000000000000000000');
    await page.waitForFunction(() => document.querySelector('#searchStatus').textContent.includes('No match on Base'));

    await page.goBack();
    await page.goto(base + '?agent=999999999');
    await page.waitForSelector('.agent .empty');
    assert.match(await page.locator('.agent').innerText(), /No agent #999999999/);

    await page.goto(base + '?agent=2290');
    await page.waitForFunction(() => document.querySelector('.eyebrow')?.textContent.includes('#2,290'));
    for (const width of [1440, 768, 390, 320]) {
      await page.setViewportSize({width, height: 1000});
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `horizontal overflow at ${width}`);
      await page.screenshot({path: join(artifacts, `agent-${width}.png`), fullPage: true});
    }
    assert.deepEqual(errors, []);
    console.log('Browser checks passed: landing lists, agent labels and records, owner and sweeper cases, search, unknown agents, deep links, and responsive layouts.');
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exit(1);});
