// Eltera — единая учётная запись сотрудника для трёх продуктов:
// Навигатор, Кабинет Eltera (результаты оценок) и CRM Shellix.
// Одна почта + один пароль. Создание и изменение записи синхронизируется
// во все три системы: они читают ЭТОТ store, своих учёток не заводят.
// Персист в localStorage + подписка + межвкладочная синхронизация.
(function () {
  var ACC = 'eltera.account.v1';
  var SES = 'eltera.session.v1';

  // Единственная запись сотрудника. Пароль в прототипе хранится как есть;
  // в проде — только хеш на сервере, клиент его не видит.
  var DEFAULTS = {
    id: 'EMP-0442',
    name: 'Анна Кузнецова',
    email: 'anna.kuznetsova@romashka.ru',
    password: 'eltera2026',
    phone: '+7 900 123-45-67',
    // Доступ к продуктам — грантами. Почта и пароль общие, набор систем разный.
    access: { navigator: true, cabinet: true, crm: true },
    roles: { navigator: 'Менеджер по подбору', cabinet: 'Сотрудник', crm: 'Рекрутер' },
    updatedAt: ''
  };

  var PRODUCTS = [
    { key: 'navigator', name: 'Навигатор', note: 'Подбор позиций', href: 'Навигатор.dc.html' },
    { key: 'cabinet', name: 'Кабинет Eltera', note: 'Результаты оценок', href: 'Портал сотрудника.dc.html' },
    { key: 'crm', name: 'Shellix', note: 'CRM подбора', href: 'CRM.dc.html' }
  ];

  var subs = [];

  function readRaw(key) { try { return JSON.parse(localStorage.getItem(key)) || null; } catch (e) { return null; } }
  function writeRaw(key, val) { try { localStorage.setItem(key, JSON.stringify(val)); } catch (e) {} }

  function get() {
    var a = readRaw(ACC) || {};
    var merged = Object.assign({}, DEFAULTS, a);
    merged.access = Object.assign({}, DEFAULTS.access, a.access || {});
    merged.roles = Object.assign({}, DEFAULTS.roles, a.roles || {});
    return merged;
  }

  function stamp() {
    var d = new Date();
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    return p(d.getDate()) + '.' + p(d.getMonth() + 1) + '.' + d.getFullYear() + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  // Единственная точка изменения учётки. Любая правка почты или пароля
  // мгновенно действует во всех трёх системах — отдельных копий нет.
  function update(patch) {
    var next = Object.assign({}, get(), patch || {}, { updatedAt: stamp() });
    if (patch && patch.access) next.access = Object.assign({}, get().access, patch.access);
    if (patch && patch.roles) next.roles = Object.assign({}, get().roles, patch.roles);
    if (patch && patch.email) next.email = String(patch.email).trim().toLowerCase();
    writeRaw(ACC, next);
    // Смена почты или пароля обесценивает открытые сессии во всех продуктах.
    if (patch && (patch.email || patch.password)) signOut();
    notify();
    return next;
  }

  function normalize(email) { return String(email || '').trim().toLowerCase(); }

  // Возвращает { ok } либо { error: 'email' | 'password' | 'access' }.
  function signIn(email, password, product) {
    var a = get();
    if (normalize(email) !== normalize(a.email)) return { error: 'email' };
    if (String(password || '') !== String(a.password)) return { error: 'password' };
    if (product && !a.access[product]) return { error: 'access' };
    writeRaw(SES, { email: a.email, at: Date.now(), product: product || null });
    notify();
    return { ok: true, account: a };
  }

  function session() {
    var s = readRaw(SES);
    if (!s) return null;
    // Сессия одна на все продукты: вошёл в одном — открыты остальные.
    if (normalize(s.email) !== normalize(get().email)) { signOut(); return null; }
    return s;
  }

  function signOut() { try { localStorage.removeItem(SES); } catch (e) {} notify(); }

  function products() {
    var a = get();
    return PRODUCTS.map(function (p) {
      return { key: p.key, name: p.name, note: p.note, href: p.href, allowed: !!a.access[p.key], role: a.roles[p.key] || '' };
    });
  }

  function notify() {
    var snap = { account: get(), session: session() };
    subs.slice().forEach(function (f) { try { f(snap); } catch (e) {} });
  }

  function subscribe(fn) {
    subs.push(fn);
    return function () { subs = subs.filter(function (x) { return x !== fn; }); };
  }

  try {
    window.addEventListener('storage', function (e) { if (e.key === ACC || e.key === SES) notify(); });
  } catch (e) {}

  window.ElteraAccount = {
    get: get, update: update, signIn: signIn, signOut: signOut,
    session: session, products: products, subscribe: subscribe,
    PRODUCTS: PRODUCTS
  };
})();
