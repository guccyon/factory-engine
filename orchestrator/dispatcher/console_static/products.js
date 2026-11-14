// 製品のページ（ADR 0059、#475）。概要／見通し／障害と問題／利用状況／リリースと環境／準備度。
// 準備度は今までの断片（/products/body?p=。products_page.py）、リリースと環境は出荷のページ（shipPage）をそのまま使う。/ship はここへ移した
// console.js の esc・put・busy・card・shipPage・repoOf・loadProd・S を使う（console.js の後に読む）
const PP_TABS = [["overview", "概要"], ["outlook", "見通し"], ["issues", "障害と問題"], ["usage", "利用状況"], ["release", "リリースと環境"], ["readiness", "準備度"]];
let ppTab = "overview";
function ppTabFromUrl() { const t = new URLSearchParams(location.search).get("tab"); return PP_TABS.some(x => x[0] === t) ? t : null }
function ppSkeleton(product) {
  ppTab = ppTabFromUrl() || ppTab;
  return `<div class="pp"><div class="pp-h"><a class="m" href="/products" data-nav>← 製品</a><h1 class="t-heading">${esc(product)}</h1></div>
    <nav class="pp-tabs" role="tablist">${PP_TABS.map(([k, n]) => `<button role="tab" data-pptab="${k}" aria-selected="${k === ppTab}">${n}</button>`).join("")}</nav>
    ${PP_TABS.map(([k]) => `<section class="pp-s" data-pp="${k}" ${k === ppTab ? "" : "hidden"}><div id="pp-${k}"></div>${k === "readiness" ? '<div id="prod-b"><div class="m">読み込み中…</div></div>' : ""}</section>`).join("")}</div>`;
}
const ppItems = (s, p) => (s.items || []).filter(i => repoOf(i) === p);
function ppOverview(s, p) {
  const xs = ppItems(s, p), r = (s.releases || []).find(x => x.repo === p), c = k => xs.filter(i => i.stage === k).length;
  const ib = ((s.inbox || {}).items || []).filter(i => !i.answered && i.kind !== "notice" && String(i.ref || "").startsWith(p));
  const ver = e => r ? VER(((r.environments || {})[e] || {}).release_id) : "なし";
  const t = (k, v, tab, cls) => `<button class="c-tile${cls ? " " + cls : ""}" data-pptab="${tab}"><div class="k">${k}</div><div class="v">${v}</div></button>`;
  return `<div class="c-tiles">${t("本番", ver("prod"), "release")}${t("Web 検証", ver("staging"), "release")}${t("作業中", c("running"), "outlook")}
    ${t("次に来るもの", c("queued") + c("ready") + c("waiting"), "outlook")}${t("止まっている", c("human"), "issues", c("human") ? "is-bad" : "")}${t("判断待ち", ib.length, "issues", ib.length ? "is-warn" : "")}</div>
    ${ib.length ? `<section class="card"><h2>判断待ち</h2><ul class="c-list">${ib.map(i => `<li><a class="c-g" href="${ibHref(i.key)}" data-nav>${esc(i.title)}</a><span class="c-m">${esc(((s.inbox || {}).kinds || {})[i.kind] || i.kind)}</span></li>`).join("")}</ul></section>` : ""}`;
}
// 見通し。Gen1 はロードマップを持たないので、チケットの順番と投入待ちから「次に来るもの」を出す
function ppOutlook(s, p) {
  const xs = ppItems(s, p), run = xs.filter(i => i.stage === "running" || i.stage === "review"),
    next = xs.filter(i => ["queued", "ready", "waiting"].includes(i.stage)).sort((a, b) => (a.rank || 999) - (b.rank || 999) || (a.priority ?? 9) - (b.priority ?? 9));
  const sec = (h, ys, empty) => `<section class="card"><h2>${h}（${ys.length}）</h2>${ys.length ? ys.slice(0, 12).map(card).join("") : `<div class="m">${empty}</div>`}${ys.length > 12 ? `<div class="m">ほか ${ys.length - 12} 件は<a href="/line" data-nav>ライン</a>で</div>` : ""}</section>`;
  return sec("作業中・検査中", run, "いまはない") + sec("次に来るもの", next, "投入待ちのチケットはない") +
    `<div class="m">ロードマップを工場に持たせるかはまだ決めていない（ADR 0059 の残り）。今はチケットの順番と投入待ちから出している</div>`;
}
// 障害と問題。QA の差し止め（S1 の Incident）・止まっている作業・p0 の修正
function ppIssues(s, p) {
  const hold = (s.factory || {}).qa_hold, inc = ((hold || {}).incidents || []).filter(i => !i.repo || i.repo === p);
  const xs = ppItems(s, p), stuck = xs.filter(i => i.stage === "human"), p0 = xs.filter(i => i.stage !== "human" && i.level === "p0");
  return `<section class="card"><h2>進行中の障害</h2>${inc.length ? `<ul class="c-list">${inc.map(i => `<li><span class="c-status is-bad">${esc(String(i.severity || "").toUpperCase())}</span><span class="c-g">${esc(i.title || "")}${i.drill ? "（演習）" : ""}</span></li>`).join("")}</ul><div class="m">QA がラインを差し止めている。S1・S2 の修正は工場が自分で直して出し直す</div>` : '<div class="c-empty"><b>進行中の障害はありません</b></div>'}</section>
    <section class="card"><h2>急ぎの修正（p0）</h2>${p0.length ? p0.map(card).join("") : '<div class="m">いまはない</div>'}</section>
    <section class="card"><h2>止まっている作業</h2>${stuck.length ? stuck.map(card).join("") : '<div class="m">いまはない</div>'}</section>`;
}
// 利用状況は運用の機能（上位のプラン。ADR 0059 の 2）。置き場だけ作る
function ppUsage() {
  return `<div class="c-gate"><b>利用状況</b><span class="c-sub">使われ方（画面ごとの利用・イベントの推移）を、事業部が持つ正本か、つないだ外部のサービス（Google Analytics・Firebase・BigQuery など）から出します。上位のプランの機能です</span>
    <div class="c-shot">例の画面</div><span class="m">運用を始めるときに、オンボーディングで置き場を決めて有効にします（期間限定で試す入口は商用化のときに足します）</span></div>`;
}
function ppRender(s, product) {
  put("pp-overview", ppOverview(s, product));
  put("pp-outlook", ppOutlook(s, product));
  put("pp-issues", ppIssues(s, product));
  put("pp-usage", ppUsage());
  const r = (s.releases || []).filter(x => x.repo === product);
  if (!document.getElementById("ship")) put("pp-release", '<div id="ship"></div>');
  if (force || !busy("ship")) put("ship", r.length ? shipPage(r) : '<div class="c-empty"><b>まだ出荷していません</b><span>出荷の指示は Claude（窓口の MCP）からもできます</span></div>');
  if (prodOf !== product || Date.now() - prodAt > 60000 || !document.querySelector("#prod-b .prod")) loadProd(product);
}
document.addEventListener("click", e => {
  const b = e.target.closest("[data-pptab]"); if (!b) return;
  ppTab = b.dataset.pptab;
  document.querySelectorAll(".pp-tabs [data-pptab]").forEach(x => x.setAttribute("aria-selected", x.dataset.pptab === ppTab));
  document.querySelectorAll(".pp-s").forEach(x => x.hidden = x.dataset.pp !== ppTab);
  history.replaceState(null, "", location.pathname + "?tab=" + ppTab);
});
// /ship（前の出荷のページ・通知のリンク）は製品のページの「リリースと環境」へ。出荷した製品が 1 つならその製品、ほかは製品の一覧
function ppShipRedirect(s) {
  const rs = s.releases || [];
  history.replaceState(null, "", rs.length === 1 ? `/products/${rs[0].repo}?tab=release` : "/products");
}
