#!/usr/bin/env node
// Проверяет, что пакет самодостаточен: каждая локальная ссылка разрешается,
// и что в коммит не попали секреты.
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const pages = ['index.html', 'navigator.html'];
const refRe = /(?:src|href)="(?!https?:|#|data:|mailto:)([^"{}]+)"/g;

let errors = 0;
const fail = (m) => { console.error('  ✗ ' + m); errors++; };

for (const page of pages) {
  const file = path.join(root, page);
  if (!fs.existsSync(file)) { fail(page + ' отсутствует'); continue; }
  const html = fs.readFileSync(file, 'utf8');
  const seen = new Set();
  let m;
  while ((m = refRe.exec(html))) {
    const ref = m[1].replace(/^\.\//, '').split('?')[0];
    if (!ref || seen.has(ref)) continue;
    seen.add(ref);
    if (!fs.existsSync(path.join(root, ref))) fail(page + ' → ' + ref + ' не найден');
  }
  console.log('  ✓ ' + page + ': ссылок проверено ' + seen.size);
}

const secretRe = /(bot[0-9]{6,}:[A-Za-z0-9_-]{30,}|sk-[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)/;
const walk = (dir) => {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === 'node_modules' || entry.name === '.git') continue;
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) { walk(p); continue; }
    if (!/\.(html|js|json|md|example)$/.test(entry.name)) continue;
    if (secretRe.test(fs.readFileSync(p, 'utf8'))) fail('возможный секрет в ' + path.relative(root, p));
  }
};
walk(root);
if (fs.existsSync(path.join(root, '.env'))) fail('.env лежит в пакете — он не должен коммититься');

if (errors) { console.error('\nПроверка не пройдена: ' + errors); process.exit(1); }
console.log('\nПроверка пройдена: пакет самодостаточен, секретов нет.');
