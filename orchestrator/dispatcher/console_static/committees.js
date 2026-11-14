// 委員会（工場の委員会の開催と議事録）。知見の委員会（審査・定例、ADR 0024）と事故の調査委員会（週次の見回り、ADR 0046）
// 一覧は新しい順。PC は左に一覧・右に議事録、スマホは一覧から議事録へ移る。中身は GET /api/committees・/api/committees/<id>（console_committee.py）
// console.js の esc・put・dur・link・route・S を使う（console.js の後に読む）
let cmList = null, cmAt = 0, cmOf = "", cmDoc = {}, cmFilter = "";
const cmWhen = t => { const d = new Date(t); return isNaN(d) ? "" : `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}` };
function cmSkeleton(id) {
  return `<div class="cm ${id ? "open" : ""}"><div class="cm-l"><h1 class="t-heading">委員会</h1><p class="m">工場の委員会がいつ開かれ、何を決めたか。委員会は自分で実装せず、決めたことは Issue にして工場のラインで作る</p>
    <div class="cm-f" id="cm-f"></div><div id="cm-list"><div class="m">読み込み中…</div></div></div><div class="cm-r" id="cm-d"></div></div>`;
}
async function cmLoad() {
  cmAt = Date.now();
  try { cmList = await (await fetch("/api/committees")).json() } catch (e) { cmList = {error: true} }
  if (route()[0] === "committees") cmRender(route()[1]);
}
async function cmLoadDoc(id) {
  cmDoc[id] = "loading";
  try { const r = await fetch("/api/committees/" + id); cmDoc[id] = r.ok ? await r.json() : {error: r.status} } catch (e) { cmDoc[id] = {error: true} }
  if (route()[0] === "committees" && route()[1] === id) cmRender(id);
}
function cmRow(m, sel) {
  return `<a class="cm-row${sel ? " sel" : ""}" href="/committees/${m.id}" data-nav><span class="c-status ${m.level === "bad" ? "is-warn" : "is-ok"}"></span>
    <span class="cm-t"><b>${esc(m.title)}</b><span class="m">${esc(m.committee_ja)} · ${esc(m.kind)}${m.repo ? " · " + esc(m.repo) : ""}</span></span>
    <span class="cm-o"><span>${esc(m.outcome || "")}</span><span class="m">${cmWhen(m.at)}</span></span></a>`;
}
// 議事録の文の中の URL（Issue・PR）は押せるように
const cmText = x => String(x).split(/(https:\/\/\S+)/).map((t, i) => i % 2 ? link(t, t.replace("https://github.com/", "")) : esc(t)).join("");
function cmDetail(id) {
  const d = cmDoc[id];
  if (!d || d === "loading") return '<div class="m">読み込み中…</div>';
  if (d.error) return `<div class="c-empty"><b>議事録が見つかりません</b><a href="/committees" data-nav>委員会の一覧へ</a></div>`;
  const meta = [d.committee_ja, d.kind, d.repo, cmWhen(d.at), d.cost_usd != null ? "$" + Number(d.cost_usd).toFixed(2) : "", d.model || ""].filter(Boolean).map(esc).join(" · ");
  return `<article class="card cm-doc"><a class="m cm-back" href="/committees" data-nav>← 一覧</a><h2>${esc(d.title)}</h2><div class="m">${meta}</div>
    ${d.outcome ? `<div class="cm-out">${esc(d.outcome)}</div>` : ""}${d.link ? `<div>${link(d.link, "PR を開く")}</div>` : ""}
    ${d.sections.map(s => `<section><h3>${esc(s.h)}</h3>${s.text ? `<p>${esc(s.text)}</p>` : ""}${(s.items || []).length ? `<ul>${s.items.map(x => `<li>${cmText(x)}</li>`).join("")}</ul>` : ""}</section>`).join("")}</article>`;
}
function cmRender(arg) {
  if (!cmList || Date.now() - cmAt > 60000) { if (!cmLoad._p) cmLoad._p = cmLoad().finally(() => cmLoad._p = null) }
  document.querySelector(".cm")?.classList.toggle("open", !!arg);
  const cs = (cmList && cmList.committees) || {knowledge: "知見の委員会", incident: "事故の調査委員会"};
  put("cm-f", `<div class="tabs">${[["", "すべて"], ...Object.entries(cs)].map(([k, n]) => `<button data-cmf="${k}" class="${k === cmFilter ? "on" : ""}" aria-pressed="${k === cmFilter}">${esc(n)}</button>`).join("")}</div>`);
  if (cmList && !cmList.error) {
    const xs = cmList.meetings.filter(m => !cmFilter || m.committee === cmFilter);
    put("cm-list", xs.length ? xs.map(m => cmRow(m, String(m.id) === arg)).join("") : '<div class="c-empty"><b>まだ開かれていません</b><span>知見の委員会は知見の PR ごとと週 1 回、事故の調査委員会は週 1 回開かれます</span></div>');
  } else if (cmList) put("cm-list", '<div class="m">読み込めませんでした</div>');
  if (arg) { if (!cmDoc[arg]) cmLoadDoc(arg); put("cm-d", cmDetail(arg)) }
  else put("cm-d", '<div class="c-empty cm-none"><b>開催を選ぶと議事録が出ます</b></div>');
}
document.addEventListener("click", e => { const b = e.target.closest("[data-cmf]"); if (!b) return; cmFilter = b.dataset.cmf; cmRender(route()[1]) });
