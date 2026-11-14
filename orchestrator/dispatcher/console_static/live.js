// 事業部の様子（ADR 0059、#476）。レーンの上をチケットが流れていく様子と、リアルタイムに動くチャート。顧客の安心とデモに使う。
// レーンはジョブの種類ごと。左から 順番待ち → 作業中（経過に合わせて進む）→ 検査・マージ待ち。止まっているものは赤
// 更新はサーバーからの知らせ（/api/stream の SSE）。つながらないときは console.js の 10 秒ごとの読み直し
// console.js の esc・put・dur・repoOf・S を使う（console.js の後に読む）
const LIVE_KIND = {implement: "実装", plan: "計画", design: "デザイン", qa: "QA", review: "検査", desk: "窓口", investigate: "調査", spike: "スパイク",
  governance: "委員会", ship: "出荷", other: "そのほか"};
const liveSeries = {at: [], running: [], queued: [], done: []};
const LIVE_KEEP = 180;   // 点の数（知らせのたびに 1 点。だいたい 30 分ほど）
function liveSample(s) {
  const c = k => (s.items || []).filter(i => i.stage === k).length, now = Date.now();
  if (liveSeries.at.length && now - liveSeries.at[liveSeries.at.length - 1] < 1500) return;
  liveSeries.at.push(now); liveSeries.running.push(c("running")); liveSeries.queued.push(c("queued") + c("ready")); liveSeries.done.push(c("done"));
  for (const k in liveSeries) if (liveSeries[k].length > LIVE_KEEP) liveSeries[k].shift();
}
function liveSkeleton() {
  return `<div class="live"><div class="live-h"><h1 class="t-heading">事業部の様子</h1><span class="m" id="live-conn"></span><a class="m" href="/line" data-nav>ラインの一覧 →</a></div>
    <div id="live-tiles"></div><section class="card"><h2>レーン</h2><div id="live-lanes"></div></section>
    <section class="card"><h2>動き <span class="m">この画面を開いてからの推移</span></h2><div id="live-chart"></div></section></div>`;
}
function liveLanes(s) {
  const xs = (s.items || []).filter(i => ["queued", "ready", "running", "review", "human"].includes(i.stage));
  const kindOf = i => LIVE_KIND[i.kind] ? i.kind : i.stage === "review" ? "review" : "implement";
  const kinds = [...new Set(xs.map(kindOf))].sort((a, b) => Object.keys(LIVE_KIND).indexOf(a) - Object.keys(LIVE_KIND).indexOf(b));
  if (!xs.length) return `<div class="c-empty"><b>いまは流れている作業がありません</b><span>チケットが入ると、ここをレーンに沿って流れていきます</span></div>`;
  const pos = i => {
    if (i.stage === "queued" || i.stage === "ready") return 4 + ((i.rank || 1) % 5) * 2;
    if (i.stage === "running") return 18 + Math.min(1, (i.elapsed_sec || 0) / Math.max(1, i.timeout_sec || 2400)) * 60;
    if (i.stage === "review") return 84;
    return 50;
  };
  return `<div class="c-lanes live-lanes">${kinds.map(k => { const ys = xs.filter(i => kindOf(i) === k), run = ys.filter(i => i.stage === "running").length;
    return `<div class="c-lane"><span class="c-name">${esc(LIVE_KIND[k])}</span><div class="c-track live-track">${ys.map(i =>
      `<span class="c-job live-job${i.stage === "human" ? " is-stuck" : ""}${i.stage === "running" ? " is-run" : ""}" style="left:${pos(i).toFixed(1)}%" title="${esc((i.ref || "") + " " + (i.title || ""))}"></span>`).join("")}</div>
      <span class="c-m">${run} · ${ys.length}</span></div>`}).join("")}
    <div class="live-axis m"><span>順番待ち</span><span>作業中</span><span>検査・マージ待ち</span></div></div>`;
}
function liveLine(vals, cls) {
  const n = vals.length; if (n < 2) return "";
  const hi = Math.max(1, ...liveSeries.running, ...liveSeries.queued), W = 600, H = 120;
  return `<path class="${cls}" d="${vals.map((v, i) => `${i ? "L" : "M"}${(i * W / (n - 1)).toFixed(1)} ${(H - 4 - v * (H - 12) / hi).toFixed(1)}`).join(" ")}"/>`;
}
function liveChart() {
  if (liveSeries.at.length < 2) return '<div class="m">点が 2 つ集まると線になります</div>';
  const last = k => liveSeries[k][liveSeries[k].length - 1];
  return `<svg class="live-svg" viewBox="0 0 600 120" preserveAspectRatio="none" role="img" aria-label="作業中と順番待ちの数の推移">${liveLine(liveSeries.queued, "q")}${liveLine(liveSeries.running, "r")}</svg>
    <div class="live-leg m"><span class="r">作業中 ${last("running")}</span><span class="q">順番待ち ${last("queued")}</span><span>今日の済み ${last("done")}</span></div>`;
}
function liveRender(s) {
  liveSample(s);
  const f = s.factory || {}, c = k => (s.items || []).filter(i => i.stage === k).length;
  const t = (k, v, cls) => `<div class="c-tile${cls ? " " + cls : ""}"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  put("live-tiles", `<div class="c-tiles">${t("作業中", c("running"))}${t("順番待ち", c("queued") + c("ready"))}${t("検査・マージ待ち", c("review"))}${t("今日の済み", c("done"))}${t("止まっている", c("human"), c("human") ? "is-bad" : "")}${t("今日の費用", "$" + (f.cost_today_usd || 0).toFixed(2))}</div>`);
  put("live-lanes", liveLanes(s));
  put("live-chart", liveChart());
  put("live-conn", liveStreamOn ? "● リアルタイム" : "10 秒ごとに更新");
}
// サーバーからの知らせ。changed を受けたら読み直す（console.js の load）。切れたら EventSource が自分でつなぎ直す
let liveStreamOn = false;
(() => {
  if (!window.EventSource) return;
  const es = new EventSource("/api/stream");
  es.addEventListener("changed", () => { liveStreamOn = true; load() });
  es.onerror = () => { liveStreamOn = false };
})();
