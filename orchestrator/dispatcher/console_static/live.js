// 稼働状況（ADR 0059、#476。2026-11-16 に「事業部の様子」から改め、工場の保守を「保守」タブに入れた）。
// 「流れ」タブは工程ごとの段の上をチケットが流れていく様子と、作業中・順番待ちの数の推移。顧客の安心とデモに使う。「保守」タブは maint.js
// 段は工程（計画・実装・検査・QA・出荷。委員会・窓口は動いているときだけ）。どの工程かはいまのジョブの種類で決める（/api/live の steps）。
// 段の中の左右は状態: 左の「待ち」→ 中の「作業中」（経過に合わせて進む）→ 右の「次へ渡す待ち」。人の対応待ちは赤
// 丸に触れる（マウスを載せる・押す）と、チケット・状態・ジョブ・経過を出す
// 更新はサーバーからの知らせ（/api/stream の SSE）。つながらないときは console.js の 10 秒ごとの読み直し
// console.js の esc・put・dur・link・repoOf・S を使う（console.js の後に読む）
let liveMeta = null, liveMetaAt = 0, liveDots = [], liveTipFor = null, liveTipPinned = false;
async function liveLoadMeta() {
  liveMetaAt = Date.now();
  try { liveMeta = await (await fetch("/api/live")).json() } catch (e) { liveMeta = liveMeta || null }
  if (route()[0] === "live" && S) liveRender(S);
}
function liveSkeleton() {
  liveTab = new URLSearchParams(location.search).get("tab") === "maint" ? "maint" : "flow";
  return `<div class="live"><div class="live-h"><h1 class="t-heading">稼働状況</h1><span class="m" id="live-conn"></span><a class="m" href="/line" data-nav>ラインの一覧 →</a></div>
    <nav class="pp-tabs" role="tablist">${LIVE_TABS.map(([k, n]) => `<button role="tab" data-livetab="${k}" aria-selected="${k === liveTab}">${n}<span class="c-count" data-n="live-${k}" hidden></span></button>`).join("")}</nav>
    <div class="live-s" data-live="maint" ${liveTab === "maint" ? "" : "hidden"}>${mtSkeleton()}</div>
    <div class="live-s" data-live="flow" ${liveTab === "flow" ? "" : "hidden"}><div id="live-tiles"></div><section class="card"><h2>工程ごとの流れ <span class="m">丸 1 つがチケット 1 件。触れると詳しく</span></h2><div id="live-lanes"></div><div id="live-leg"></div></section>
    <section class="card"><h2>作業中と順番待ちの推移 <span class="m" id="live-ch"></span></h2><div id="live-chart"></div></section></div></div>`;
}
const LIVE_TABS = [["flow", "流れ"], ["maint", "保守"]];
let liveTab = "flow";
document.addEventListener("click", e => {
  const b = e.target.closest("[data-livetab]"); if (!b) return;
  liveTab = b.dataset.livetab;
  document.querySelectorAll("[data-livetab]").forEach(x => x.setAttribute("aria-selected", x.dataset.livetab === liveTab));
  document.querySelectorAll(".live-s").forEach(x => x.hidden = x.dataset.live !== liveTab);
  history.replaceState(null, "", location.pathname + (liveTab === "maint" ? "?tab=maint" : ""));
  liveTip(null);
});
const LIVE_ZONES = [["wait", "待ち"], ["work", "作業中"], ["next", "次へ渡す待ち"]];
function liveStepOf(i, steps) {
  if (i.kind) { const st = steps.find(x => x.kinds.includes(i.kind)); if (st) return st.id }
  if (i.pr_url || i.stage === "review") return "review";
  return "build";
}
// 置き場所（左からの %）と、触れたときの状態の文
// 返すのは [区画, 作業中の進み（0〜1）, 状態の文]。待ちと次へ渡す待ちは、区画の中に等間隔に並べる（liveLanes）
function livePlace(i) {
  if (i.stage === "human") return ["work", 0.3, "人の対応待ち" + (i.note ? `: ${i.note}` : "")];
  if (i.stage === "ready") return ["wait", 0, "投入待ち（同時に流す上限で待っている）"];
  if (i.stage === "queued") return ["wait", 0, i.held ? "保留中" : `順番待ち（${i.rank || "?"} 番目）`];
  if (i.stage === "running") {
    const t = i.timeout_sec || 2400, e = i.elapsed_sec || 0;
    return ["work", Math.min(1, e / Math.max(1, t)), `作業中 ${dur(e)}（上限 ${dur(t)}）${i.stopping ? " · 止めている" : ""}`];
  }
  if (i.stage === "review") return [/マージ待ち|承認待ち/.test(i.note || "") ? "next" : "wait", 0, i.note || "検査待ち"];
  return ["work", 0.5, i.stage];
}
const LIVE_SPAN = {wait: [3, 23], work: [29, 79], next: [85, 97]};
function liveLanes(s) {
  const steps = (liveMeta && liveMeta.steps) || [];
  if (!steps.length) return '<div class="m">読み込み中…</div>';
  const kinds = (liveMeta && liveMeta.kinds) || {};
  const xs = (s.items || []).filter(i => ["ready", "queued", "running", "review", "human"].includes(i.stage));
  const ships = (s.releases || []).flatMap(r => (r.pending || []).map(p => ({ship: true, ref: r.repo, title: "出荷の処理", request: p.request})));
  liveDots = [];
  const rows = steps.map(st => {
    const ys = st.id === "ship" ? ships : xs.filter(i => liveStepOf(i, steps) === st.id);
    if (!ys.length && !st.fixed) return "";
    const placed = ys.map(i => i.ship ? ["work", 0.3, "処理中（" + ({ship_requested: "staging へ出荷", approved: "本番へ出荷", rollback_requested: "前のバージョンへ戻す"}[i.request] || i.request) + "）"] : livePlace(i));
    const dots = ys.map((i, k) => {
      const [zone, prog, state] = placed[k], [a, b] = LIVE_SPAN[zone];
      const same = placed.map((p, j) => [p[0], j]).filter(p => p[0] === zone).map(p => p[1]), m = same.indexOf(k);
      const left = zone === "work" ? a + prog * (b - a) : a + (same.length > 1 ? m * Math.min(8, (b - a) / (same.length - 1)) : 0);
      const n = liveDots.push({i, step: st.name, state, kind: i.kind ? (kinds[i.kind] || i.kind) : ""}) - 1;
      const cls = i.stage === "human" ? " is-stuck" : (i.stage === "running" || i.ship) ? " is-run" : "";
      return `<button type="button" class="c-job live-job${cls}" style="left:${left.toFixed(1)}%" data-li="${n}" data-key="${esc(String(i.job_id || i.ref || "") + ":" + (i.kind || i.stage || ""))}" aria-label="${esc((i.ref || "") + " " + state)}"></button>`;
    }).join("");
    const run = ys.filter(i => i.stage === "running" || i.ship).length;
    return `<div class="c-lane"><span class="c-name">${esc(st.name)}</span><div class="c-track live-track">${LIVE_ZONES.map(([z]) => `<span class="live-z live-z-${z}"></span>`).join("")}${dots}</div>
      <span class="c-m" title="作業中 · この工程の全部">${run} · ${ys.length}</span></div>`;
  }).join("");
  const empty = !xs.length && !ships.length ? `<div class="c-empty"><b>いまは流れている作業がありません</b><span>チケットが入ると、ここを工程に沿って流れていきます</span></div>` : "";
  return `${empty}<div class="c-lanes live-lanes">${rows}<div class="live-axis m"><span></span>${LIVE_ZONES.map(([z, n]) => `<span class="live-ax-${z}">${n}</span>`).join("")}<span></span></div></div>`;
}
function liveLegend() {
  return `<div class="live-leg m"><span><i class="live-dot is-run"></i>作業中（点滅）</span><span><i class="live-dot"></i>待ち・次へ渡す待ち</span><span><i class="live-dot is-stuck"></i>人の対応待ち</span>
    <span>右端の数は「作業中 · その工程の全部」</span><span>検査で指摘が付くと「指摘の直し」で実装に戻り、直したら検査にかけ直す</span></div>`;
}
// チケットの URL。記録に URL がなければ、GitHub の「owner/repo#番号」から作る
function liveTicketUrl(i) {
  if (i.url && /^https:\/\//.test(i.url)) return i.url;
  const m = /^([\w.-]+\/[\w.-]+)#(\d+)$/.exec(i.ref || "");
  return m && (!i.source || i.source === "github") ? `https://github.com/${m[1]}/issues/${m[2]}` : "";
}
function liveTipHtml(d) {
  const i = d.i, u = liveTicketUrl(i);
  const a = t => u ? `<a href="${esc(u)}" target="_blank" rel="noopener" title="チケットを開く">${t}</a>` : t;
  return `<b>${a(esc(i.ref || ""))}</b>${i.title ? `<div>${a(esc(i.title))}</div>` : ""}<dl>
    <dt>工程</dt><dd>${esc(d.step)}</dd><dt>状態</dt><dd>${esc(d.state)}</dd>
    ${d.kind ? `<dt>ジョブ</dt><dd>${esc(d.kind)}${i.attempt > 1 ? `（${i.attempt} 回目）` : ""}${i.job_id ? ` · #${i.job_id}` : ""}</dd>` : ""}
    ${i.priority != null ? `<dt>優先度</dt><dd>${i.priority}</dd>` : ""}</dl>
    ${i.pr_url ? `<div class="live-tip-l">${link(i.pr_url, "PR を開く")}</div>` : ""}`;
}
function liveTip(btn, pin) {
  let tip = document.getElementById("live-tip");
  if (!tip) { tip = document.createElement("div"); tip.id = "live-tip"; tip.className = "live-tip"; tip.setAttribute("role", "tooltip"); document.body.appendChild(tip) }
  if (!btn) { tip.hidden = true; liveTipFor = null; liveTipPinned = false; return }
  const d = liveDots[+btn.dataset.li]; if (!d) return;
  tip.innerHTML = liveTipHtml(d); tip.hidden = false; liveTipFor = btn.dataset.key; liveTipPinned = !!pin;
  const r = btn.getBoundingClientRect(), w = Math.min(320, innerWidth - 24);
  tip.style.width = w + "px";
  tip.style.left = Math.max(12, Math.min(innerWidth - w - 12, r.left + r.width / 2 - w / 2)) + scrollX + "px";
  const below = r.bottom + 8 + tip.offsetHeight < innerHeight;
  tip.style.top = (below ? r.bottom + 8 : r.top - tip.offsetHeight - 8) + scrollY + "px";
}
// 丸から詳しい表示へマウスを移してリンクを押せるよう、閉じるのを少し待つ（表示の上にいる間は閉じない）
let liveTipHide = 0;
const liveTipLater = () => { clearTimeout(liveTipHide); liveTipHide = setTimeout(() => { if (!liveTipPinned) liveTip(null) }, 250) };
document.addEventListener("mouseover", e => {
  if (e.target.closest("#live-tip")) { clearTimeout(liveTipHide); return }
  const b = e.target.closest(".live-job[data-li]"); if (b && !liveTipPinned) { clearTimeout(liveTipHide); liveTip(b, false) }
});
document.addEventListener("mouseout", e => {
  if (liveTipPinned || e.relatedTarget?.closest?.("#live-tip,.live-job[data-li]")) return;
  if (e.target.closest(".live-job[data-li],#live-tip")) liveTipLater();
});
document.addEventListener("focusin", e => { const b = e.target.closest(".live-job[data-li]"); if (b) liveTip(b, false) });
document.addEventListener("click", e => {
  const b = e.target.closest(".live-job[data-li]");
  if (b) { if (liveTipPinned && liveTipFor === b.dataset.key) liveTip(null); else liveTip(b, true); return }
  if (!e.target.closest("#live-tip")) liveTip(null);
});
document.addEventListener("keydown", e => { if (e.key === "Escape") liveTip(null) });
function liveChart(s) {
  if (!liveMeta || !liveMeta.trend) return '<div class="m">読み込み中…</div>';
  const now = {at: new Date().toISOString(), running: (s.items || []).filter(i => i.stage === "running").length, queued: (s.items || []).filter(i => i.stage === "queued").length};
  const pts = [...liveMeta.trend, now];
  const hi = Math.max(1, ...pts.map(p => Math.max(p.running, p.queued))), W = 600, H = 140, n = pts.length;
  const x = k => (k * W / Math.max(1, n - 1)).toFixed(1), y = v => (H - 6 - v * (H - 16) / hi).toFixed(1);
  const line = (k, cls) => `<path class="${cls}" d="${pts.map((p, j) => `${j ? "L" : "M"}${x(j)} ${y(p[k])}`).join(" ")}"/>`;
  const t0 = new Date(pts[0].at), hm = d => `${d.getHours()}:${String(d.getMinutes()).padStart(2, "0")}`;
  return `<div class="live-chart-w"><span class="live-ymax m">${hi}</span><svg class="live-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="直近の作業中と順番待ちの数の推移">
    <line class="grid" x1="0" x2="${W}" y1="${y(hi)}" y2="${y(hi)}"/><line class="grid" x1="0" x2="${W}" y1="${y(0)}" y2="${y(0)}"/>${line("queued", "q")}${line("running", "r")}</svg></div>
    <div class="live-x m"><span>${hm(t0)}</span><span>いま</span></div>
    <div class="live-leg m"><span class="r">作業中 ${now.running}</span><span class="q">順番待ち ${now.queued}</span><span>${liveMeta.bucket_min} 分ごと。ジョブの記録から数えるので、開いた直後から見える</span></div>`;
}
function liveRender(s) {
  if (!liveMeta || Date.now() - liveMetaAt > 60000) { if (!liveLoadMeta._p) liveLoadMeta._p = liveLoadMeta().finally(() => liveLoadMeta._p = null) }
  const f = s.factory || {}, c = k => (s.items || []).filter(i => i.stage === k).length;
  const t = (k, v, cls) => `<div class="c-tile${cls ? " " + cls : ""}"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  put("live-tiles", `<div class="c-tiles">${t("作業中", c("running"))}${t("順番待ち", c("queued") + c("ready"))}${t("検査・マージ待ち", c("review"))}${t("本日完了分", c("done"))}${t("人の対応待ち", c("human"), c("human") ? "is-bad" : "")}${t("今日の費用", "$" + (f.cost_today_usd || 0).toFixed(2))}</div>`);
  put("live-lanes", liveLanes(s));
  put("live-leg", liveLegend());
  if (liveMeta) put("live-ch", `直近 ${liveMeta.trend_hours} 時間`);
  put("live-chart", liveChart(s));
  put("live-conn", liveStreamOn ? "● リアルタイム" : "10 秒ごとに更新");
  // 保守のタブ（maint.js）。タブの数は、工場の不調と建設者宛ての判断の件
  mtRender(s);
  const n = mtCounts(s), mn = n.al + n.b + n.late, cb = document.querySelector('.c-count[data-n="live-maint"]');
  if (cb) { cb.textContent = mn || ""; cb.hidden = !mn; cb.classList.toggle("is-new", mn > 0) }
  // 開いている詳しい表示は、描き直したあとの同じ丸に付け直す（中身も新しくする）
  if (liveTipFor != null) { const b = [...document.querySelectorAll(".live-job[data-key]")].find(x => x.dataset.key === liveTipFor); b ? liveTip(b, liveTipPinned) : liveTip(null) }
}
// サーバーからの知らせ。changed を受けたら読み直す（console.js の load）。切れたら EventSource が自分でつなぎ直す
let liveStreamOn = false;
(() => {
  if (!window.EventSource) return;
  const es = new EventSource("/api/stream");
  es.addEventListener("changed", () => { liveStreamOn = true; load() });
  es.onerror = () => { liveStreamOn = false };
})();
