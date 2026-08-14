/* Дымовой прогон фронта без браузера.
 *
 * Берёт <script> из templates/navigator.html, подсовывает ему заглушки DOM и
 * реальный ответ /api/navigator, после чего прогоняет все экраны и модалки.
 * Ловит то, ради чего иначе пришлось бы кликать руками: исключения в рендере,
 * пустые обязательные строки, потерянные обработчики.
 *
 * Данные берутся из ответа API — сначала сохраните его:
 *   curl -u admin:PASSWORD http://127.0.0.1:8000/api/navigator -o nav_api.json
 *   node smoke_navigator.js nav_api.json
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const html = fs.readFileSync(path.join(__dirname, 'templates', 'navigator.html'), 'utf8');
const code = html.split('<script>')[1].split('</script>')[0];
const payload = JSON.parse(fs.readFileSync(process.argv[2] || 'nav_api.json', 'utf8'));

const appNode = { innerHTML: '', dataset: {} };
const emptyList = [];
const listeners = {};
const sandbox = {
  console,
  setTimeout, clearTimeout,
  localStorage: { getItem: () => null, setItem: () => {} },
  navigator: { clipboard: { writeText: () => {} } },
  document: {
    getElementById: (id) => (id === 'app' ? appNode : null),
    querySelectorAll: () => emptyList,
    querySelector: () => null,
    addEventListener: (type, fn) => { listeners[type] = fn; },
    activeElement: null
  },
  fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve(payload) })
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: 'navigator.html' });

// let/const верхнего уровня живут в лексической области контекста, а не
// свойствами sandbox, — достаём их отдельным выражением.
const api = vm.runInContext(
  '({ S, DATA, view, viewModals, pass, sorted, issues, computeExceptions,'
  + ' computeOutreach, candidateText, nearNames, deadLinks })',
  sandbox
);
Object.assign(sandbox, api);

const S = api.S;
const DATA = api.DATA;
Object.assign(DATA, payload);
S.loading = false;

const problems = [];
function check(name, fn) {
  try {
    const out = fn();
    if (typeof out === 'string' && /undefined|NaN|\[object Object\]/.test(out)) {
      const at = out.match(/.{0,60}(undefined|NaN|\[object Object\]).{0,60}/);
      problems.push(name + ' → мусор в разметке: ' + (at ? at[0].replace(/\s+/g, ' ') : ''));
    }
    return out;
  } catch (err) {
    problems.push(name + ' → ИСКЛЮЧЕНИЕ: ' + err.message + '\n' + String(err.stack).split('\n')[1]);
    return '';
  }
}

/* --- экраны --- */
S.screen = 'search';
const search = check('экран подбора', () => sandbox.view());
S.screen = 'norm';
['exc', 'req', 'rate', 'src'].forEach((tab) => {
  S.adminTab = tab;
  check('админ: ' + tab, () => sandbox.view());
});
S.screen = 'search';

/* --- модалки на каждой позиции --- */
DATA.rows.forEach((r) => {
  S.detail = r.id; check('Подробнее ' + r.id, () => sandbox.viewModals()); S.detail = null;
  S.modal = r.id; check('Текст кандидату ' + r.id, () => sandbox.viewModals()); S.modal = null;
  S.gaps = r.id; check('Пробелы ' + r.id, () => sandbox.viewModals()); S.gaps = null;
});
S.authOpen = true; check('Модалка входа', () => sandbox.viewModals()); S.authOpen = false;
if (DATA.cps.length) {
  S.cpEdit = Object.assign({}, DATA.cps[0]);
  check('Настройки контрагента', () => sandbox.viewModals());
  S.cpEdit = null;
  S.cpDelete = DATA.cps[0];
  check('Удаление контрагента', () => sandbox.viewModals());
  S.cpDelete = null;
}

/* --- фильтры и сортировки --- */
const counts = {};
['rate', 'rec', 'need', 'fresh', 'dist'].forEach((sort) => {
  S.sort = sort;
  counts['sort:' + sort] = check('сортировка ' + sort, () => sandbox.sorted(DATA.rows.filter(sandbox.pass)).length);
});
S.sort = 'rate';
const filters = [
  ['fCit', 'РФ'], ['fCit', 'РФ + РБ'], ['fCit', 'ЕАЭС'],
  ['fHouse', 'yes'], ['fHouse', 'free'], ['fHouse', 'no'],
  ['fMeals', 'yes'], ['fMeals', 'free'], ['fMeals', 'no'],
  ['fGender', 'мужчины'], ['fGender', 'женщины'], ['fGender', 'пары'],
  ['fMed', 'no'], ['fMed', 'arranged'], ['fSb', 'none'], ['fSb', 'notfull'],
  ['fRec', 'set'], ['fRec', 'none'], ['fRec', 'expired'],
  ['rateMin', '3500'], ['ageCand', '45'], ['q', 'комплектовщик'], ['scope', 'gaps']
];
filters.forEach(([key, value]) => {
  const before = S[key];
  S[key] = value;
  counts[key + '=' + value] = check('фильтр ' + key + '=' + value, () => DATA.rows.filter(sandbox.pass).length);
  S[key] = before;
});
(DATA.schedules || []).forEach((sched) => {
  S.fSched = sched;
  counts['fSched=' + sched] = DATA.rows.filter(sandbox.pass).length;
  S.fSched = '';
});

/* --- города и радиус --- */
const withCoords = DATA.cities.filter((c) => typeof c.lat === 'number');
S.sel = ['Москва']; S.radius = 50;
const near50 = check('радиус 50 от Москвы', () => sandbox.nearNames());
S.radius = 100;
const near100 = check('радиус 100 от Москвы', () => sandbox.nearNames());
S.sel = []; S.radius = 0;

/* --- тексты кандидату --- */
const texts = DATA.rows.map((r) => sandbox.candidateText(r, {}));
const leaked = [];
DATA.rows.forEach((r, i) => {
  const t = texts[i].text;
  [r.cp, r.obj, r.src].filter(Boolean).forEach((secret) => {
    if (t.toLowerCase().includes(String(secret).toLowerCase())) leaked.push(r.id + ' → «' + secret + '»');
  });
  if (/undefined|NaN/.test(t)) problems.push('текст кандидату ' + r.id + ': ' + t.split('\n')[0]);
});

console.log('=== ДЫМОВОЙ ПРОГОН ===');
console.log('позиций:', DATA.rows.length, '| городов:', DATA.cities.length, '| с координатами:', withCoords.length);
console.log('с пробелами:', DATA.rows.filter((r) => sandbox.issues(r).length).length,
  '| всего пробелов:', DATA.rows.reduce((n, r) => n + sandbox.issues(r).length, 0));
console.log('исключений:', sandbox.computeExceptions().length, '| запросов заказчикам:', sandbox.computeOutreach().length);
const withMedia = DATA.rows.filter((r) => (r.media || []).length);
console.log('с материалами (кнопка «Фото объекта»):', withMedia.length,
  '| битых ссылок:', sandbox.deadLinks ? sandbox.deadLinks().length : 0);
console.log('соседние города 50 км от Москвы:', near50.join(', ') || '—');
console.log('соседние города 100 км от Москвы:', near100.join(', ') || '—');
console.log('счётчики фильтров:', JSON.stringify(counts, null, 0));
console.log('утечки контрагента/объекта в текст кандидату:', leaked.length ? leaked.join('; ') : 'нет');
console.log('размер разметки экрана подбора:', search.length, 'символов');
console.log('\n--- пример текста кандидату ---\n' + texts[0].text);
console.log('\n--- проблемы ---');
console.log(problems.length ? problems.join('\n') : 'нет');
process.exit(problems.length ? 1 : 0);
