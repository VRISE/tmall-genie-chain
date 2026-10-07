// 「猪八戒」问题实体批量灌值。在已登录的
// https://iap.aligenie.com/console/newskill/120248/entity/101852 页面
// 用 F12 控制台粘贴执行；接口靠同源 cookie，ctoken 从 cookie 里取。
//
// 用法：
//   __loadHave()                       // 先拉现值去重
//   __push(['红烧肉怎么做', '月亮为什么发光'])   // 排队
//   __runQ()                            // 6 路并发开始写
//   __stat()                            // 看进度
// 同义词（把多种说法挂到同一个标准答案上）：
//   __syn(56434433, '今天吃什么', ['今晚吃什么', '中午吃什么'])

var SID = 120248, EID = 101852;
function tok() { return (document.cookie.match(/ctoken=([^;&]+)/) || [])[1]; }
function H() { return { 'Content-Type': 'application/json', 'x-csrf-token': tok() }; }
function url(q) { return '/api/skills/' + SID + '/entities/' + EID + '/entries?ctoken=' + tok() + (q || ''); }

window.__have = new Set();
window.__queue = [];
window.__up = { n: 0, ok: 0, skip: 0, bad: [] };

window.__loadHave = function (p) {
  p = p || 1;
  return fetch(url('&pageIndex=' + p + '&pageSize=100&query='), { credentials: 'include', headers: H() })
    .then(function (r) { return r.json(); })
    .then(function (j) {
      var rs = (j.retValue && j.retValue.result) || [];
      rs.forEach(function (x) { window.__have.add(x.normValue); });
      if (rs.length < 100) { return 'have=' + window.__have.size + ' total=' + j.retValue.totalCount; }
      return window.__loadHave(p + 1);
    });
};

window.__push = function (vals) {
  vals.forEach(function (v) { if (window.__queue.indexOf(v) < 0) window.__queue.push(v); });
  return 'queued=' + window.__queue.length;
};

// 名词 × 句式，一次生成一批
window.__pushX = function (nouns, patterns) {
  var out = [];
  nouns.forEach(function (n) {
    patterns.forEach(function (p) {
      var v = p.split('{a}').join(n);
      if (out.indexOf(v) < 0) out.push(v);
    });
  });
  return window.__push(out);
};

window.__runQ = function () {
  var q = window.__queue, i = 0;
  window.__queue = [];
  window.__up = { n: 0, ok: 0, skip: 0, bad: [] };
  function run() {
    if (i >= q.length) return Promise.resolve();
    var v = q[i++];
    if (window.__have.has(v)) { window.__up.skip++; return run(); }
    window.__have.add(v);
    return fetch(url(), {
      method: 'POST', credentials: 'include', headers: H(),
      body: JSON.stringify({ normValue: v, synonyms: [] })
    }).then(function (r) {
      return r.text().then(function (t) {
        window.__up.n++;
        if (r.status === 200) window.__up.ok++;
        else { window.__up.bad.push(v + ' ' + r.status); window.__have.delete(v); }
        return run();
      });
    }).catch(function () { window.__up.bad.push(v + ' ERR'); window.__have.delete(v); return run(); });
  }
  Promise.all([run(), run(), run(), run(), run(), run()]).then(function () { window.__up.done = 1; });
  return 'running=' + q.length;
};

window.__stat = function () { return JSON.stringify(window.__up) + ' have=' + window.__have.size; };

// 同义词：normValue 必须一起带上（不带会被清掉），所以签名是 (id, 标准值, [说法…])
window.__syn = function (id, normValue, synonyms) {
  return fetch(url(''), {
    method: 'PUT', credentials: 'include', headers: H(),
    body: JSON.stringify({ id: id, entityId: EID, normValue: normValue, synonyms: synonyms })
  }).then(function (r) { return r.text(); });
};

window.__del = function (id) {
  return fetch('/api/skills/' + SID + '/entities/' + EID + '/entries/' + id + '?ctoken=' + tok(),
    { method: 'DELETE', credentials: 'include', headers: H() })
    .then(function (r) { return r.status + ' ' + r.statusText; });
};
