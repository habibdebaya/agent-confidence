'use strict';

(async () => {
  try {
    const response = await fetch('../static/data/snapshot.json');
    if (!response.ok) return;
    const data = await response.json();
    const select = document.querySelector('#experimentCase');
    for (const row of data.experiments) {
      const option = document.createElement('option');
      option.value = row.id; option.textContent = row.name; select.append(option);
    }
    const render = () => {
      const row = data.experiments.find(item => item.id === select.value);
      const bars = document.querySelector('#experimentBars'); bars.replaceChildren();
      for (const [label, value, extra] of [['Plain mean', row.raw_mean, 'baseline'], ['Confidence index', row.score, '']]) {
        const line = document.createElement('div'); line.className = 'bar-row';
        const name = document.createElement('span'); name.textContent = label;
        const track = document.createElement('div'); track.className = 'bar-track';
        const bar = document.createElement('div'); bar.className = 'bar ' + extra; bar.style.width = value + '%';
        track.append(bar);
        const number = document.createElement('span'); number.textContent = value.toFixed(2);
        line.append(name, track, number); bars.append(line);
      }
      document.querySelector('#experimentCaption').textContent = row.interpretation;
    };
    select.value = 'linked_wallets'; select.addEventListener('change', render); render();
    document.querySelector('#experimentControls').hidden = false;
    document.querySelector('#experimentFigure').hidden = false;
  } catch {
    // The complete experiment table remains readable without scripting.
  }
})();
