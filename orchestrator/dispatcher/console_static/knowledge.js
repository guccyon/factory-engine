// 知見（事業部の知見の一覧と 1 件。ADR 0061 の決定 8）。正本は製品リポの .factory-config/knowledge/。ここは supervisor が残す写しを読む
// ドメインの業務知識（工場に依存しないもの）は製品の docs/ にあるので、そこへの道だけ出す
// 委員会のページと同じ並び（PC は左に一覧・右に 1 件、スマホは一覧から 1 件へ移る）。中身は GET /api/knowledge・/api/knowledge/<id>（console_knowledge.py）
// console.js の esc・put・link・route を使う（console.js の後に読む）
let knList = null, knAt = 0, knDoc = {}, knQ = "", knTheme = "", knConf = "", knTimer = null;
const knHref = i => `/knowledge/${i.repo}/${i.id}`;
function knSkeleton(arg) {
  return `<div class="cm kn ${arg ? "open" : ""}"><div class="cm-l"><h1 class="t-heading">知見</h1>
    <p class="m">事業部の開発者が全員知っておく知見。チケットの振り返りで見つけ、知見のレビューを通して事業部に広げたもの。ワーカーには作用先が合う仕事に渡る</p>
    <div class="cm-f" id="kn-f"></div><div class="m" id="kn-p"></div><div id="kn-list"><div class="m">読み込み中…</div></div></div><div class="cm-r" id="kn-d"></div></div>`;
}
async function knLoad() {
  knAt = Date.now();
  const q = new URLSearchParams();
  if (knQ) q.set("q", knQ);
  if (knTheme) q.set("theme", knTheme);
  if (knConf) q.set("confidence", knConf);
  try { knList = await (await fetch("/api/knowledge?" + q)).json() } catch (e) { knList = {error: true} }
  if (route()[0] === "knowledge") knRender(route()[1]);
}
async function knLoadDoc(arg) {
  knDoc[arg] = "loading";
  const [o, r, id] = arg.split("/");
  try { const x = await fetch(`/api/knowledge/${id}?repo=${encodeURIComponent(o + "/" + r)}`); knDoc[arg] = x.ok ? await x.json() : {error: x.status} } catch (e) { knDoc[arg] = {error: true} }
  if (route()[0] === "knowledge" && route()[1] === arg) knRender(arg);
}
function knRow(i, sel) {
  const warn = i.status === "stale" || i.status === "draft";
  return `<a class="cm-row${sel ? " sel" : ""}" href="${knHref(i)}" data-nav><span class="c-status ${warn ? "is-warn" : "is-ok"}"></span>
    <span class="cm-t"><b>${esc(i.statement || "")}</b><span class="m">${esc(i.id)} · ${esc(i.theme_ja)} · ${esc(i.repo)}</span></span>
    <span class="cm-o"><span>${esc(i.confidence_ja)}</span><span class="m">${esc(i.status_ja)}</span></span></a>`;
}
const knList2 = (h, xs) => (xs || []).length ? `<section><h3>${esc(h)}</h3><ul>${xs.map(x => `<li>${esc(x)}</li>`).join("")}</ul></section>` : "";
function knDetail(arg) {
  const d = knDoc[arg];
  if (!d || d === "loading") return '<div class="m">読み込み中…</div>';
  if (d.error) return `<div class="c-empty"><b>知見が見つかりません</b><a href="/knowledge" data-nav>知見の一覧へ</a></div>`;
  const meta = [d.id, d.repo, d.theme_ja, d.confidence_ja, d.status_ja, d.acts_on_ja ? "作用: " + d.acts_on_ja : ""].filter(Boolean).map(esc).join(" · ");
  const scope = Object.entries(d.scope || {}).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : v}`);
  const dates = [d.verified_at ? "確かめた日 " + d.verified_at : "", d.review_by ? "見直す期限 " + d.review_by : "", d.uses ? `直近 60 日でワーカーに ${d.uses} 回渡した` : "直近 60 日はワーカーに渡していない"].filter(Boolean);
  return `<article class="card cm-doc"><a class="m cm-back" href="/knowledge" data-nav>← 一覧</a><h2>${esc(d.id)}</h2><div class="m">${meta}</div>
    <section><h3>主張</h3><p>${esc(d.statement || "")}</p></section>
    ${knList2("効く範囲", scope)}${knList2("確かさと使われ方", dates)}${knList2("無効になる条件", d.invalidate_when)}
    ${knList2("根拠", d.evidence)}${knList2("関わる知見", d.related)}${knList2("決めた記録", d.decided_by ? [d.decided_by] : [])}
    ${d.enforcement ? knList2("担保", [d.enforcement]) : ""}<div>${link(d.url, "製品リポで開く")}</div></article>`;
}
function knRender(arg) {
  if (!knList || Date.now() - knAt > 60000) { if (!knLoad._p) knLoad._p = knLoad().finally(() => knLoad._p = null) }
  document.querySelector(".kn")?.classList.toggle("open", !!arg);
  if (!document.getElementById("kn-q") || (knList && knList.themes && document.getElementById("kn-th").options.length < 2)) {
    const th = (knList && knList.themes) || {};
    put("kn-f", `<div class="kn-fs"><input id="kn-q" type="search" placeholder="言葉で探す" value="${esc(knQ)}" aria-label="言葉で探す">
      <select id="kn-th" aria-label="テーマ"><option value="">すべてのテーマ</option>${Object.entries(th).map(([k, n]) => `<option value="${k}"${k === knTheme ? " selected" : ""}>${esc(n)}</option>`).join("")}</select>
      <select id="kn-cf" aria-label="確かさ"><option value="">すべての確かさ</option>${Object.entries((knList && knList.confidence) || {}).map(([k, n]) => `<option value="${k}"${k === knConf ? " selected" : ""}>${esc(n)}</option>`).join("")}</select></div>`);
  }
  if (knList && !knList.error) {
    put("kn-p", knList.products.map(p => `${esc(p.repo)} の知見 ${p.count} 件 · ${link(p.docs_url, "ドメインの知識（製品の docs/）")}`).join("<br>"));
    put("kn-list", knList.items.length ? knList.items.map(i => knRow(i, `${i.repo}/${i.id}` === arg)).join("")
      : (knList.products.length ? '<div class="c-empty"><b>合う知見はありません</b></div>' : '<div class="c-empty"><b>知見の写しがまだありません</b><span>工場が 1 時間に 1 回ほど製品リポから読みます</span></div>'));
  } else if (knList) put("kn-list", '<div class="m">読み込めませんでした</div>');
  if (arg) { if (!knDoc[arg]) knLoadDoc(arg); put("kn-d", knDetail(arg)) }
  else put("kn-d", '<div class="c-empty cm-none"><b>知見を選ぶと中身が出ます</b></div>');
}
document.addEventListener("input", e => {
  if (e.target.id === "kn-q") { knQ = e.target.value.trim(); clearTimeout(knTimer); knTimer = setTimeout(knLoad, 300) }
});
document.addEventListener("change", e => {
  if (e.target.id === "kn-th") { knTheme = e.target.value; knLoad() }
  if (e.target.id === "kn-cf") { knConf = e.target.value; knLoad() }
});
