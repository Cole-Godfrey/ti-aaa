const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/tiaaa/dashboard/static/app.js', 'utf8');
const refresh = source.slice(source.indexOf('async function refreshCodexAuth()'), source.indexOf('function renderClaudeAuth'));
function setup(api) {
  const elements = Object.fromEntries(['refreshCodex', 'codexAuthState', 'onboardCodexState'].map(id => [id, { disabled: false, textContent: '' }]));
  let timeout;
  const state = { codexAuth: { logged_in: true } };
  const context = vm.createContext({ state, api, AbortController, element: id => elements[id],
    setTimeout: fn => { timeout = fn; return 1; }, clearTimeout: () => {} });
  vm.runInContext(refresh, context);
  return { elements, state, run: () => context.refreshCodexAuth(), timeout: () => timeout() };
}
test('Docker login instruction replaces the loading state', async () => {
  const ui = setup(async () => ({ installed: true, logged_in: false, login_command: 'docker exec -it tiaaa codex login --device-auth' }));
  await ui.run();
  assert.match(ui.elements.codexAuthState.textContent, /docker exec -it tiaaa/);
  assert.equal(ui.elements.codexAuthState.textContent, ui.elements.onboardCodexState.textContent);
  assert.equal(ui.elements.refreshCodex.disabled, false);
});
test('failed HTTP request clears checking and a stale connected state', async () => {
  const ui = setup(async () => { throw new Error('503'); });
  await ui.run();
  assert.match(ui.elements.codexAuthState.textContent, /Could not check/);
  assert.equal(ui.state.codexAuth.logged_in, false);
  assert.equal(ui.elements.refreshCodex.disabled, false);
});
test('a hung check is aborted and refresh becomes usable again', async () => {
  let requests = 0;
  const ui = setup((path, options) => {
    requests++;
    return new Promise((resolve, reject) => options.signal.addEventListener('abort', () => reject(Object.assign(new Error(), { name: 'AbortError' }))));
  });
  const pending = ui.run();
  await ui.run();
  assert.equal(requests, 1);
  assert.equal(ui.elements.refreshCodex.disabled, true);
  ui.timeout();
  await pending;
  assert.match(ui.elements.codexAuthState.textContent, /timed out/);
  assert.equal(ui.elements.refreshCodex.disabled, false);
});
test('invalid response cannot leave checking displayed', async () => {
  const ui = setup(async () => ({}));
  await ui.run();
  assert.match(ui.elements.codexAuthState.textContent, /Could not check/);
});
test('connected and missing CLI statuses render accurately', async () => {
  for (const installed of [true, false]) {
    const ui = setup(async () => ({ installed, logged_in: installed }));
    await ui.run();
    assert.match(ui.elements.codexAuthState.textContent, installed ? /Codex connected/ : /not installed/);
  }
});
