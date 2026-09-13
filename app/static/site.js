'use strict';

const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number = value => new Intl.NumberFormat('en-US', {maximumFractionDigits: 1}).format(value);
const precise = value => Number(value.toFixed(4)).toString();
const short = value => value ? `${value.slice(0, 8)}…${value.slice(-5)}` : 'Unavailable';
const exampleNotes = {51085: '30 recorded reviewer groups', 51120: '8 recorded reviewer groups', 47215: 'One recorded payment match', 25975: 'No eligible quality ratings'};
let snapshot, cataloguePromise, evidencePromise, selected, searchVersion = 0;

async function readJSON(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`Could not load the saved data (${response.status}).`);
  return response.json();
}
function catalogue() {
  if (!cataloguePromise) cataloguePromise = readJSON('./static/data/catalogue.json').catch(error => {cataloguePromise = null; throw error;});
  return cataloguePromise;
}
function evidence() {
  if (!evidencePromise) evidencePromise = readJSON('./static/data/evidence.json').then(bundle => bundle.records.map(row => Object.fromEntries(bundle.columns.map((key, i) => [key, row[i]])))).catch(error => {evidencePromise = null; throw error;});
  return evidencePromise;
}
function resultFor(id) {
  return snapshot.scores[id] || {score: 0, support: 0, groups: 0, admitted_reviews: 0, reserve: snapshot.reserve, excluded: {}};
}
function nameOf(agent) { return agent[1] || `Agent #${agent[0]}`; }
function showMissing(message) {
  selected = null;
  $('#assessment').setAttribute('aria-busy', 'false');
  $('#assessment').innerHTML = `<p class="eyebrow">LOOKUP RESULT</p><h2>No matching agent</h2><p class="loading">${esc(message)}</p><p class="loading">The lookup covers registered agents in this saved Base snapshot. An unknown address has no assessment.</p>`;
  document.querySelectorAll('.example').forEach(button => button.setAttribute('aria-pressed', 'false'));
}
function metric(value, label) { return `<div class="metric"><strong>${number(value)}</strong><small>${label}</small></div>`; }

function selectAgent(agent, updateURL = true) {
  selected = agent[0];
  const id = agent[0], result = resultFor(id);
  const matches = snapshot.payments.filter(row => row.agent_id === id);
  const excluded = result.excluded.agent_linked_group || 0;
  const repeated = result.admitted_reviews - result.groups;
  let title = 'Limited group evidence';
  if (result.groups === 0) title = 'No qualifying support found';
  else if (result.score === 0) title = 'No positive support after grouping';
  else if (result.score >= 50) title = 'Evidence from multiple reviewer groups';
  const description = result.groups
    ? `${number(result.admitted_reviews)} admitted quality ratings reduce to ${number(result.groups)} source ${result.groups === 1 ? 'group' : 'groups'}. The calculation includes an uncertainty reserve of four sources.`
    : agent[4] === 0
      ? `${number(agent[3])} active feedback records contain no usable starred quality ratings. Other feedback types are outside this score.`
      : 'The recorded relationships place these quality ratings in groups linked to the agent’s current owner.';
  const supportNote = result.groups
    ? `${number(result.groups)} recorded groups contribute ${precise(result.support)} units of support. ${number(repeated)} additional ratings share those groups and create no additional source weight.`
    : 'No reviewer group contributes positive support under the declared evidence rules.';
  const paymentNote = matches.length
    ? `${number(matches.length)} feedback ${matches.length === 1 ? 'record has' : 'records have'} a prior direct payment match in the captured data. Transfers establish a payment relationship; successful service delivery is unverified.`
    : 'No prior direct payment match was found in the captured data. Payments through shared wallets, escrow, or other rails may not be attributable.';
  $('#assessment').setAttribute('aria-busy', 'false');
  $('#assessment').innerHTML = `
    <div class="assessment-head"><div><h2>${esc(nameOf(agent))}</h2><p class="agent-id">BASE · AGENT #${id}</p></div><span class="badge">Snapshot assessment</span></div>
    <div class="score-row"><div class="score-ring" style="--score:${result.score}"><div class="score-inner"><span class="score-number" id="scoreValue">${number(result.score)}</span><small>CONFIDENCE / 100</small></div></div><div class="score-explanation"><h3>${title}</h3><p>${description}</p></div></div>
    <div class="metrics">${metric(agent[3], 'Active feedback records')}${metric(result.admitted_reviews, 'Admitted quality ratings')}${metric(result.groups, 'Reviewer groups')}${metric(matches.length, 'Prior payment matches')}</div>
    <div class="observations"><div><h3>Rating evidence</h3><p>${supportNote}${excluded ? ` ${number(excluded)} owner-linked ratings are excluded.` : ''}</p></div><div><h3>Payment evidence and coverage</h3><p>${paymentNote}</p></div></div>
    <p class="score-caveat">The index measures recorded rating support. Payment matches do not change the score. Results depend on the accuracy of the observed reviewer groups.</p>
    <details class="evidence" id="evidenceDetails"><summary>Supporting ratings, group assignments, and payment records</summary><div class="evidence-content" id="evidenceContent"><p>Loading the supporting records…</p></div></details>`;
  document.querySelectorAll('.example').forEach(button => button.setAttribute('aria-pressed', String(Number(button.dataset.agent) === id)));
  if (updateURL) {
    const url = new URL(location.href); url.searchParams.set('agent', id);
    history.pushState({}, '', url);
  }
  $('#evidenceDetails').addEventListener('toggle', async event => {
    if (!event.target.open || event.target.dataset.loaded) return;
    event.target.dataset.loaded = 'true';
    try {
      const rows = snapshot.example_evidence[id] || (await evidence()).filter(row => row.agent_id === id);
      if (selected !== id) return;
      renderEvidence(agent, rows, matches);
    } catch (error) {
      if (selected === id) { $('#evidenceContent').innerHTML = `<p>${esc(error.message)} Close and reopen this section to retry.</p>`; event.target.dataset.loaded = ''; }
    }
  });
}

function renderEvidence(agent, rows, matches) {
  const groups = new Map();
  for (const row of rows) {
    if (row.group === row.agent_group) continue;
    const group = groups.get(row.group) || {count: 0, minimum: 101, witness: null};
    group.count++;
    if (row.rating < group.minimum) {group.minimum = row.rating; group.witness = row;}
    groups.set(row.group, group);
  }
  const ranked = [...groups].sort((a, b) => a[1].minimum - b[1].minimum || a[0].localeCompare(b[0]));
  const groupTable = ranked.length ? `<div class="scroll-table"><table><thead><tr><th>Recorded group</th><th>Ratings</th><th>Minimum /100</th><th>Source event</th></tr></thead><tbody>${ranked.slice(0, 100).map(([id, group]) => `<tr><td class="mono">${esc(short(id.replace('entity:', '')))}</td><td>${number(group.count)}</td><td>${number(group.minimum)}</td><td><a href="https://basescan.org/tx/${group.witness.feedback_tx}" target="_blank" rel="noopener">Feedback ↗</a></td></tr>`).join('')}</tbody></table></div>` : '<p>There are no admitted source groups. This result concerns evidence availability under the stated eligibility rules.</p>';
  const paymentTable = matches.length ? `<div class="scroll-table"><table><thead><tr><th>Prior transfer</th><th>USDC</th><th>Payment block</th><th>Feedback block</th></tr></thead><tbody>${matches.map(row => `<tr><td><a href="https://basescan.org/tx/${row.payment_tx}" target="_blank" rel="noopener">${esc(short(row.payment_tx))} ↗</a></td><td>${esc(row.amount_usdc)}</td><td>${number(row.payment_block)}</td><td>${number(row.feedback_block)}</td></tr>`).join('')}</tbody></table></div>` : '<p>No attributable prior payment is present in this collection for this agent’s feedback.</p>';
  $('#evidenceContent').innerHTML = `${groupTable}${ranked.length > 100 ? '<p>Showing the 100 groups with the lowest contributions. All scoring inputs are available in the evidence download.</p>' : ''}<p>Grouping is a relationship heuristic and can merge unrelated people or miss coordinated wallets. The minimum uses every admitted rating in the group.</p><h3>Payment context</h3>${paymentTable}<p><a href="./static/data/evidence.json" download>Download the scoring inputs</a> · <a href="./technical-report/#reproduce" target="_blank" rel="noopener">Reproduce the calculation ↗</a></p><p class="mono">Recorded agent addresses: ${agent[2].map(address => esc(address)).join(', ')}</p>`;
}

async function search(query, updateURL = true) {
  const generation = ++searchVersion;
  $('#searchStatus').textContent = 'Searching the saved catalogue…';
  $('#searchResults').hidden = true;
  try {
    const rows = await catalogue();
    if (generation !== searchVersion) return;
    const value = query.trim().toLowerCase();
    const numeric = /^#?\d+$/.test(value);
    const address = /^0x[0-9a-f]{40}$/.test(value);
    if (!value) { $('#searchStatus').textContent = 'Enter a name, agent ID, or address.'; return; }
    const hits = rows.filter(row => numeric ? String(row[0]) === value.replace(/^#/, '') : address ? row[2].includes(value) : row[1].toLowerCase().includes(value));
    if (!hits.length) {
      const message = address ? 'This address does not identify an agent owner or active declared wallet in this snapshot.' : `No registered agent matches “${query.trim()}” in this snapshot.`;
      $('#searchStatus').textContent = message; showMissing(message); return;
    }
    if (hits.length === 1) { $('#searchStatus').textContent = 'One matching agent.'; selectAgent(hits[0], updateURL); return; }
    $('#searchStatus').textContent = `${number(hits.length)} matching agents. Showing up to 30; select one or narrow the search.${address ? ' This address is associated with multiple agents.' : ''}`;
    const results = $('#searchResults'); results.hidden = false;
    results.innerHTML = hits.slice(0, 30).map(row => `<button class="search-hit" type="button" data-hit="${row[0]}">${esc(nameOf(row))}<small>Agent #${row[0]} · ${number(row[3])} feedback records</small></button>`).join('');
    results.querySelectorAll('button').forEach(button => button.addEventListener('click', () => selectAgent(hits.find(row => row[0] === Number(button.dataset.hit)))));
  } catch (error) { if (generation === searchVersion) $('#searchStatus').textContent = error.message + ' Please try again.'; }
}

async function start() {
  try {
    snapshot = await readJSON('./static/data/snapshot.json');
    const date = new Date(snapshot.timestamp).toISOString().slice(0, 10);
    $('#snapshotLine').textContent = `${number(snapshot.summary.agents)} registered agents · ${date} UTC · Block ${number(snapshot.block)}`;
    $('#methodVersion').textContent = `${snapshot.version} · ${number(snapshot.summary.scored_agents)} nonzero scores`;
    $('#examples').innerHTML = snapshot.examples.map(agent => `<button type="button" class="example" data-agent="${agent[0]}" aria-pressed="false"><span><strong>${esc(nameOf(agent))}</strong><small>${exampleNotes[agent[0]] || `Agent #${agent[0]}`}</small></span><span>${number(resultFor(agent[0]).score)}</span></button>`).join('');
    $('#examples').querySelectorAll('button').forEach(button => button.addEventListener('click', () => selectAgent(snapshot.examples.find(agent => agent[0] === Number(button.dataset.agent)))));
    $('#searchForm').addEventListener('submit', event => { event.preventDefault(); search($('#query').value); });
    const initial = new URLSearchParams(location.search).get('agent');
    if (initial) {
      const example = snapshot.examples.find(agent => String(agent[0]) === initial);
      if (example) selectAgent(example, false); else await search(initial, false);
    } else selectAgent(snapshot.examples[0], false);
    window.addEventListener('popstate', () => { const id = new URLSearchParams(location.search).get('agent'); if (id) search(id, false); else selectAgent(snapshot.examples[0], false); });
  } catch (error) {
    $('#assessment').setAttribute('aria-busy', 'false');
    $('#assessment').innerHTML = `<h2>The snapshot could not be loaded</h2><p>${esc(error.message)} Reload the page to retry.</p>`;
    $('#snapshotLine').textContent = 'Snapshot unavailable.';
  }
}
start();
