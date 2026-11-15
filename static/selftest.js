fetch('test_vectors.json')
  .then(response => {
    if (!response.ok) throw new Error('vector fetch failed');
    return response.json();
  })
  .then(vector => {
    const epsilon = 1e-6;
    const ok = Math.abs(IUE.breakEven(vector.c, vector.r, vector.p) - vector.break_even) < epsilon &&
      Math.abs(IUE.margin(vector.c, vector.r, vector.p, vector.u) - vector.margin) < epsilon &&
      Math.abs(IUE.entry(vector.c, vector.r, vector.u, vector.g) - vector.entry) < epsilon;
    document.querySelector('#result').textContent = ok ? 'PASS' : 'FAIL';
  })
  .catch(() => {
    document.querySelector('#result').textContent = 'FAIL';
  });
