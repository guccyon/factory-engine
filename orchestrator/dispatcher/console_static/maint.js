// 工場の保守（ADR 0059、#477）。提供元が直す工場の側の件を 1 か所に集める：工場の不調・建設者宛ての件・見回りの結果・最近うまくいかなかったジョブ
// 建設者宛てのお知らせ（工場の不調と同じもの）は不調の欄と重なるので、建設者宛ての欄は判断の件だけ
// ほかのページ（ホーム・ライン）には件数とここへのリンクだけを残す
// console.js の esc・put・dur・ibHref・alertsView・alLog・S を使う（console.js の後に読む）。記録の表示・積み直しは不調と同じボタン（data-allog・data-rerun）
let mtData = null, mtAt = 0;
async function mtLoad() {
  mtAt = Date.now();
  try { mtData = await (await fetch("/api/maint")).json() } catch (e) { mtData = {error: true} }
  if (route()[0] === "maint" && S) mtRender(S);
}
function mtSkeleton() {
  return `<div class="mt"><h1 class="t-heading">工場の保守</h1><p class="m">提供元（建設者）が直す工場の側の件です。事業主が判断するものは受信トレイに出ます</p>
    <div id="mt-tiles"></div><div id="mt-alerts"></div><section class="card"><h2>建設者宛ての件</h2><div id="mt-builder"></div></section>
    <section class="card"><h2>見回り</h2><div id="mt-patrols"><div class="m">読み込み中…</div></div></section>
    <section class="card"><h2>事故の台帳</h2><div id="mt-inc"></div></section>
    <section class="card"><h2>うまくいかなかったジョブ <span class="m" id="mt-jh"></span></h2><div id="mt-jobs"></div></section></div>`;
}
const mtBuilder = s => ((s.inbox || {}).items || []).filter(i => i.who === "builder" && !i.answered && i.kind !== "notice");
function mtCounts(s) {
  const al = ((s.alerts || {}).open || []).filter(a => !a.inbox).length, b = mtBuilder(s).length, d = mtData && !mtData.error ? mtData : null;
  return {al, b, late: d ? d.patrols.filter(p => p.overdue).length : 0, inc: d ? d.incidents.length : 0, jobs: d ? d.jobs.length : 0};
}
function mtRender(s) {
  if (!mtData || Date.now() - mtAt > 30000) { if (!mtData || !mtLoad._p) { mtLoad._p = mtLoad().finally(() => mtLoad._p = null) } }
  const n = mtCounts(s), t = (k, v, cls) => `<div class="c-tile${v && cls ? " " + cls : ""}"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  put("mt-tiles", `<div class="c-tiles">${t("工場の不調", n.al, "is-warn")}${t("建設者宛て", n.b, "is-warn")}${t("期限を過ぎた見回り", n.late, "is-bad")}${t("開いている事故", n.inc, "is-warn")}${t("うまくいかなかったジョブ", n.jobs)}</div>`);
  put("mt-alerts", alertsView(s.alerts) || '<section class="card"><h2>工場の不調</h2><div class="m">いまはない</div></section>');
  const b = mtBuilder(s);
  put("mt-builder", b.length ? `<ul class="c-list">${b.map(i => `<li><a class="c-g" href="${ibHref(i.key)}" data-nav>${esc(i.title)}</a><span class="c-m">${esc(((s.inbox || {}).kinds || {})[i.kind] || i.kind)}</span></li>`).join("")}</ul>` : '<div class="m">いまはない</div>');
  if (!mtData) return;
  if (mtData.error) { put("mt-patrols", '<div class="m">読み込めませんでした</div>'); return }
  // 動かしている見回りを上に（期限を過ぎたものが先頭）、止めているものは名前だけ 1 行に
  const on = mtData.patrols.filter(p => p.enabled).sort((a, b) => b.overdue - a.overdue), off = mtData.patrols.filter(p => !p.enabled);
  put("mt-patrols", `${on.length ? `<ul class="c-list">${on.map(p => `<li><span class="c-status ${p.overdue ? "is-bad" : "is-ok"}"></span><span class="c-g">${esc(p.name)}</span>
    <span class="c-m">${p.overdue ? "期限を過ぎた · " : ""}${p.age_sec != null ? dur(p.age_sec) + "前に動いた" : "まだ動いていない"}</span></li>`).join("")}</ul>` : '<div class="m">動かしている見回りはない（指示役の合図に宣言がない）</div>'}
    ${off.length ? `<div class="m">止めている見回り: ${off.map(p => esc(p.name)).join("・")}</div>` : ""}
    ${mtData.patrolled ? "" : '<div class="m">今日の事故の日次の見回りはまだ</div>'}`);
  const inc = mtData.incidents, sg = mtData.signs;
  put("mt-inc", (inc.length ? `<ul class="c-list">${inc.map(c => `<li><span class="c-status ${c.severity === "high" || c.severity === "critical" ? "is-bad" : "is-warn"}">${esc(c.severity || "")}</span><span class="c-g">${esc(c.id)} ${esc(c.title || "")}</span><span class="c-m">${esc(c.resource || "")}</span></li>`).join("")}</ul>` : '<div class="m">開いている事故はない</div>')
    + (sg.length ? `<div class="m">見回りの兆し ${sg.length} 件（直近 30 時間）</div>` : "") + (mtData.outbox ? `<div class="m">送り出し待ちの報告 ${mtData.outbox} 件</div>` : ""));
  put("mt-jh", `直近 ${mtData.jobs_hours} 時間`);
  if (force || !busy("mt-jobs")) put("mt-jobs", mtData.jobs.length ? mtData.jobs.map(j => `<div class="mt-job"><div class="mt-jh"><span class="c-status ${j.state === "failed" ? "is-bad" : ""}">${j.state === "failed" ? "失敗" : "取り消し"}</span>
    <span class="c-g">${esc(j.repo)}#${j.issue_number} · ${esc(j.kind)}${j.attempt > 1 ? ` · ${j.attempt} 回目` : ""}</span><span class="c-m">${dur(j.age_sec)}前</span></div>
    ${j.error ? `<div class="m mt-err">${esc(j.error)}</div>` : ""}<div class="acts"><button data-allog="${j.id}">${alLog[j.id] != null ? "記録を閉じる" : "作業の記録"}</button>${j.state === "failed" ? `<button data-rerun="${j.id}">積み直す</button>` : ""}</div>
    ${alLog[j.id] != null ? `<pre class="al-log">${esc(alLog[j.id])}</pre>` : ""}</div>`).join("") : '<div class="m">いまはない</div>');
}
