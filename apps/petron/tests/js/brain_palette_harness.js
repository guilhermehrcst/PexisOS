// How brain.js chooses its palette, checked by running the real script in a Node `vm` against stubs.
//
//   node tests/js/brain_palette_harness.js [path/to/brain.js]
//
// Precedence: ?palette= (valid) > <meta name="petron-palette"> (valid) > adaptive.
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const BRAIN = process.argv[2] || path.join(__dirname, '..', '..', 'brain', 'brain.js');
const SOURCE = fs.readFileSync(BRAIN, 'utf8');

function paletteFor({ search = '', meta = null }) {
  const noop = new Proxy(function () {}, { get: () => noop, apply: () => noop, set: () => true });
  const canvas = { getContext: () => noop, getBoundingClientRect: () => ({ width: 800, height: 600 }),
    addEventListener() {}, width: 0, height: 0 };
  const query = (selector) => {
    if (selector === 'meta[name="petron-palette"]') return meta === null ? null : { content: meta };
    return null;
  };
  const window = { location: { search }, devicePixelRatio: 1, innerWidth: 800, innerHeight: 600,
    matchMedia: () => ({ matches: true, addEventListener() {} }),   // reduced motion: no animation loop
    requestAnimationFrame: () => 0, cancelAnimationFrame() {}, addEventListener() {} };
  const document = { getElementById: (id) => (id === 'brain' ? canvas : null), querySelector: query,
    addEventListener() {}, hidden: false };
  window.document = document;
  vm.runInNewContext(SOURCE, { window, document, URLSearchParams, Math, Object, Number, Array });
  return window.PetronBrain.getPalette();
}

const cases = [
  ['no declaration: the local runtime keeps the adaptive default', {}, 'adaptive'],
  ['the page declares spectrum: it is the default', { meta: 'spectrum' }, 'spectrum'],
  ['the page declares mono', { meta: 'mono' }, 'mono'],
  ['?palette= wins over the declaration', { search: '?palette=mono', meta: 'spectrum' }, 'mono'],
  ['?palette=adaptive wins over a spectrum declaration', { search: '?palette=adaptive', meta: 'spectrum' }, 'adaptive'],
  ['?palette=state is still the alias of adaptive', { search: '?palette=state', meta: 'spectrum' }, 'adaptive'],
  ['an unknown declaration falls back to adaptive', { meta: 'neon' }, 'adaptive'],
  ['an unknown ?palette= falls back to the declaration', { search: '?palette=neon', meta: 'spectrum' }, 'spectrum'],
  ['an unknown ?palette= with no declaration falls back to adaptive', { search: '?palette=neon' }, 'adaptive'],
  ['an empty declaration is ignored', { meta: '' }, 'adaptive'],
];

let failed = 0;
for (const [name, input, expected] of cases) {
  try {
    assert.equal(paletteFor(input), expected);
    console.log(`PASS ${name}`);
  } catch (error) {
    failed += 1;
    console.log(`FAIL ${name}\n     ${String(error.message).split('\n')[0]}`);
  }
}
console.log(`\n${cases.length - failed}/${cases.length} palette scenarios passed (${BRAIN})`);
process.exit(failed ? 1 : 0);
