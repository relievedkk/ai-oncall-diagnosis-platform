const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const childProcess = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

test('every local SRI declaration matches the served asset bytes', () => {
  const root = path.resolve(__dirname, '..');
  const html = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
  const declarations = [...html.matchAll(
    /<(?:script|link)\b[^>]*(?:src|href)="(\/static\/[^"]+)"[^>]*integrity="sha384-([^"]+)"[^>]*>/g,
  )];

  assert.ok(declarations.length >= 4, 'expected integrity declarations for vendored assets');
  for (const [, url, expected] of declarations) {
    const file = path.join(root, url.replace(/^\//, ''));
    const relative = path.relative(root, file).split(path.sep).join('/');
    const worktreeBytes = fs.readFileSync(file);
    const artifactBytes = childProcess.execFileSync(
      'git', ['show', `:${relative}`], { cwd: root, encoding: 'buffer' },
    );
    assert.deepEqual(worktreeBytes, artifactBytes, `${url} differs from the Git artifact bytes`);
    const actual = crypto.createHash('sha384').update(artifactBytes).digest('base64');
    assert.equal(actual, expected, `integrity mismatch for ${url}`);
  }
});
