'use strict';

const BASESCAN = 'https://basescan.org';
const PAGE = 25;
const ROWS = 25;
const NAMES = {paid_first: 'Paying customer', independent: 'Independent', via_service: 'Exchange or bridge', linked: 'Linked by funding', sweeper: 'Mass reviewer', owner_linked: 'Linked to owner'};
const TITLE = 'Tracing ERC-8004 Reviews';
const KINDS = {exchange: 'exchange', bridge: 'bridge', relayer: 'relayer', paymaster: 'paymaster', faucet: 'faucet', wallet_infra: 'wallet service', platform: 'platform', busy: 'busy address'};
const SERVICE = new Set(Object.keys(KINDS));
const an = word => (/^[aeiou]/.test(word) ? 'an ' : 'a ') + word;
const state = {meta: null, full: null, loading: null, shards: new Map(), current: null, token: 0, matches: [], active: -1, hinted: null};

const $ = selector => document.querySelector(selector);
const fmt = value => Number(value).toLocaleString('en-US');
const short = value => value ? value.slice(0, 6) + '…' + value.slice(-4) : '';
const plural = (count, one, many = one + 's') => `${fmt(count)} ${count === 1 ? one : many}`;
const num = value => (Math.round(value * 10) / 10).toLocaleString('en-US', {maximumFractionDigits: 1});
const pct = share => share > 0 && share < 0.001 ? '<0.1%' : `${(share * 100).toFixed(share > 0 && share < 0.1 ? 1 : 0)}%`;
const day = time => new Date(time * 1000).toISOString().slice(0, 10);
const record = (columns, values) => Object.fromEntries(columns.map((column, index) => [column, values[index]]));
const come = count => count === 1 ? 'comes' : 'come';

function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const scan = (kind, value, label) => h('a', {
  href: `${BASESCAN}/${kind}/${value}`, target: '_blank', rel: 'noopener', class: 'mono', title: value,
}, label || short(value));

async function json(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${path} ${response.status}`);
  return response.json();
}

function rules() {
  return {
    independent: 'Funded by its own wallet, with no link to other reviewers',
    via_service: "Funded through an exchange or bridge, so we can't see who is behind it",
    paid_first: 'Paid the agent before reviewing',
    linked: 'Connected to other reviewer wallets through where its first funds came from',
    sweeper: `Reviewed ${fmt(state.meta.thresholds.sweeper_agents)} or more agents`,
    owner_linked: "Connected to the agent's owner",
  };
}

function unhint() {
  state.hinted = null;
  $('#tip').hidden = true;
}

function hint(node, lines) {
  const tip = $('#tip');
  const show = () => {
    state.hinted = node;
    tip.replaceChildren(h('strong', {text: lines[0]}), ...lines.slice(1).map(line => h('span', {text: line})));
    tip.hidden = false;
    const box = node.getBoundingClientRect();
    const left = Math.min(Math.max(8, box.left + box.width / 2 - tip.offsetWidth / 2), innerWidth - tip.offsetWidth - 8);
    const top = box.top - tip.offsetHeight - 8;
    tip.style.left = `${left}px`;
    tip.style.top = `${top < 8 ? box.bottom + 8 : top}px`;
  };
  for (const type of ['mouseenter', 'focus']) node.addEventListener(type, show);
  for (const type of ['mouseleave', 'blur']) node.addEventListener(type, unhint);
  return node;
}

function bindHints() {
  addEventListener('scroll', unhint, {capture: true, passive: true});
  for (const type of ['resize', 'blur', 'pointerdown']) addEventListener(type, unhint);
  document.addEventListener('visibilitychange', unhint);
  document.addEventListener('mouseout', event => { if (!event.relatedTarget) unhint(); });
  document.addEventListener('mouseover', event => { if (state.hinted && !state.hinted.contains(event.target)) unhint(); });
}

const swatch = label => h('span', {class: `swatch l-${label}`, 'aria-hidden': 'true'});
const chip = (label, text = NAMES[label]) => h('span', {class: 'label-chip'}, swatch(label), text);

function stack(counts, total, size = 'sm') {
  const labels = state.meta.labels.filter(label => counts[label]);
  const bar = h('div', {class: `stack stack-${size}`, role: 'img',
    'aria-label': labels.map(label => `${NAMES[label]} ${fmt(counts[label])}`).join(', ')});
  const text = rules();
  for (const label of labels) {
    bar.append(hint(h('span', {class: `seg l-${label}`, style: `flex-grow:${counts[label]}`}),
      [`${NAMES[label]} ${pct(counts[label] / total)}`, `${plural(counts[label], 'review')}`, text[label]]));
  }
  return bar;
}

function legend(target, counts, total, show = 'share') {
  const labels = state.meta.labels.filter(label => counts[label]);
  target.replaceChildren(...labels.map(label => h('li', {}, swatch(label), NAMES[label],
    h('span', {class: 'count', text: show === 'share' ? pct(counts[label] / total) : fmt(counts[label])}))));
}

function shardName(id) {
  return `feedback/${String(Math.floor(id / state.meta.thresholds.shard)).padStart(3, '0')}.json`;
}

function loadShard(name) {
  if (!state.shards.has(name)) {
    state.shards.set(name, json(`static/data/${name}`).catch(error => { state.shards.delete(name); throw error; }));
  }
  return state.shards.get(name);
}

function loadFull() {
  if (state.loading) return state.loading;
  const status = $('#searchStatus');
  status.textContent = 'Loading agents';
  state.loading = Promise.all(['agents', 'reviewers'].map(name => json(`static/data/${name}.json`)))
    .then(([agents, reviewers]) => {
      const byId = new Map(agents.records.map(row => [row[0], row]));
      const byAddress = new Map();
      const add = (address, id, note) => {
        const key = address.toLowerCase();
        if (!byAddress.has(key)) byAddress.set(key, new Map());
        if (!byAddress.get(key).has(id)) byAddress.get(key).set(id, note);
      };
      for (const row of agents.records) {
        add(row[2], row[0], 'owner');
        for (const wallet of row[3]) add(wallet, row[0], 'agent wallet');
      }
      const reviewerOf = new Map();
      const members = new Map();
      const funders = new Map(reviewers.funders.map(row => [row[0], record(reviewers.funder_columns, row)]));
      for (const row of reviewers.records) {
        reviewerOf.set(row[0], row);
        if (!members.has(row[1])) members.set(row[1], []);
        members.get(row[1]).push(row[0]);
        for (const id of row[6]) add(row[0], id, 'reviewed by this wallet');
      }
      const sourceStats = new Map(reviewers.sources.map(row => [row[0], record(reviewers.source_columns, row)]));
      state.full = {agents, byId, byAddress, reviewerOf, members, sourceStats, funders};
      status.textContent = '';
      return state.full;
    })
    .catch(error => {
      status.textContent = 'Agents could not be loaded. Refresh to try again.';
      state.loading = null;
      throw error;
    });
  return state.loading;
}

function baseLabel(source, ownerSource) {
  if (source === ownerSource) return 'owner_linked';
  const stats = state.full.sourceStats.get(source);
  if (stats.agents >= state.meta.thresholds.sweeper_agents) return 'sweeper';
  if (stats.wallets > 1) return 'linked';
  return stats.traceable ? 'independent' : 'via_service';
}

function groupPairs(pairs, ownerSource) {
  const {reviewerOf, sourceStats} = state.full;
  const groups = new Map();
  for (const pair of pairs) {
    const source = reviewerOf.get(pair.reviewer)[1];
    if (!groups.has(source)) {
      groups.set(source, {source, stats: sourceStats.get(source), base: baseLabel(source, ownerSource),
        pairs: [], records: 0, paid: 0, lowest: Infinity, highest: -Infinity});
    }
    const group = groups.get(source);
    group.pairs.push(pair);
    group.records += pair.records;
    group.paid += pair.paid_first;
    group.lowest = Math.min(group.lowest, pair.lowest);
    group.highest = Math.max(group.highest, pair.highest);
  }
  for (const group of groups.values()) group.pairs.sort((a, b) => b.records - a.records || (a.reviewer < b.reviewer ? -1 : 1));
  return [...groups.values()].sort((a, b) => b.records - a.records || b.pairs.length - a.pairs.length || (a.source < b.source ? -1 : 1));
}

async function agentView(id) {
  const full = await loadFull();
  const row = full.byId.get(id);
  if (!row) return null;
  const agent = record(full.agents.columns, row);
  if (!agent.records) return {agent, entry: null, groups: []};
  const shard = await loadShard(shardName(id));
  const entry = shard.agents[String(id)];
  const pairs = entry.pairs.map(values => {
    const pair = record(shard.columns, values);
    pair.sample = pair.sample.map(sample => record(shard.record_columns, sample));
    return pair;
  });
  return {agent, entry, groups: groupPairs(pairs, entry.owner_source)};
}

const funderOf = wallet => state.full.reviewerOf.get(wallet)[2];
const kindOf = wallet => state.full.reviewerOf.get(wallet)[4];
const linkOf = wallet => state.full.reviewerOf.get(wallet)[5];

function sameFunder(group) {
  const funders = new Set(group.pairs.map(pair => funderOf(pair.reviewer)));
  return funders.size === 1 && !funders.has('') ? [...funders][0] : null;
}

function funderName(funder, owner) {
  if (funder === owner) return 'the agent owner';
  const known = state.full.funders.get(funder);
  return known && known.name ? known.name : short(funder);
}

function funderNote(wallet) {
  const funder = funderOf(wallet);
  const kind = kindOf(wallet);
  if (!funder) return '';
  const known = state.full.funders.get(funder);
  if (SERVICE.has(kind)) return kind === 'busy' && known && known.transactions ? `busy address, ${fmt(known.transactions)} transactions` : KINDS[kind];
  const parts = [];
  const owned = [...(state.full.byAddress.get(funder) || [])].filter(([, note]) => note === 'owner').map(([id]) => id);
  if (owned.length) parts.push(owned.length === 1 ? `owner of agent #${owned[0]}` : `owner of ${fmt(owned.length)} agents`);
  if (known && known.reviewers > 1) parts.push(`first funded ${fmt(known.reviewers)} reviewer wallets`);
  if (known && known.reviewers > 1 && known.transactions) parts.push(`sent ${fmt(known.transactions)} transactions`);
  return parts.join(', ');
}

function describe(agent, groups, counts) {
  if (!agent.records) return ['No reviews yet.'];
  const lines = [];
  if (agent.reviewers === 1 && agent.records > 1) lines.push(`All ${fmt(agent.records)} come from one wallet.`);
  if (counts.owner_linked) {
    const funded = groups.filter(group => group.base === 'owner_linked').flatMap(group => group.pairs)
      .filter(pair => funderOf(pair.reviewer) === agent.owner);
    const direct = funded.reduce((sum, pair) => sum + pair.records, 0);
    lines.push(direct
      ? `${fmt(direct)} ${come(direct)} from ${plural(funded.length, 'wallet')} the owner funded.`
      : `${fmt(counts.owner_linked)} ${come(counts.owner_linked)} from wallets linked to the owner.`);
  }
  const big = groups.find(group => group.base === 'linked' && group.pairs.length > 1 && group.records / agent.records >= 0.2);
  if (big) {
    const values = big.lowest === big.highest ? `, all rating ${num(big.lowest)}` : '';
    const batch = big.pairs.every(pair => linkOf(pair.reviewer) === 'batch');
    const how = sameFunder(big) && !batch ? 'with one funder' : batch ? 'funded through the same service within a minute of each other' : 'linked to each other';
    lines.push(`${fmt(big.records)} ${come(big.records)} from ${fmt(big.pairs.length)} wallets ${how}${values}.`);
  }
  if (!big && counts.linked / agent.records >= 0.3) lines.push(`${fmt(counts.linked)} ${come(counts.linked)} from wallets linked by funding to other reviewers.`);
  if (counts.via_service / agent.records >= 0.2) lines.push(`${fmt(counts.via_service)} ${come(counts.via_service)} from wallets funded through exchanges or bridges.`);
  if (counts.sweeper) lines.push(`${fmt(counts.sweeper)} ${come(counts.sweeper)} from wallets that reviewed ${fmt(state.meta.thresholds.sweeper_agents)} or more agents.`);
  const busiest = groups.flatMap(group => group.pairs).reduce((top, pair) => pair.records > top.records ? pair : top);
  if (agent.reviewers > 1 && busiest.records >= 20 && busiest.records / agent.records >= 0.5) lines.push(`One wallet left ${fmt(busiest.records)}.`);
  if (counts.paid_first) lines.push(`${fmt(counts.paid_first)} ${counts.paid_first === 1 ? 'follows' : 'follow'} a payment to the agent.`);
  return lines.slice(0, 3);
}

function via(wallet, owner) {
  const funder = funderOf(wallet);
  const kind = kindOf(wallet);
  if (!funder) return 'No funding record';
  const name = funderName(funder, owner);
  if (!SERVICE.has(kind)) return `Funded by ${name}`;
  return name === short(funder) ? `Funded through ${an(KINDS[kind])}` : `Funded through ${name}, ${an(KINDS[kind])}`;
}

function linkText(group, owner) {
  const wallets = group.pairs.map(pair => pair.reviewer);
  const n = wallets.length;
  const reasons = {};
  for (const wallet of wallets) (reasons[linkOf(wallet)] ||= []).push(wallet);
  const parts = [];
  if (!group.stats.wallets || group.stats.wallets === 1) parts.push(via(wallets[0], owner));
  if (reasons.batch) {
    const service = sameFunder({pairs: reasons.batch.map(reviewer => ({reviewer}))});
    const where = service ? funderName(service, owner) : 'the same services';
    parts.push(`${reasons.batch.length === n ? (n === 1 ? 'Funded' : `All ${fmt(n)} funded`) : `${fmt(reasons.batch.length)} funded`} through ${where} within a minute of other reviewers here`);
  }
  if (reasons.same_funder) {
    const funder = sameFunder({pairs: reasons.same_funder.map(reviewer => ({reviewer}))});
    const count = reasons.same_funder.length === n ? (n === 1 ? '' : `All ${fmt(n)} `) : `${fmt(reasons.same_funder.length)} `;
    parts.push(funder ? `${count || 'Funded '}${count ? 'first funded ' : ''}by ${funderName(funder, owner)}`.trim() : `${count}share a first funder with other wallets here`.trim());
  }
  if (reasons.chain) parts.push(`${reasons.chain.length === n && n > 1 ? `All ${fmt(n)}` : fmt(reasons.chain.length)} funded by another wallet in this group`);
  if (reasons.other) parts.push(`${reasons.other.length === n && n > 1 ? `All ${fmt(n)}` : fmt(reasons.other.length)} linked through agent ownership, a payment loop or an earlier funder`);
  if (group.stats.agents > 1) parts.push(`Reviewed ${plural(group.stats.agents, 'agent')}`);
  return parts.join(' · ');
}

const valueRange = (lowest, highest) => lowest === highest ? `all ${num(lowest)}` : `${num(lowest)} to ${num(highest)}`;

function agentLink(id, children) {
  return h('a', {href: `?agent=${id}`, onclick: event => { event.preventDefault(); show(id, {scroll: true}); }}, children);
}

function otherAgents(group, agentId) {
  const {members, reviewerOf, byId} = state.full;
  const ids = new Set();
  for (const wallet of members.get(group.source) || []) for (const id of reviewerOf.get(wallet)[6]) if (id !== agentId) ids.add(id);
  if (!ids.size) return null;
  const list = [...ids].sort((a, b) => byId.get(b)[4] - byId.get(a)[4] || a - b).slice(0, 5);
  const rest = ids.size - list.length;
  return h('p', {class: 'also'}, 'Also reviewed ',
    ...list.flatMap((id, index) => [index ? ', ' : '', agentLink(id, byId.get(id)[1] || `#${id}`)]),
    rest ? ` and ${fmt(rest)} more` : '');
}

function table(head, rows, render) {
  const body = h('tbody', {}, rows.slice(0, ROWS).map(render));
  const wrap = h('div', {class: 'table-wrap'}, h('table', {},
    h('thead', {}, h('tr', {}, head.map(([text, cls]) => h('th', {class: cls, text})))), body));
  if (rows.length <= ROWS) return [wrap];
  const more = h('button', {class: 'btn', type: 'button', text: `Show all ${fmt(rows.length)}`});
  more.addEventListener('click', () => { body.append(...rows.slice(ROWS).map(render)); more.remove(); });
  return [wrap, more];
}

function sourceBody(group, agentId) {
  const common = group.pairs.length > 1 ? sameFunder(group) : null;
  const commonNote = common ? funderNote(group.pairs[0].reviewer) : '';
  const wallets = table([['Wallet'], ['Funded by'], ['Funding tx'], ['Reviews', 'num'], ['Values']], group.pairs, pair => {
    const [, , funder, tx] = state.full.reviewerOf.get(pair.reviewer);
    const known = funder && state.full.funders.get(funder);
    const note = common ? '' : funderNote(pair.reviewer);
    return h('tr', {},
      h('td', {}, scan('address', pair.reviewer)),
      h('td', {}, funder ? [scan('address', funder, known && known.name ? known.name : null), note ? h('span', {class: 'note', text: ` ${note}`}) : null] : 'None'),
      h('td', {}, tx ? scan('tx', tx) : ''),
      h('td', {class: 'num', text: fmt(pair.records)}),
      h('td', {text: valueRange(pair.lowest, pair.highest)}));
  });
  const rows = group.pairs.flatMap(pair => pair.sample.map(row => ({...row, wallet: pair.reviewer})))
    .sort((a, b) => b.block - a.block || b.feedback_index - a.feedback_index);
  const reviews = table([['Date'], ['Wallet'], ['Value', 'num'], ['Tags'], ['Review tx'], ['Payment']], rows, row => {
    const tags = [row.tag1, row.tag2].filter(Boolean).join(' / ');
    return h('tr', {},
      h('td', {text: day(row.time)}),
      h('td', {}, scan('address', row.wallet)),
      h('td', {class: 'num', text: num(row.value)}),
      h('td', {class: 'tags-cell', title: tags, text: tags || 'None'}),
      h('td', {}, scan('tx', row.tx)),
      h('td', {}, row.payment_tx ? scan('tx', row.payment_tx) : row.claims_payment ? h('span', {class: 'note', text: 'Claimed, not found'}) : ''));
  });
  const capped = group.pairs.some(pair => pair.records > pair.sample.length);
  return h('div', {class: 'src-body'},
    otherAgents(group, agentId),
    h('h4', {text: 'Wallets'}),
    commonNote ? h('p', {class: 'also', text: `Their funder ${funderName(common, null)}, ${commonNote}.`}) : null,
    wallets,
    h('h4', {}, 'Reviews', capped ? h('span', {class: 'note', text: ` latest ${fmt(state.meta.thresholds.sample)} per wallet`}) : null),
    reviews);
}

function sourceItem(group, agentId, owner) {
  const chips = [chip(group.base)];
  if (group.paid && (group.base === 'linked' || group.base === 'independent')) chips.push(chip('paid_first', `${fmt(group.paid)} paid`));
  const detail = linkText(group, owner);
  const details = h('details', {},
    h('summary', {},
      h('span', {class: 'src-name'}, ...chips, h('span', {class: 'src-size', text: `${plural(group.pairs.length, 'wallet')} · ${plural(group.records, 'review')}`})),
      h('span', {class: 'src-count', text: valueRange(group.lowest, group.highest)}),
      detail ? h('span', {class: 'src-link', text: detail}) : null));
  details.addEventListener('toggle', () => {
    if (details.open && !details.querySelector('.src-body')) details.append(sourceBody(group, agentId));
  });
  return h('li', {class: 'source'}, details);
}

function sourceList(groups, agentId, owner) {
  const list = h('ol', {class: 'sources'});
  const append = (from, to) => {
    for (let i = from; i < to; i++) list.append(sourceItem(groups[i], agentId, owner));
  };
  append(0, Math.min(PAGE, groups.length));
  if (groups.length <= PAGE) return [list];
  const more = h('button', {class: 'btn sources-more', type: 'button', text: `Show all ${fmt(groups.length)} sources`});
  more.addEventListener('click', () => { append(PAGE, groups.length); more.remove(); });
  return [list, more];
}

function download(view, counts) {
  const {agent, entry, groups} = view;
  const payload = {
    chain: 'Base', block: state.meta.block, method: state.meta.version,
    agent: {id: agent.id, name: agent.name, owner: agent.owner, wallets: agent.wallets, owner_source: entry ? entry.owner_source : null},
    reviews: agent.records, independent_wallets: agent.independent_wallets, labels: counts,
    sources: groups.map(group => ({
      source: group.source, label: group.base, reviews: group.records, wallets_in_source: group.stats.wallets, agents_rated: group.stats.agents,
      wallets: group.pairs.map(pair => {
        const [, , funder, tx, kind, link] = state.full.reviewerOf.get(pair.reviewer);
        const known = funder ? state.full.funders.get(funder) : null;
        return {wallet: pair.reviewer, first_funder: funder || null, funding_tx: tx || null, funder_name: known && known.name ? known.name : null,
          funder_category: kind, funder_transactions: known ? known.transactions : null, link: link || null,
          reviews: pair.records, paid_first: pair.paid_first, claims_payment: pair.claims_payment, lowest: pair.lowest, highest: pair.highest,
          latest_reviews: pair.sample};
      }),
    })),
  };
  const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], {type: 'application/json'}));
  const link = h('a', {href: url, download: `erc8004-base-agent-${agent.id}.json`});
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function renderAgent(view) {
  const {agent, entry, groups} = view;
  const counts = Object.fromEntries(state.meta.labels.map(label => [label, agent[label]]));
  const lines = describe(agent, groups, counts);
  const copy = h('button', {class: 'btn', type: 'button', text: 'Copy link'});
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(`${location.origin}${location.pathname}?agent=${agent.id}`);
      copy.textContent = 'Copied';
    } catch {
      copy.textContent = 'Copy failed';
    }
    setTimeout(() => { copy.textContent = 'Copy link'; }, 1600);
  });
  const save = h('button', {class: 'btn', type: 'button', text: 'JSON'});
  save.addEventListener('click', () => download(view, counts));
  $('#agent').replaceChildren(...[
    h('div', {class: 'agent-head'},
      h('div', {},
        h('p', {class: 'eyebrow', text: `#${fmt(agent.id)} · Owner `}, scan('address', agent.owner)),
        h('h2', {text: agent.name || 'Unnamed agent'})),
      h('div', {class: 'agent-actions'}, copy, agent.records ? save : null)),
    h('div', {class: 'contrast'},
      h('div', {}, h('strong', {text: fmt(agent.records)}), h('span', {text: agent.records === 1 ? 'review' : 'reviews'}),
        agent.records ? h('small', {text: `from ${plural(agent.reviewers, 'wallet')}`}) : null),
      h('span', {class: 'arrow', 'aria-hidden': 'true', text: '→'}),
      h('div', {class: 'backed'}, h('strong', {text: fmt(agent.independent_wallets)}),
        h('span', {text: agent.independent_wallets === 1 ? 'independent wallet' : 'independent wallets'}))),
    agent.records ? h('figure', {class: 'breakdown'}, stack(counts, agent.records, 'lg'), h('ul', {class: 'legend', 'data-agent-legend': ''})) : null,
    lines.length ? h('ul', {class: 'verdict'}, lines.map(line => h('li', {text: line}))) : null,
    groups.length ? h('div', {},
      h('h3', {class: 'group-title', text: `Where the reviews come from`}),
      sourceList(groups, agent.id, agent.owner)) : null,
  ].filter(Boolean));
  const target = $('[data-agent-legend]');
  if (target) legend(target, counts, agent.records, 'count');
  document.title = `${agent.name || `Agent #${agent.id}`} · ${TITLE}`;
  for (const button of document.querySelectorAll('#chips button')) button.setAttribute('aria-pressed', String(Number(button.dataset.agent) === agent.id));
  state.current = agent.id;
}

function clearAgent() {
  unhint();
  $('#agent').replaceChildren();
  document.body.classList.remove('has-agent');
  document.title = TITLE;
  for (const button of document.querySelectorAll('#chips button')) button.setAttribute('aria-pressed', 'false');
  state.current = null;
}

async function show(id, {push = true, scroll = false} = {}) {
  const token = ++state.token;
  unhint();
  const url = new URL(location.href);
  url.searchParams.set('agent', id);
  if (push && state.current !== id) history.pushState({agent: id}, '', url);
  document.body.classList.add('has-agent');
  if (!state.full || !state.shards.has(shardName(id))) $('#agent').replaceChildren(h('p', {class: 'empty', text: 'Loading'}));
  let view;
  try {
    view = await agentView(id);
  } catch {
    if (token === state.token) $('#agent').replaceChildren(h('p', {class: 'empty', text: 'This agent could not be loaded. Refresh to try again.'}));
    return;
  }
  if (token !== state.token) return;
  if (view) renderAgent(view);
  else $('#agent').replaceChildren(h('p', {class: 'empty', text: `No agent #${id} on Base at this snapshot.`}));
  if (scroll) {
    const top = $('#agent').getBoundingClientRect().top;
    if (top < 60 || top > innerHeight * 0.6) $('#agent').scrollIntoView({block: 'start'});
  }
}

function search(query) {
  const q = query.trim().toLowerCase();
  if (!q || !state.full) return [];
  const {agents, byId, byAddress} = state.full;
  const note = row => row[4] ? plural(row[4], 'review') : '';
  const id = q.replace(/^#/, '');
  if (/^\d+$/.test(id)) {
    const row = byId.get(Number(id));
    return row ? [[row, note(row)]] : [];
  }
  if (/^0x[0-9a-f]{40}$/.test(q)) return [...(byAddress.get(q) || [])].slice(0, 12).map(([agentId, kind]) => [byId.get(agentId), kind]);
  if (/^0x[0-9a-f]{3,}$/.test(q)) {
    const hits = [];
    for (const [address, entries] of byAddress) {
      if (!address.startsWith(q)) continue;
      for (const [agentId, kind] of entries) {
        hits.push([byId.get(agentId), `${kind} ${short(address)}`]);
        if (hits.length >= 12) break;
      }
      if (hits.length >= 12) break;
    }
    return hits;
  }
  const hits = agents.records.filter(row => row[1] && row[1].toLowerCase().includes(q));
  hits.sort((a, b) => b[4] - a[4] || a[0] - b[0]);
  return hits.slice(0, 12).map(row => [row, note(row)]);
}

function renderResults() {
  const list = $('#results');
  const input = $('#query');
  list.replaceChildren(...state.matches.map(([row, note], index) => h('li', {
    role: 'option', id: `result-${index}`, 'aria-selected': String(index === state.active),
    onmousedown: event => { event.preventDefault(); choose(index); },
  }, h('span', {class: 'rid', text: `#${row[0]}`}), h('span', {class: 'rname', text: row[1] || 'Unnamed agent'}), h('span', {class: 'rnote', text: note}))));
  list.hidden = !state.matches.length;
  input.setAttribute('aria-expanded', String(!list.hidden));
  if (state.active >= 0) input.setAttribute('aria-activedescendant', `result-${state.active}`);
  else input.removeAttribute('aria-activedescendant');
}

function choose(index) {
  const match = state.matches[index];
  if (!match) return;
  state.matches = [];
  state.active = -1;
  renderResults();
  $('#query').blur();
  show(match[0][0], {scroll: true});
}

function update() {
  const query = $('#query').value;
  state.matches = search(query);
  state.active = state.matches.length ? 0 : -1;
  renderResults();
  $('#searchStatus').textContent = query.trim() && state.full && !state.matches.length ? 'No match on Base.' : '';
}

function bindSearch() {
  const input = $('#query');
  let timer;
  input.addEventListener('focus', () => loadFull().then(update).catch(() => {}));
  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => loadFull().then(update).catch(() => {}), 80);
  });
  input.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      if (!state.matches.length) return;
      event.preventDefault();
      const step = event.key === 'ArrowDown' ? 1 : -1;
      state.active = (state.active + step + state.matches.length) % state.matches.length;
      renderResults();
      $(`#result-${state.active}`).scrollIntoView({block: 'nearest'});
    } else if (event.key === 'Escape') {
      state.matches = [];
      renderResults();
    }
  });
  input.addEventListener('blur', () => setTimeout(() => {
    if (document.activeElement === input) return;
    state.matches = [];
    renderResults();
  }, 120));
  $('#search').addEventListener('submit', async event => {
    event.preventDefault();
    await loadFull().catch(() => {});
    update();
    choose(Math.max(state.active, 0));
  });
}

function renderFacts(meta) {
  const {summary} = meta;
  const facts = [
    [fmt(summary.records), 'reviews on Base'],
    [fmt(summary.reviewers), 'wallets left those reviews'],
    [fmt(summary.sources), 'separate sources behind those wallets'],
    [fmt(meta.payments.length), 'reviews follow a payment to the agent'],
  ];
  $('#facts').replaceChildren(...facts.map(([value, label]) => h('div', {}, h('dt', {text: label}), h('dd', {text: value}))));
  $('#baseBar').replaceChildren(stack(summary.labels, summary.records, 'lg'));
  legend($('#baseLegend'), summary.labels, summary.records);
}

function renderTop(meta) {
  const labels = meta.labels;
  $('#topList').replaceChildren(...meta.top.map(([id, name, records, , , backed, ...values], index) => {
    const counts = Object.fromEntries(labels.map((label, i) => [label, values[i]]));
    return h('li', {}, agentLink(id, [
      h('span', {class: 'top-rank', text: fmt(index + 1)}),
      h('span', {class: 'top-name'}, h('strong', {text: name || `Agent #${id}`}), h('small', {text: plural(records, 'review')})),
      stack(counts, records),
      h('span', {class: 'top-share', text: fmt(backed)}),
    ]));
  }));
}

function renderRules() {
  const text = rules();
  $('#labelRules').replaceChildren(...state.meta.labels.map(label => h('li', {}, swatch(label), h('strong', {text: NAMES[label]}), h('span', {text: text[label]}))));
}

function renderMeta(meta) {
  const date = new Date(meta.timestamp).toLocaleDateString('en-GB', {day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC'});
  $('#snapshotLine').textContent = `Base snapshot at block ${fmt(meta.block)}, ${date}.`;
  const chips = $('#chips');
  for (const [id, name] of meta.examples) {
    const button = h('button', {type: 'button', 'data-agent': id, title: `Agent #${id}`, 'aria-pressed': 'false', text: name || `#${id}`});
    button.addEventListener('click', () => show(id, {scroll: true}));
    chips.append(button);
  }
}

function requested() {
  const value = new URLSearchParams(location.search).get('agent');
  return value && /^\d+$/.test(value) ? Number(value) : null;
}

async function route() {
  const id = requested();
  if (id === null) {
    state.token++;
    clearAgent();
  } else {
    await show(id, {push: false});
  }
}

function bindTheme() {
  const root = document.documentElement;
  const button = $('#themeToggle');
  const media = matchMedia('(prefers-color-scheme: dark)');
  const system = () => media.matches ? 'dark' : 'light';
  const current = () => root.dataset.theme || system();
  const label = () => button.setAttribute('aria-label', current() === 'dark' ? 'Switch to light mode' : 'Switch to dark mode');
  button.addEventListener('click', () => {
    const next = current() === 'dark' ? 'light' : 'dark';
    if (next === system()) delete root.dataset.theme;
    else root.dataset.theme = next;
    try {
      if (next === system()) localStorage.removeItem('theme');
      else localStorage.setItem('theme', next);
    } catch {}
    label();
  });
  media.addEventListener('change', label);
  label();
}

async function init() {
  bindTheme();
  bindSearch();
  bindHints();
  try {
    state.meta = await json('static/data/meta.json');
  } catch {
    $('#agent').replaceChildren(h('p', {class: 'empty', text: 'The snapshot could not be loaded. Refresh to try again.'}));
    return;
  }
  renderMeta(state.meta);
  renderFacts(state.meta);
  renderTop(state.meta);
  renderRules();
  addEventListener('popstate', route);
  await route();
}

init();
