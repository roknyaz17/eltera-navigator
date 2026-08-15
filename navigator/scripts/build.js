#!/usr/bin/env node
// Сборки как таковой нет — это статический пакет без бандлера.
// Скрипт прогоняет проверку и складывает отдаваемые файлы в dist/.
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const root = path.resolve(__dirname, '..');
const dist = path.join(root, 'dist');
const include = ['index.html', 'navigator.html', 'support.js', 'assets', 'eltera'];

execFileSync(process.execPath, [path.join(__dirname, 'check.js')], { stdio: 'inherit' });

fs.rmSync(dist, { recursive: true, force: true });
const copy = (from, to) => {
  const st = fs.statSync(from);
  if (st.isDirectory()) {
    fs.mkdirSync(to, { recursive: true });
    for (const e of fs.readdirSync(from)) copy(path.join(from, e), path.join(to, e));
  } else {
    fs.mkdirSync(path.dirname(to), { recursive: true });
    fs.copyFileSync(from, to);
  }
};
for (const item of include) {
  const src = path.join(root, item);
  if (fs.existsSync(src)) copy(src, path.join(dist, item));
}
console.log('\ndist/ собран: ' + include.filter((i) => fs.existsSync(path.join(root, i))).join(', '));
