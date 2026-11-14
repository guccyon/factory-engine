// 問い合わせのページ（ADR 0059、#474）。人から事業部への入口。窓口の相談（desk.py）を、MCP・API から出したものと同じ一覧で見る。
// /ask 一覧、/ask/new 新しい依頼（送る前に似た依頼を出す）、/ask/<id> 依頼の詳細（やりとり・今のステップ・下書きの確認・受け取り）
// console.js の esc・put・post・go・toast・busy・route・S・TOKEN を使う（console.js の後に読む）
const ASK_STATE = {open: ["振り分け中", "is-info"], waiting_requester: ["返事待ち", "is-warn"], drafted: ["下書きの確認待ち", "is-warn"],
  confirmed: ["起票中", "is-info"], routed: ["振り分け済", "is-info"], awaiting_confirm: ["実行の確認待ち", "is-warn"], running: ["対応中", "is-info"],
  answered: ["回答済", "is-ok"], delivered: ["受け取り待ち", "is-warn"], accepted: ["完了", "is-ok"], handed_off: ["チケットにした", "is-ok"], closed: ["閉じた", ""]};
const ASK_ENDED = ["closed", "accepted", "handed_off"];
let askList = null, askAt = 0, askDetail = null, askDetailOf = null, askProd = "", askEnded = false;
const askDraft = {kind: "request", product: "", body: "", level: "", visibility: "all"};
const askTime = t => t ? String(t).slice(5, 16).replace("T", " ").replace("-", "/") : "";

function askProducts() {
  const xs = new Set([...(S.products || []).map(p => p.product), ...(S.releases || []).map(r => r.repo), ...(askList || []).map(c => c.product)]);
  return [...xs].filter(Boolean).sort();
}
async function askLoad(id) {
  askAt = Date.now();
  try {
    if (id) { askDetailOf = id; askDetail = await (await fetch("/api/ask/" + id)).json(); }
    askList = (await (await fetch("/api/ask")).json()).conversations || [];
  } catch (e) { toast("問い合わせを読み込めませんでした"); return }
  askPaint();
}
function askSkeleton(arg) {
  return `<div class="ask"><div class="ask-h"><h1 class="t-heading">問い合わせ</h1>${arg === "new" ? "" : '<a class="c-btn is-primary" href="/ask/new" data-nav>新しい依頼</a>'}</div><div id="ask-b"><div class="m">読み込み中…</div></div></div>`;
}
function askRender(arg) {
  const id = /^\d+$/.test(arg) ? arg : "";
  if (!askList || Date.now() - askAt > 15000 || (id && askDetailOf !== id)) askLoad(id); else askPaint();
}
function askPaint() {
  const arg = route()[1];
  if (route()[0] !== "ask") return;
  if (arg === "new") { if (!document.getElementById("ask-new")) put("ask-b", askNewView()); askSimilar(); return }
  if (/^\d+$/.test(arg)) { if (askDetail && askDetailOf === arg && !busy("ask-b")) put("ask-b", askDetailView(askDetail)); return }
  put("ask-b", askListView());
}
function askRow(c) {
  const [st, cls] = ASK_STATE[c.state] || [c.state, ""];
  return `<li><a href="/ask/${c.id}" data-nav class="ask-r"><span class="c-status ${cls}">${esc(st)}</span><span class="c-g">${esc(c.title || "（題なし）")}</span>
    <span class="c-m">${esc(c.product)} · ${c.kind === "inquiry" ? "問い合わせ" : "依頼"} · ${esc(c.opened_by)} · ${askTime(c.updated_at)}</span></a></li>`;
}
function askListView() {
  const ps = askProducts(), xs = (askList || []).filter(c => (!askProd || c.product === askProd) && ASK_ENDED.includes(c.state) === askEnded);
  const n = (askList || []).filter(c => (!askProd || c.product === askProd) && !ASK_ENDED.includes(c.state)).length;
  return `<div class="ask-f"><div class="tabs"><button data-askended="" class="${askEnded ? "" : "on"}">進行中 ${n}</button><button data-askended="1" class="${askEnded ? "on" : ""}">終わった件</button></div>
    <label class="m">製品 <select id="ask-prod"><option value="">すべて</option>${ps.map(p => `<option ${p === askProd ? "selected" : ""}>${esc(p)}</option>`).join("")}</select></label></div>
    <section class="card">${xs.length ? `<ul class="c-list">${xs.map(askRow).join("")}</ul>`
      : `<div class="c-empty"><b>${askEnded ? "終わった件はありません" : "進行中の依頼はありません"}</b><span>依頼は「新しい依頼」か、Claude（窓口の MCP）から出せます。どちらもここに並びます</span></div>`}</section>`;
}
// 新しい依頼。送る前に、同じ製品の似た依頼を出して重ねて出すのを避ける
function askNewView() {
  const ps = askProducts(), d = askDraft;
  if (!d.product && ps.length) d.product = ps[0];
  const r = (n, v, t) => `<label class="ask-o"><input type="radio" name="${n}" value="${v}" ${d[n] === v ? "checked" : ""}> ${t}</label>`;
  return `<form id="ask-new" class="card ask-form">
    <div class="ask-row"><span class="m">種類</span>${r("kind", "request", "依頼（作業を頼む）")}${r("kind", "inquiry", "問い合わせ（調べてもらう・聞く）")}</div>
    <label class="ask-row"><span class="m">製品</span><select name="product">${ps.map(p => `<option ${p === d.product ? "selected" : ""}>${esc(p)}</option>`).join("")}</select></label>
    <label class="ask-col"><span class="m">中身（1 行目が題になります）</span><textarea name="body" rows="6" maxlength="4000" placeholder="例: 予約の確認メールに教室の住所を入れてほしい">${esc(d.body)}</textarea></label>
    <div id="ask-sim"></div>
    <div class="ask-row"><span class="m">希望の優先度</span><select name="level"><option value="">おまかせ</option>${["p0", "p1", "p2", "p3"].map(l => `<option ${l === d.level ? "selected" : ""}>${l}</option>`).join("")}</select></div>
    <div class="ask-row"><span class="m">見える人</span>${r("visibility", "all", "全員")}${r("visibility", "me", "自分だけ")}</div>
    <div class="ask-act"><a class="c-btn" href="/ask" data-nav>やめる</a><button class="c-btn is-primary" type="submit">送る</button></div></form>`;
}
const bigrams = s => { const t = String(s || "").toLowerCase().replace(/\s+/g, ""), out = new Set(); for (let i = 0; i < t.length - 1; i++) out.add(t.slice(i, i + 2)); return out };
function askLike(a, b) { const x = bigrams(a), y = bigrams(b); if (!x.size || !y.size) return 0; let n = 0; x.forEach(g => { if (y.has(g)) n++ }); return n / Math.min(x.size, y.size) }
function askSimilar() {
  const d = askDraft, el = document.getElementById("ask-sim"); if (!el) return;
  const xs = d.body.trim().length < 4 ? [] : (askList || []).filter(c => c.product === d.product).map(c => [askLike(d.body.split("\n")[0], c.title), c])
    .filter(([s]) => s >= 0.35).sort((a, b) => b[0] - a[0]).slice(0, 3);
  put("ask-sim", xs.length ? `<div class="c-notice is-warn"><span class="c-g">似た依頼があります。同じなら、そちらに言い足してください<ul class="c-list">${xs.map(([, c]) => askRow(c)).join("")}</ul></span></div>` : "");
}
document.addEventListener("input", e => {
  const f = e.target.closest("#ask-new"); if (!f) return;
  askDraft[e.target.name] = e.target.value; if (e.target.name === "body" || e.target.name === "product") askSimilar();
});
document.addEventListener("change", e => {
  if (e.target.closest("#ask-new")) { askDraft[e.target.name] = e.target.value; askSimilar(); return }
  if (e.target.id === "ask-prod") { askProd = e.target.value; askPaint() }
});
document.addEventListener("click", e => { const b = e.target.closest("button[data-askended]"); if (b) { askEnded = !!b.dataset.askended; askPaint() } });
document.addEventListener("submit", async e => {
  if (e.target.id !== "ask-new") return; e.preventDefault();
  const d = askDraft; if (!d.body.trim()) { toast("中身を書いてください"); return }
  const res = await fetch("/api/ask", {method: "POST", headers: {"Content-Type": "application/json", "X-Console-Token": TOKEN},
    body: JSON.stringify({kind: d.kind, product: d.product, body: d.body, level: d.level || null, visibility: d.visibility})});
  const r = await res.json().catch(() => ({}));
  if (!res.ok) { toast("送れませんでした: " + (r.error || res.status)); return }
  toast("送りました"); Object.assign(askDraft, {body: "", level: ""}); askAt = 0; go("/ask/" + r.conversation.id)
});
// 詳細。答えの欄（返事・受け取り・下書きの確認）は画面の下に止める
const ASK_ROLE = {requester: "依頼者", factory: "事業部", desk: "相談役"};
function askMsg(m) {
  if (m.event !== "desk.message" || !m.body) return "";
  const me = m.role === "requester";
  return `<div class="ask-m ${me ? "me" : ""}"><div class="m">${esc(me ? m.author : ASK_ROLE[m.role] || m.author || "")} · ${askTime(m.at)}</div><div class="b">${esc(m.body)}</div></div>`;
}
function askDetailView(r) {
  if (!r || r.error) return `<div class="c-empty"><b>依頼が見つかりません</b><span><a href="/ask" data-nav>一覧に戻る</a></span></div>`;
  const c = r.conversation, s = r.status || {}, [st, cls] = ASK_STATE[c.state] || [c.state, ""], ended = ASK_ENDED.includes(c.state);
  const vis = (c.wanted || {}).visibility === "me" ? "自分だけ" : "全員";
  const draft = r.draft && c.state === "drafted" ? `<div class="c-notice"><span class="c-g"><b>下書き</b> ${esc(r.draft.summary || r.draft.title || "")}${(r.draft.tasks || []).length ? `<ul>${r.draft.tasks.map(t => `<li>${esc(t.title || "")}</li>`).join("")}</ul>` : ""}
    <button class="c-btn is-primary is-small" data-askconfirm="${r.draft.draft_id}">この下書きで投入する</button></span></div>` : "";
  const ans = s.answer ? `<div class="ask-act">${s.answer.options.map(o => `<button class="c-btn ${o.id === "run" || o.id === "accept" ? "is-primary" : ""}" data-askans="${esc(o.id)}" data-how="${c.state === "awaiting_confirm" ? "confirm" : "messages"}">${esc(o.label)}</button>`).join("")}</div>` : "";
  const reply = ended ? "" : `<div class="ans dock ask-reply"><textarea id="ask-txt" rows="2" maxlength="4000" placeholder="返事・言い足し（差し戻し・続きを頼むときは理由も）"></textarea>
    <div class="ask-act">${ans}<button class="c-btn" data-asksend>送る</button></div></div>`;
  return `<div class="ask-d card"><div class="ask-dh"><a href="/ask" data-nav class="m">← 一覧</a><h2>${esc(c.title)}</h2>
    <div class="m">${esc(c.product)} · ${c.kind === "inquiry" ? "問い合わせ" : "依頼"} · ${esc(c.opened_by)} · 見える人: ${vis}</div>
    <div class="ask-st"><span class="c-status ${cls}">${esc(st)}</span> <span class="m">${esc(s.step || "")}${s.next ? " · " + esc(s.next) : ""}</span></div></div>
    ${(r.tickets || []).length ? `<div class="m">チケット: ${r.tickets.map(t => esc(t.source_ref || "#" + t.id) + "（" + esc(t.state) + "）").join("・")}</div>` : ""}
    ${s.delivered && s.delivered.summary ? `<div class="c-notice is-info"><span class="c-g"><b>提出</b> ${esc(s.delivered.summary)}</span></div>` : ""}
    ${draft}<div class="ask-ms">${(r.messages || []).map(askMsg).join("")}</div>${reply}
    ${ended ? "" : `<div class="ask-close"><button class="c-btn is-small" data-askclose>取り下げる</button></div>`}</div>`;
}
async function askAct(path, body) { if (await post(path, body)) { const t = document.getElementById("ask-txt"); if (t) t.value = ""; askLoad(askDetailOf) } }
document.addEventListener("click", e => {
  const id = askDetailOf, t = () => (document.getElementById("ask-txt") || {}).value || "";
  if (e.target.closest("[data-asksend]")) { if (!t().trim()) { toast("返事を書いてください"); return } askAct(`/api/ask/${id}/messages`, {body: t()}) }
  const a = e.target.closest("[data-askans]");
  if (a) { const o = a.dataset.askans; if ((o === "more" || o === "return") && !t().trim()) { toast("理由を書いてから押してください"); return }
    askAct(`/api/ask/${id}/${a.dataset.how}`, a.dataset.how === "confirm" ? {answer: o, note: t()} : {answer: o, body: t()}) }
  const c = e.target.closest("[data-askconfirm]"); if (c) askAct(`/api/ask/${id}/confirm`, {draft_id: +c.dataset.askconfirm});
  if (e.target.closest("[data-askclose]")) askAct(`/api/ask/${id}/close`, {reason: "withdrawn"});
});
