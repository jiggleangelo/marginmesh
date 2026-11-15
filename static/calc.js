(() => {
  const api = {
    breakEven: (c, r, p) => c / (r * p),
    margin: (c, r, p, u) => p * r * u - c,
    entry: (c, r, u, g) => c / (r * u * (1 - g)),
    blended: (pin, pout, k) => (k * pin + pout) / (k + 1)
  };
  window.IUE = api;
  const form = document.querySelector('#calculator');
  const dataNode = document.querySelector('#iue-data');
  if (!form || !dataNode) return;
  const data = JSON.parse(dataNode.dataset.json);
  form.addEventListener('submit', event => {
    event.preventDefault();
    const fields = new FormData(form);
    const n = key => Number(fields.get(key));
    const c = n('rent'), r = n('r'), u = n('u'), g = n('g');
    const k = n('k'), ulo = n('ulo'), uhi = n('uhi');
    const valid = [c, r, u, g, k, ulo, uhi].every(Number.isFinite) &&
      c > 0 && r > 0 && u > 0 && u <= 1 && g >= 0 && g <= .95 &&
      k >= 0 && k <= 20 && ulo > 0 && ulo <= uhi && uhi <= 1;
    const output = document.querySelector('#calc-output');
    if (!valid) {
      output.textContent = 'Invalid calculator values.';
      return;
    }
    const prices = data.endpoints.map(row => api.blended(row.price_in, row.price_out, k)).sort((a, b) => a - b);
    const low = api.entry(c, r, uhi, g);
    const high = api.entry(c, r, ulo, g);
    const entryAtU = api.entry(c, r, u, g);
    const utilisation = Math.round(u * 100) + '%';
    if (!prices.length) {
      output.textContent = `Current scenario at ${utilisation} utilisation: market margins unavailable; target-margin entry price $${entryAtU.toFixed(3)}. No market price. Entry band $${low.toFixed(3)}–$${high.toFixed(3)}. Match utilisation unavailable. no market price.`;
      return;
    }
    const min = prices[0];
    const median = prices.length % 2 ? prices[(prices.length - 1) / 2] : (prices[prices.length / 2 - 1] + prices[prices.length / 2]) / 2;
    const beMin = api.breakEven(c, r, min);
    const beMed = api.breakEven(c, r, median);
    const marginMin = api.margin(c, r, min, u);
    const marginMed = api.margin(c, r, median, u);
    const match = beMin / (1 - g);
    const verdict = low < min ? 'room to undercut' : low <= median ? 'competitive at median' : 'priced out';
    const pct = value => value > 1 ? 'not viable on this chip at this price' : Math.round(value * 100) + '%';
    output.textContent = `Current scenario at ${utilisation} utilisation: margin/GPU-hour at min $${marginMin.toFixed(3)} and median $${marginMed.toFixed(3)}; target-margin entry price $${entryAtU.toFixed(3)}. Break-even at min ${pct(beMin)}; at median ${pct(beMed)}. Entry band $${low.toFixed(3)}–$${high.toFixed(3)}. Match utilisation ${pct(match)}. ${verdict}.`;
  });
})();
