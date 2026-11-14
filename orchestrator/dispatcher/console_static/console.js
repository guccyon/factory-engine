const TOKEN=document.querySelector('meta[name="console-token"]').content;
const LANES=[["human","対応が必要"],["running","作業中"],["queued","順番待ち"],["ready","投入待ち（上限）"],["waiting","依存待ち"],["review","検査・判定・マージ待ち"],["done","今日の済み"]];
let levels={};
const CAT={env:"実行環境",perm:"権限",human:"判断待ち","factory-bug":"工場の不具合",ticket:"チケット",external:"外部",budget:"予算・上限",idle:"ラインが空"};
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const dur=s=>s==null?"":s<60?s+"秒":s<3600?Math.floor(s/60)+"分":Math.floor(s/3600)+"時間"+Math.floor(s%3600/60)+"分";
const link=(u,t)=>u&&/^https:\/\//.test(u)?`<a href="${esc(u)}" target="_blank" rel="noopener">${esc(t)}</a>`:esc(t);
function toast(t){const m=document.getElementById("msg");m.textContent=t;m.style.display="block";setTimeout(()=>m.style.display="none",3000)}
async function post(path,body){const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-Console-Token":TOKEN},body:JSON.stringify(body||{})});
 const j=await r.json().catch(()=>({}));toast(r.ok?"受け付けました":"失敗: "+(j.error||r.status));if(r.ok&&(path.startsWith("/api/choices/")||path==="/api/inbox/answer"||path==="/api/budget"||path==="/api/factory/mode"))force=true;load();return r.ok}
function acts(i){let h="";
 if(i.ticket_id&&["waiting","ready","queued","running"].includes(i.stage)){
  h+=`<select data-t="${i.ticket_id}" aria-label="優先度">`+Object.entries(levels).map(([k,v])=>`<option value="${k}" ${k===i.level?"selected":""}>${k} ${esc(v.name)}</option>`).join("")+`</select>`;
  if(i.stage!=="running")h+=i.held?`<button data-a="resume" data-t="${i.ticket_id}">再開</button>`:`<button data-a="hold" data-t="${i.ticket_id}">保留</button>`}
 if(i.job_id&&["queued","running"].includes(i.stage)&&!i.stopping)h+=`<button class="danger" data-a="stop" data-j="${i.job_id}">止める</button>`;
 return h?`<div class="acts">${h}</div>`:""}
const VER=r=>r?esc(r.split("@").pop()):"なし";
function shipView(rs){return rs.map(r=>{const st=r.environments.staging||{},pr=r.environments.prod||{};
 const busy=r.pending.length?`<span class="m">処理中: ${r.pending.map(p=>({ship_requested:"staging へ出荷",approved:"本番へ出荷",rollback_requested:"前の版へ戻す",reseed_requested:"シードから入れ直す",restart_requested:"再起動（時計を進める）"}[p.request]||p.request)).join("・")}</span>`:"";
 let b="";if(!r.pending.length){
  if(r.prod_held)b+=`<span class="m">本番には出さない（段階: ${esc(r.stage.stage)}＝${esc(r.stage.ja)}。staging だけ動かす）</span>`;
  else if(st.release_id&&st.release_id!==pr.release_id)b+=`<button class="go" data-ship="approve" data-r="${esc(st.release_id)}">本番に出す（${VER(st.release_id)}）</button>`;
  if(!r.prod_held&&pr.previous&&!pr.rollback_blocked)b+=`<button class="danger" data-ship="rollback" data-repo="${esc(r.repo)}">本番を前の版に戻す（${VER(pr.previous)} へ）</button>`;
  if(pr.rollback_blocked)b+=`<span class="m">戻せない: ${esc(pr.rollback_blocked)}</span>`;
  if(r.touch)b+=`<button class="danger" data-ship="reseed" data-repo="${esc(r.repo)}">検証の DB をシードから入れ直す</button>`}
 const t=r.touch,tv=t?`<div class="touch"><span class="m">触れる環境 ${VER(t.release_id)}${t.reachable_from_phone?"":"（この機械からだけ開ける。SHIP_TOUCH_HOST・SHIP_TOUCH_BIND が未設定）"}</span>${(t.entries||[]).map(e=>`<div class="e"><div>${esc(e.label)}</div><a href="${/^https?:\/\//.test(e.url)?esc(e.url):"#"}" target="_blank" rel="noopener">${esc(e.url)}</a>${e.qr_svg||""}</div>`).join("")}${(t.accounts||[]).length?`<div class="e"><div>ログインの手がかり</div>${t.accounts.map(a=>`<div class="m">${esc(a.label)}</div><code>${esc(a.value)}</code>`).join("")}</div>`:""}${t.note?`<span class="m">${esc(t.note)}</span>`:""}</div>`:"";
 const l=r.last;const last=l?`<span class="m">直近: ${esc({"release.deployed":"出荷","release.rolled_back":"戻した","release.built":"イメージを作った","release.reseeded":"シードから入れ直した","release.restarted":"再起動した","release.ship_failed":"失敗"}[l.name]||l.name)} ${VER(l.release_id)}${l.env?" "+esc({staging:"検証",prod:"本番"}[l.env]||l.env):""}${l.seconds!=null?" "+l.seconds+" 秒":""}${l.error?" — "+esc(l.error).slice(0,120):""}</span>`:"";
 return `<div class="rel"><span class="t">${esc(r.repo)}</span><span class="env">検証 <b>${VER(st.release_id)}</b></span><span class="env">本番 <b>${VER(pr.release_id)}</b></span>${busy}${b}${last}${tv}</div>`}).join("")}
function pairsView(ps){return ps.map(p=>`<div class="rn" style="border-color:var(--accent)"><h2>新しいランナーの接続の依頼</h2><div><b>${esc(p.name||"?")}</b> <span class="m">${esc(p.platform||"")} · ${esc(p.version||"")}</span></div>
 <div style="font-size:28px;font-weight:700;letter-spacing:.08em;margin:6px 0">${esc(p.code)}</div><div class="m">Mac のターミナルに同じ番号が出ていれば「許可」を押し、Mac 側でも y を押す。違えば「拒否」。10 分で切れる</div>
 <div class="acts"><button class="go" data-pair="approve" data-p="${esc(p.pair_id)}">許可</button><button class="danger" data-pair="reject" data-p="${esc(p.pair_id)}">拒否</button></div></div>`).join("")}
function runnersView(rs){if(!rs.length)return"";
 return `<div class="rn"><h2>登録済みのランナー（工場の外の機械に入れた常駐プロセス）</h2><table><tr><th>機械</th><th>状態</th><th>版・道具</th><th>置き場</th><th></th></tr>${rs.map(r=>`<tr>
  <td><b>${esc(r.name||"?")}</b><div class="m">${esc(r.kind||"")} ${esc(r.platform||"")}</div></td>
  <td><span class="dot ${r.online?"on":""}"></span>${r.uninstall_pending?"外す依頼の処理待ち":r.online?"動いている":r.last_seen_age_sec==null?"連絡待ち":"連絡なし（"+dur(r.last_seen_age_sec)+"前）"}<div class="m">登録 ${esc((r.registered_at||"").slice(0,16).replace("T"," "))}</div></td>
  <td>${esc(r.version||"")}<div class="m">${esc(r.tools||"")}${r.devices?" · 端末 "+esc(r.devices)+" 台":""}</div></td>
  <td><code>${esc(r.path||"")}</code><div class="m">常駐 ${esc(r.label||"")}${r.xcode?" · "+esc(r.xcode):""}</div><div class="m">できること: ${esc(r.scope||"")}</div><div class="m">外しても残るもの: <code>${esc(r.home||"")}</code>・<code>${esc(r.logs||"")}</code>・トークン</div></td>
  <td>${r.uninstall_pending?"":`<button class="danger" data-runner="${esc(r.id)}">外す</button>`}</td></tr>`).join("")}</table>
  <div class="m">外すと工場とランナーの鍵を捨て、ランナーが自分の常駐（LaunchAgent）を外して止まる。手で外すときはその機械で <code>scripts/macrunner.sh uninstall</code></div></div>`}
const WHO_ORDER=["owner","po","design_chief","builder","requester"];
// 受信トレイの 1 件の答え方（ADR 0034 の決定 2）。押したら何が起きるか（effect）を必ず出す。取り返しのつかない作用は 2 度押し
function answerView(i,n){const a=i.answer;
 if(i.answered){const w=i.answered;return `<div class="ans done">回答済: ${esc(w.by||"?")}（${esc({console:"操業コンソール",review_page:"レビューページ",factoryctl:"factoryctl",mcp:"Claude",github:"GitHub",slack:"Slack"}[w.via]||w.via||"")}）が『${esc(w.label||w.option||"")}』と回答。反映を待っている</div>`}
 if(!a)return "";
 const agg=i.key==="choice:exit_capture";   // まとめた実装後の画面: 印は画面ごとに下の欄で付ける
 const k=esc(i.key),note=!agg&&(a.extra.length||a.type==="ack")?`<input data-note="${n}" placeholder="${a.note_hint?esc(a.note_hint):a.extra.some(e=>e.id==="return")?"コメント（理由・違和感。戻す・違和感では必須）":"コメント（任意。違和感では必須）"}" maxlength="300">`:"";
 const opts=a.options.map(o=>`<button class="${o.id===a.recommended||a.type!=="choose"?"go":""}" data-ib="${k}" data-o="${esc(o.id)}" data-n="${n}"${a.irreversible?' data-irrev="1"':""}>${a.type==="choose"&&o.id.length<=2?esc(o.id)+". ":""}${esc(o.label)}${o.id===a.recommended?"（推奨）":""}</button>`).join("");
 const extra=a.extra.map(e=>`<button class="${e.id==="return"?"danger":""}" data-ib="${k}" data-o="${esc(e.id)}" data-n="${n}" data-need="${e.note_required?1:""}">${esc(e.label)}</button>`).join("");
 return `<div class="ans dock"><div class="m">答えると: ${esc(a.effect)}${a.irreversible?"（取り返しがつかない。2 度押しで確定）":""}</div><div class="acts">${agg?``:""}${opts}${extra}${note}</div></div>`}
// 問いの中身（judge が人に回した判断の今の案・他の案・見立てなど）。答える前に読めるように件の下に出す
function detailView(i){return i.detail&&i.detail.length?`<dl class="dt">${i.detail.map(d=>`<dt>${esc(d.label)}</dt><dd>${esc(d.text)}</dd>`).join("")}</dl>`:""}
const KIND={design_variants:"画面の体験の案の選択",design_look:"画面の見た目の案の選択",concept_tone:"コンセプトとトーン＆マナーの選択",taste_sample:"抜き取り",exit_capture:"実装後の画面"};
function card(i){const lv=i.level||"p2";let meta=[srcLink(i),i.pr_url?link(i.pr_url,"PR"):""];
 if(i.job_id)meta.push(`ジョブ ${i.job_id}${i.kind&&i.kind!=="implement"?"（"+esc(i.kind)+"）":""}${i.attempt>1?" 試行"+i.attempt:""}`);
 if(i.rank)meta.push(`${i.rank} 番目`);if(i.held)meta.push("保留中");
 if(i.stage==="running")meta.push(`経過 ${dur(i.elapsed_sec)} / 打ち切り ${dur(i.timeout_sec)}`+(i.stopping?(i.preempted_for?" 割り込みで停止中":" 停止中"):""));
 if(i.waiting_on&&i.waiting_on.length)meta.push("待っている: "+i.waiting_on.map(esc).join(" "));
 if(i.note)meta.push(esc(i.note));
 return `<div class="it ${lv}${i.held?" held":""}"><div class="t">${i.priority!=null?`<span class="badge ${lv}">${esc(lv)}</span>`:""}${esc(i.title||"（題名なし）")}</div><div class="m">${meta.filter(Boolean).join(" · ")}</div>${acts(i)}</div>`}
function scaleView(sc,l){if(!sc)return"";const d=sc.desired,r=sc.running,n=(v)=>v==null?"?":v,paused=l&&l.draining;
 const who=sc.at?`${esc(sc.by||"")} が ${esc(String(sc.at).slice(0,16).replace("T"," "))} に宣言${sc.reason?"（"+esc(sc.reason)+"）":""}`:"環境変数の値（宣言なし）";
 return `<span class="stat ${paused?"bad":""}" title="流量（いま動いている数 / 上限）。ワーカー: 同時に動かす作業、チケット: 同時に手を付けるチケット（PR の検査・マージ待ちを含む。計画待ち・計画中は含まない）、委員会: 知見をまとめる係、窓口: 相談に答える係。上限は ${who}。変えるのは factoryctl line scale か POST /api/line/scale${paused?"。ラインは一時停止中なので、ワーカーの枠は取り出さない":""}">流量 ワーカー <b>${n(r.workers)} / ${d.workers}</b> · チケット <b>${n(r.tickets)} / ${d.tickets}</b> · 委員会 <b>${n(r.governance)} / ${d.governance}</b> · 窓口 <b>${n(r.desk)} / ${d.desk}</b></span>`}
function lineView(l){if(!l)return"";const w=[l.running.length?`実行中 ${l.running.length} 件の終わり待ち`:"",l.shipping.length?`出荷 ${l.shipping.length} 件の処理待ち`:""].filter(Boolean).join("・");
 return `<label class="stat sw ${l.draining&&!l.ready?"bad":""}" title="オフにすると順番待ちを取り出さない（実行中のジョブは最後まで流す）。工場の更新のとき">ライン <input type="checkbox" role="switch" id="line" ${l.draining?"":"checked"}><b class="${l.draining?"":"on"}">${l.draining?(l.ready?"一時停止中（更新してよい）":"一時停止中（"+w+"）"):"流している"}</b></label>`}
function holdView(h){if(!h)return"";const xs=(h.incidents||[]).map(i=>`${esc(String(i.severity||"").toUpperCase())} ${esc(i.title||"")}${i.drill?"（演習）":""}`).join("・");
 return `<span class="stat bad" title="QA 役が S1 の Incident でラインを差し止めている（ADR 0043 の O5）。S1 の修正（p0）と QA の確かめ直しだけ流れる。解除は手動だけ">QA が差し止め中 <b>${xs}</b> <button data-hold="release">差し止めを解除</button></span>`}
const put=(id,h)=>{const e=document.getElementById(id);if(e&&e._h!==h){e._h=h;e.innerHTML=h}};
document.addEventListener("change",e=>{const t=e.target;if(t.id==="keepawake")return post("/api/host/keep-awake",{prevent_sleep:t.checked});if(t.id==="line")return post(t.checked?"/api/factory/resume":"/api/factory/drain",{reason:"操業コンソール"});if(t.matches("select[data-t]"))post(`/api/tickets/${t.dataset.t}/priority`,{level:t.value})});
document.addEventListener("click",e=>{const b=e.target.closest("button[data-hold]");if(!b)return;
 const r=(prompt("解除の理由（直した・演習を終えた など）")||"").trim();if(!r)return;post("/api/line/release-hold",{reason:r})});
document.addEventListener("click",e=>{const s=e.target.closest("button[data-ship]");if(!s)return;
 if(!s.classList.contains("armed")){s.dataset.label=s.textContent;s.classList.add("armed");s.textContent="もう一度押すと実行";setTimeout(()=>{s.classList.remove("armed");s.textContent=s.dataset.label},4000);return}
 if(s.dataset.ship==="approve")post("/api/releases/approve",{release_id:s.dataset.r}).then(ok=>ok&&advance("release:"+s.dataset.r));else if(s.dataset.ship==="reseed")post("/api/releases/reseed",{repo:s.dataset.repo});else post("/api/releases/rollback",{repo:s.dataset.repo,env:"prod"})});
document.addEventListener("click",e=>{const b=e.target.closest("button[data-ib]");if(!b)return;
 const note=((document.querySelector(`input[data-note="${b.dataset.n}"]`)||{}).value||"").trim();
 if(b.dataset.need&&!note)return toast("コメントを入れてください");
 if(b.dataset.irrev&&!b.classList.contains("armed")){b.dataset.label=b.textContent;b.classList.add("armed");b.textContent="もう一度押すと確定";setTimeout(()=>{b.classList.remove("armed");b.textContent=b.dataset.label},4000);return}
 post("/api/inbox/answer",{key:b.dataset.ib,option:b.dataset.o,note}).then(ok=>{if(ok&&b.dataset.o!=="flag")advance(b.dataset.ib)})});
document.addEventListener("click",e=>{const b=e.target.closest("button[data-pair]");if(!b)return;post(`/api/runners/pairs/${b.dataset.p}/${b.dataset.pair}`,{reason:"操業コンソール"})});
document.addEventListener("click",e=>{const b=e.target.closest("button[data-runner]");if(!b)return;
 if(!b.classList.contains("armed")){b.classList.add("armed");b.textContent="もう一度押すと外す";setTimeout(()=>{b.classList.remove("armed");b.textContent="外す"},4000);return}
 post(`/api/runners/${b.dataset.runner}/uninstall`,{reason:"操業コンソール"})});
document.addEventListener("keydown",e=>{const t=e.target;if(e.key!=="Enter"||e.isComposing||e.keyCode===229||!t.matches("input[data-fnote]"))return;   // 229: Safari は変換の確定の Enter で isComposing を立てない（11-06「テーブルの」だけの印が 2 件送られた）e.preventDefault();
 const b=document.querySelector(`button[data-ch=flag][data-c="${t.dataset.fnote}"][data-o="${t.dataset.o}"]`);if(b)b.click()});
document.addEventListener("click",e=>{const b=e.target.closest("button[data-ch]");if(!b)return;const id=b.dataset.c;
 const fin=b.dataset.o?document.querySelector(`input[data-fnote="${id}"][data-o="${b.dataset.o}"]`):null;   // 実装後の画面は画面ごとの欄
 const note=((fin||document.getElementById("note-"+id)||{}).value)||"";
 if(b.dataset.ch==="seen")return post(`/api/choices/${id}/seen`,{reason:note}).then(ok=>ok&&advance("choice:"+id));
 if(b.dataset.ch==="choose")return post(`/api/choices/${id}/choose`,{option:b.dataset.o,reason:note}).then(ok=>ok&&advance("choice:"+id));
 if(b.dataset.ch==="flag"){if(!note.trim()){if(fin)fin.focus();return toast("違和感のコメントを入れてください")}const vs=b.dataset.o?document.querySelector(`select[data-fview="${id}"][data-o="${b.dataset.o}"]`):null;return post(`/api/choices/${id}/flag`,{option:b.dataset.o,note,viewpoint:(vs&&vs.value)||null}).then(ok=>{if(ok&&fin){fin.value="";fin.placeholder="送りました。ほかにもあれば一言（Enter で送る）"}})}
 if(!note.trim())return toast("戻す理由をコメントに入れてください");post(`/api/choices/${id}/return`,{reason:note}).then(ok=>ok&&advance("choice:"+id))});
document.addEventListener("click",e=>{const b=e.target.closest("button[data-a]");if(!b)return;
 if(b.dataset.a==="stop"){if(!b.classList.contains("armed")){b.classList.add("armed");b.textContent="もう一度押すと止める";setTimeout(()=>{b.classList.remove("armed");b.textContent="止める"},4000);return}
  return post(`/api/jobs/${b.dataset.j}/stop`,{})}
 post(`/api/tickets/${b.dataset.t}/${b.dataset.a}`,{})});
// 画面の拡大（11-07）。画像を押すとその場で幅いっぱいに出す（縦長はスクロール）。押すと全体、← → で同じ問いの他の画面、Esc で閉じる。⌘/Ctrl＋クリックは別タブ
let zl=[],zi=0;const Z=document.getElementById("zoom"),ZV=()=>Z.querySelector(".vp");
function zshow(){const a=zl[zi];if(!a)return;const g=document.getElementById("zoom-img");g.src=a.src;g.alt=a.alt;
 document.getElementById("zoom-t").textContent=`${a.alt}（${zi+1} / ${zl.length}）`;document.getElementById("zoom-tab").href=a.src;ZV().scrollTop=0}
const zfit=on=>{Z.classList.toggle("fit",on);document.getElementById("zoom-fit").textContent=on?"幅に合わせる":"全体を見る"};
function zclose(){Z.hidden=true;zfit(false);document.getElementById("zoom-img").removeAttribute("src")}
const zstep=d=>{zi=(zi+d+zl.length)%zl.length;zshow()};
document.addEventListener("click",e=>{const a=e.target.closest("a.zm");if(!a||e.metaKey||e.ctrlKey||e.shiftKey||e.button)return;e.preventDefault();
 const box=a.closest("[data-zl]"),src=a.getAttribute("href"),alt=(a.querySelector("img")||{}).alt||"";
 zl=box?[...box.querySelectorAll("[data-zsrc]")].filter(x=>x.dataset.zsrc).map(x=>({src:x.dataset.zsrc,alt:x.dataset.zalt||""})):[];
 if(!zl.length)zl=[{src,alt}];zi=Math.max(zl.findIndex(x=>x.src===src),0);zfit(false);Z.hidden=false;zshow()});
Z.addEventListener("click",e=>{const b=e.target.closest("[data-zm]");
 if(e.target.id==="zoom-img"||(b&&b.dataset.zm==="fit"))return zfit(!Z.classList.contains("fit"));
 if(b&&b.dataset.zm==="close"||e.target.classList.contains("vp"))return zclose();
 if(b)zstep(Number(b.dataset.zm))});
document.addEventListener("keydown",e=>{if(Z.hidden)return;if(e.key==="Escape"){e.preventDefault();zclose()}else if(e.key==="ArrowRight"||e.key==="ArrowLeft"){e.preventDefault();zstep(e.key==="ArrowRight"?1:-1)}});
// ---------------------------------------------------------------- ページ（11-07 の構成。ADR 0009 の決定 5 の簡易画面）
// URL がページ: / ホーム（ダッシュボード）・/inbox と /inbox/<key>（受信トレイとメッセージの詳細の 2 ペイン）・/line・/ship・/settings/<項目>
const SRC={github:"GitHub",jira:"Jira",redmine:"Redmine",backlog:"Backlog",console:"コンソール"};   // チケットの出どころ（tickets.source）
const SETTINGS=[["runners","ランナー"],["mode","モード"],["line","ラインと流量"],["budget","使用量の予算"],["connectors","サービスとの連携"],["mac","Mac"],["version","工場の版"]];
const LATER=["credential","Claude からのアクセス（OAuth）","工場の更新"];   // 設定に足していく予定の項目（まだ中身はない）
let S=null,view="",gi={},ibTab="open",ibKind="",ibWho=null;   // ibWho が null のときはワークベンチの既定
const enc=k=>encodeURIComponent(k);
const ibHref=k=>"/inbox/"+enc(k);
function route(){const p=location.pathname.replace(/\/+$/,"")||"/";let m;
 if((m=p.match(/^\/inbox(?:\/(.+))?$/)))return["inbox",m[1]?decodeURIComponent(m[1]):""];
 if((m=p.match(/^\/ask(?:\/(new|\d+))?$/)))return["ask",m[1]||""];
 if(p==="/line"||p==="/ship"||p==="/usage"||p==="/live")return[p.slice(1),""];
 if((m=p.match(/^\/products(?:\/([\w.-]+\/[\w.-]+))?$/)))return["products",m[1]||""];
 if((m=p.match(/^\/help(?:\/(api|cli|mobile))?$/)))return["help",m[1]||""];
 if((m=p.match(/^\/settings(?:\/([a-z-]+))?$/)))return["settings",SETTINGS.some(x=>x[0]===m[1])?m[1]:"runners"];
 return["home",""]}
// 前の構成のリンク（通知や古いタブの /#choice-1・/#choices・/#ship）を新しいページに読み替える
(()=>{if(location.pathname!=="/"||!location.hash)return;const h=location.hash.slice(1);let to="";
 if(/^choice-\d+$/.test(h))to=ibHref("choice:"+h.slice(7));else if(h==="choices")to=ibHref("choice:exit_capture");else if(h==="ship")to="/ship";else if(h==="inbox")to="/inbox";
 if(to)history.replaceState(null,"",to)})();
function go(href){if(href!==location.pathname+location.hash)history.pushState(null,"",href);render();const h=href.split("#")[1],el=h&&document.getElementById(h);if(el)el.scrollIntoView({block:"start"})}
document.addEventListener("click",e=>{const a=e.target.closest("a[data-nav]");if(!a||e.metaKey||e.ctrlKey||e.shiftKey||e.button)return;e.preventDefault();go(a.getAttribute("href"))});
addEventListener("popstate",()=>render());
const ago=s=>s==null?"":dur(s)+"待ち";
const busy=id=>{const e=document.getElementById(id);return !!e&&(e.contains(document.activeElement)&&document.activeElement.matches("input,textarea")||[...e.querySelectorAll("input:not([type=checkbox]),textarea")].some(i=>i.value.trim()))};

// ---- ホーム
function todoView(ib){const xs=ib.items.filter(i=>!i.answered&&i.kind!=="notice");   // お知らせは判断待ちに数えない（#473）
 if(!xs.length)return `<h2>判断待ち<span class="m"> プロダクトの判断（受信トレイ）</span></h2><div class="m">いまはありません</div>`;
 return `<h2>判断待ち（${xs.length} 件）<span class="m"> プロダクトの判断（受信トレイ）</span></h2><ul class="rows">${xs.slice(0,5).map(i=>`<li><a href="${ibHref(i.key)}" data-nav><span class="who">${esc(ib.who[i.who]||i.who)}</span><span class="g">${esc(i.title)}</span><span class="m">${esc(ib.kinds[i.kind]||i.kind)} · ${ago(i.age_sec)}</span></a></li>`).join("")}</ul>
  <a class="more" href="/inbox" data-nav>受信トレイをすべて見る →</a>`}
// 製品の準備（ADR 0045 の追記）。稼働 ready を越えた製品は出さない。届いたレベルと次のレベル、いちばん弱い仕事の判定、お願いと確かめてほしい読みの件数
const VCLS={blocked:"bad",weak:"warn",ready:"ok",enough:"good"};
function prodCard(ps){const xs=(ps||[]).filter(p=>!p.past_ready);if(!xs.length)return"";
 return `<h2>製品の準備（${xs.length}）<span class="m"> オンボーディングのレベルと準備度</span></h2><ul class="rows">${xs.map(p=>`<li><a href="/products/${esc(p.product)}" data-nav><span class="who">L${p.reached} ${esc(p.reached_ja)}</span><span class="g">${esc(p.product)}${p.next?` <span class="m">→ L${p.next} ${esc(p.next_ja)}</span>`:""}</span><span class="m"><b class="v-${VCLS[p.weakest.verdict]}">${esc(p.weakest.target_ja)}: ${esc(p.weakest.verdict_ja)}</b> · お願い ${p.asks} 件${p.confirms?` · 確かめてほしい読み ${p.confirms} 件`:""}${p.ready_ok?" · 稼働 ready の承認待ち":""}</span></a></li>`).join("")}</ul>
  <a class="more" href="/products" data-nav>製品をすべて見る →</a>`}
function spark(series){const v=(series||[]).map(x=>typeof x==="number"?x:null),ns=v.filter(x=>x!=null);if(ns.length<2)return '<div class="m">推移はまだない</div>';
 const lo=Math.min(...ns),hi=Math.max(...ns),W=140,H=34,X=i=>(i*(W-4)/(v.length-1)+2).toFixed(1),Y=x=>(hi===lo?H/2:H-3-(x-lo)*(H-6)/(hi-lo)).toFixed(1);
 let d="",pen=false;v.forEach((x,i)=>{if(x==null){pen=false;return}d+=(pen?"L":"M")+X(i)+" "+Y(x)+" ";pen=true});
 const li=v.length-1,last=v[li];
 return `<svg class="spk" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true"><path d="${d}"/>${last!=null?`<circle cx="${X(li)}" cy="${Y(last)}" r="2.5"/>`:""}</svg>`}
const num=(x,u)=>x==null?"不明":(typeof x==="number"?(Math.round(x*10)/10).toLocaleString():esc(x))+(u?`<small>${esc(u)}</small>`:"");
function trendView(t){if(!t||!t.items.length)return `<h2>指標の推移</h2><div class="m">毎朝の操業レポート（工場の健康）がまだ組み立てられていない。組み立てられると、レポートが描いている指標の推移がここに出る</div>`;
 const span=t.days.length?`${esc(t.days[0])} 〜 ${esc(t.days[t.days.length-1])}`:"";
 return `<h2>指標の推移 <span class="m">毎朝の操業レポートと同じ数 · ${span}（${t.days.length} 日）</span></h2>
  ${t.summary.length?`<div class="m sumline ${esc(t.level)}">${t.summary.map(esc).join(" ")}</div>`:""}
  <div class="tiles">${t.items.map(i=>`<div class="tile ${esc(i.level)}"><div class="m">${esc(i.title)}</div><div class="v">${num(i.value,i.unit)}${i.delta!=null&&i.delta!==0?`<span class="d">${i.delta>0?"+":""}${Math.round(i.delta*10)/10}</span>`:""}</div>${spark(i.series)}</div>`).join("")}</div>`}
// ホーム（ADR 0059、#472）。並びはワークベンチごと。1 つの件は 1 か所で片付けるので、ホームは件数とリンクだけ（不調の中身はラインの上、のちに工場の保守）
const HOME_ORDER={factory:["notice","todo","stats","line","trend","prod","ship"],product:["prod","todo","ship","stats","trend","line","notice"]};
function homeStats(s){const f=s.factory,hum=s.items.filter(i=>i.stage==="human").length,run=s.items.filter(i=>i.stage==="running").length,low=f.line_low_below>0&&f.planned<f.line_low_below;
 const t=(k,v,href,cls,d)=>`<a class="c-tile${cls?" "+cls:""}" href="${href}" data-nav><div class="k">${k}</div><div class="v">${v}${d?`<span class="d">${d}</span>`:""}</div></a>`;
 return `<div class="c-tiles">${t("対応が必要な作業",hum,"/line",hum?"is-bad":"")}${t("作業中",run,"/line")}${t("着手待ちのチケット",f.planned,"/line",low?"is-warn":"",low?`${f.line_low_below} 件を切った`:"")}${t("今日の費用","$"+f.cost_today_usd.toFixed(2),"/usage")}</div>`}
function homeNotice(s){const xs=((s.alerts||{}).open||[]).filter(a=>!a.inbox);if(!xs.length)return"";
 return `<a class="c-notice is-warn" href="/line" data-nav><span class="c-g">工場の不調が ${xs.length} 件あります。提供元が対応しています</span><span aria-hidden="true">→</span></a>`}
function lineSummary(items){const c=k=>items.filter(i=>i.stage===k).length;
 return `<h2><a href="/line" data-nav>ライン →</a></h2><div class="kv">${LANES.map(([k,n])=>`<span>${esc(n)} <b>${c(k)}</b></span>`).join("")}</div>`}
function shipSummary(rs){return `<h2><a href="/products" data-nav>出荷 →</a></h2>${rs.length?rs.map(r=>`<div class="kv"><b><a href="/products/${esc(r.repo)}?tab=release" data-nav>${esc(r.repo)}</a></b><span>Web 検証 <b>${VER((r.environments.staging||{}).release_id)}</b></span><span>本番 <b>${VER((r.environments.prod||{}).release_id)}</b></span>${r.pending.length?'<span class="m">処理中</span>':""}</div>`).join(""):'<div class="m">出荷した製品はまだない</div>'}`}
// 受信トレイの画面は実際の大きさの 1/4（160〜480px）で出す。高解像度のスクリーンショットでも画面の全体が見える
document.addEventListener("load",e=>{const g=e.target;if(g.tagName==="IMG"&&g.classList.contains("q")&&g.naturalWidth)g.style.width=Math.round(Math.min(480,Math.max(160,g.naturalWidth/4)))+"px"},true);
// 受信トレイの「プロンプト」（11-08）。AI エージェント越しに中身を確かめて答えるとき、どの課題かを伝えるプロンプトをコピーする。
// 決め手は件の key（工場の MCP の inbox_read・inbox_answer が受け取るもの）。URL は人が開き直すため
function shareText(i){return `以下の判断待ちの課題のサポートをして\n\nタイトル: ${i.title}\nパラメータ: ${i.key}\n\nMCPの inbox_read を使って内容を確認し詳細を説明してください。回答は inbox_answer で送ります。\n受信トレイのURL: ${location.origin}${ibHref(i.key)}`}
async function copyText(t){try{await navigator.clipboard.writeText(t);return true}catch(e){const a=document.createElement("textarea");a.value=t;a.style.position="fixed";a.style.opacity="0";document.body.appendChild(a);a.select();
 let ok=false;try{ok=document.execCommand("copy")}catch(_){}a.remove();return ok}}
document.addEventListener("click",e=>{const b=e.target.closest("button[data-share]");if(!b||!S)return;const i=((S.inbox||{}).items||[]).find(x=>x.key===b.dataset.share);if(!i)return;
 copyText(shareText(i)).then(ok=>toast(ok?"プロンプトをコピーしました。AI エージェントとの会話に貼り付けてください":"コピーできませんでした"))});
// ---- 停止のアラート（11-08）。何が止まり・どれのことで・次に何をするかを 1 行ずつ。押すと詳しく（記録・リンク・やり直す）。
// × で画面から消す（停止そのものは閉じない。消したものは「消したもの」で見られ、戻せる）
let alAll=false,alGone=false;const alOpen=new Set(),alLog={};const AL_LIM=5;
function alertRow(a,gone){const open=alOpen.has(a.key),t=a.target;
 const head=`<div class="al-h" data-al="${esc(a.key)}" role="button" tabindex="0" aria-expanded="${open}"><span class="al-g">${esc(a.group)}</span><span class="al-t">${esc(a.title)}${t?`<span class="al-o"> — ${esc(t.label)}</span>`:""}</span><span class="m al-a">${a.age_sec!=null?dur(a.age_sec)+"前から":""}</span>${gone?`<button class="al-x" data-alr="${esc(a.key)}" title="アラートに戻す">戻す</button>`:`<button class="al-x" data-alx="${esc(a.key)}" title="このアラートを消す（消したものは「消したもの」で見られる）" aria-label="消す">×</button>`}</div>`;
 if(!open)return `<div class="al">${head}</div>`;
 const links=(a.links||[]).map(l=>l.log!=null?`<button data-allog="${l.log}">${esc(l.label)}</button>`:l.nav?`<a href="${esc(l.href)}" data-nav>${esc(l.label)}</a>`:link(l.href,l.label)).join(" ");
 const acts=(a.actions||[]).map(x=>x.type==="rerun"?`<button class="go" data-rerun="${x.job_id}" data-cont="${x.continue?1:""}">${esc(x.label)}</button>`:"").join("");
 const det=[t?{label:"対象",html:t.url?link(t.url,t.label):esc(t.label)}:null,{label:"対応する人",html:esc(a.who_ja)},...(a.detail||[]).map(d=>({label:d.label,html:esc(d.text)})),
  {label:"止まった種類",html:esc(a.category_ja)},a.record&&a.record.what?{label:"工場の記録の文",html:esc(a.record.what)+(a.record.waiting_for?" — "+esc(a.record.waiting_for):"")}:null].filter(Boolean);
 const jid=(a.links||[]).map(l=>l.log).find(x=>x!=null),lg=jid!=null&&alLog[jid]!=null?`<pre class="al-log">${esc(alLog[jid])}</pre>`:"";
 return `<div class="al open">${head}<div class="al-b"><div class="ask"><b>次にすること:</b> ${esc(a.todo||"建設者に回す")}</div><dl class="dt">${det.map(d=>`<dt>${esc(d.label)}</dt><dd>${d.html}</dd>`).join("")}</dl>${acts||links?`<div class="acts">${acts}${links}</div>`:""}${lg}</div></div>`}
function alertsView(al){if(!al)return"";const xs=(al.open||[]).filter(a=>!a.inbox),gs=(al.dismissed||[]).filter(a=>!a.inbox);if(!xs.length&&!gs.length)return"";   // 人の判断待ちの停止は受信トレイ（判断待ち）に出すので、ここには出さない
 const cnt={};xs.forEach(a=>cnt[a.group]=(cnt[a.group]||0)+1);const shown=alAll?xs:xs.slice(0,AL_LIM);
 return `<section class="card alerts"><h2>工場の不調（${xs.length} 件）<span class="m">保守の担当が直すもの · ${Object.entries(cnt).map(([g,n])=>esc(g)+" "+n).join("・")}</span></h2>
 ${xs.length?shown.map(a=>alertRow(a,false)).join(""):'<div class="m">画面に出しているものはない</div>'}
 <div class="al-f">${xs.length>AL_LIM?`<button data-alall>${alAll?"少なく表示":`ほか ${xs.length-AL_LIM} 件も見る`}</button>`:""}${gs.length?`<button data-algone>${alGone?"消したものを閉じる":`消したもの（${gs.length} 件）`}</button>`:""}</div>
 ${alGone&&gs.length?`<div class="al-gone"><div class="m">画面から消したもの。止まり自体はまだ解けていない（解けるとここからも消える）</div>${gs.map(a=>alertRow(a,true)).join("")}</div>`:""}</section>`}
function alToggle(k){alOpen.has(k)?alOpen.delete(k):alOpen.add(k);force=true;render()}
document.addEventListener("click",e=>{let b;
 if((b=e.target.closest("button[data-alx]"))){alOpen.delete(b.dataset.alx);return post("/api/alerts/dismiss",{key:b.dataset.alx})}
 if((b=e.target.closest("button[data-alr]")))return post("/api/alerts/dismiss",{key:b.dataset.alr,on:false});
 if((b=e.target.closest("button[data-alall]"))){alAll=!alAll;force=true;return render()}
 if((b=e.target.closest("button[data-algone]"))){alGone=!alGone;force=true;return render()}
 if((b=e.target.closest("button[data-allog]"))){const id=b.dataset.allog;if(alLog[id]!=null){delete alLog[id];force=true;return render()}
  return fetch(`/api/jobs/${id}/log`).then(r=>r.json()).then(j=>{alLog[id]=j.text??("読めなかった: "+(j.error||""));force=true;render()}).catch(()=>toast("作業の記録を読めませんでした"))}
 if((b=e.target.closest("button[data-rerun]"))){if(!b.classList.contains("armed")){const t=b.textContent;b.classList.add("armed");b.textContent="もう一度押すと積み直す";setTimeout(()=>{b.classList.remove("armed");b.textContent=t},4000);return}
  return post(`/api/jobs/${b.dataset.rerun}/rerun`,{continue:!!b.dataset.cont})}
 const h=e.target.closest(".al-h[data-al]");if(h&&!e.target.closest("button,a"))alToggle(h.dataset.al)});
document.addEventListener("keydown",e=>{const h=e.target.closest&&e.target.closest(".al-h[data-al]");if(h&&e.target===h&&(e.key==="Enter"||e.key===" ")){e.preventDefault();alToggle(h.dataset.al)}});
// ヘルプの流量の図（11-08 オーナー）。チケット＝1 台のベルトコンベアに同時に載せる部品の数（人は描かない。手を動かす数はワーカーで決まるので、人を描くとミスリードになる）。ワーカー＝ベルトコンベアの台数
function belt(x,y,w,h){let r=`<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${h/2}" fill="var(--line)" stroke="var(--sub)" stroke-width="1"/>`;
 for(let cx=x+h/2;cx<x+w;cx+=h*1.6)r+=`<circle cx="${cx}" cy="${y+h/2}" r="${h/2-3}" fill="none" stroke="var(--sub)" stroke-width="1"/>`;return r}
function part(x,y,s){return `<rect x="${x}" y="${y}" width="${s}" height="${s}" rx="3" fill="var(--accent)"/>`}
function beltFig(title,cases,kind){const W=330,H=kind==="tickets"?90:150;let out=`<svg viewBox="0 0 ${W*2+20} ${H+50}" role="img" aria-label="${esc(title)}"><text x="0" y="14" font-size="13" font-weight="700" fill="currentColor">${esc(title)}</text>`;
 cases.forEach(([n,label],i)=>{const ox=i*(W+20),oy=26;out+=`<rect x="${ox}" y="${oy}" width="${W}" height="${H}" rx="8" fill="none" stroke="var(--line)"/>`;
  if(kind==="tickets"){const by=oy+52;out+=belt(ox+16,by,W-32,18);for(let k=0;k<n;k++){const px=ox+40+k*((W-80)/Math.max(n,1))+((W-80)/Math.max(n,1)-22)/2;out+=part(px,by-20,22)}}
  else{const g=Math.min((H-20)/n,60),bh=Math.min(16,g-14),ps=Math.min(14,g-bh-4),top=oy+(H-n*g)/2;for(let k=0;k<n;k++){const by=top+k*g+(g-bh)-4;out+=belt(ox+16,by,W-32,bh)+part(ox+40+(k*53)%(W-110),by-ps,ps)}}
  out+=`<text x="${ox+W/2}" y="${oy+H+14}" font-size="12" text-anchor="middle" fill="currentColor">${esc(label)}</text>`});
 return out+"</svg>"}
// ---- ヘルプ（11-08）。ヘッダの数字と、コンソールの言葉の意味
const HELP=`<h2>ヘルプ: ヘッダの数字と言葉の意味</h2>
<h3>ヘッダ</h3><dl>
<dt>指示役</dt><dd>工場の進行役（supervisor）が動いているか。60 秒ごとに合図を出しながら、依存が片付いたチケットを優先度の順にラインへ流し、工場の PR のマージの判定・検査と judge の開始・CI 落ちや衝突の直しの手配・日報・出荷のタイミングを回す。合図が 3 分途絶えると「〇分前に停止」と赤く出る。工場を動かしている Mac のスリープや停止を疑う</dd>
<dt>モード</dt><dd>「並走モード」（attended）は、工場が決めきれなかった判断を受信トレイに回し、1 タップの回答で流す。「全自動モード」（unattended）は、決めてよい範囲の中では工場が自分で判断して流す。切り替えは設定の「モード」（期限も付けられる）か、コマンド（factoryctl mode set）。切り替えがなければ時間帯の設定（MODE_SCHEDULE。例: 夜は全自動モード）に従う</dd>
<dt>流量</dt><dd id="flow"><div class="figs">
<figure>${beltFig("チケット＝ベルトコンベアに同時に載せる部品の数",[[1,"チケット 1: ベルトに部品が 1 つ"],[3,"チケット 3: ベルトに部品が 3 つ"]],"tickets")}<figcaption>検査やマージを待つ PR も、ベルトに載ったまま数える。待ちが多いと、次の部品を載せない</figcaption></figure>
<figure>${beltFig("ワーカー＝ベルトコンベアの台数",[[1,"ワーカー 1: ベルトコンベアが 1 台"],[3,"ワーカー 3: ベルトコンベアが 3 台"]],"workers")}<figcaption>台数を増やすと、別々のものを同時に作れる</figcaption></figure></div>「いま動いている数 / 上限」。上限は設定の「ラインと流量」で変えられる。下げても動いている作業は止めず、新しく始めないだけ</dd>
<dt>　ワーカー</dt><dd>ラインで同時に動かす作業（ジョブ）の数。実装・検査・judge・デザインなど</dd>
<dt>　チケット</dt><dd>同時に手を付けるチケットの数。作業中のものと、PR が開いたまま検査やマージを待っているものの両方を数える。満杯だとワーカーが空いていても次のチケットに手を付けない</dd>
<dt>　委員会</dt><dd>知見をまとめる係（知見の委員会・事例の分析）が同時に動く数。委員会どうしは数によらず常に 1 つずつ順に動く（並べると知見が競合・矛盾するため。検討の課題は順番待ちに積まれる）。ラインを一時停止しても止まらない</dd>
<dt>　窓口</dt><dd>相談に答える係が同時に動く数。ラインを一時停止しても止まらず、ラインのワーカーを使わない</dd>
<dt>今日の費用</dt><dd>今日終わった作業の消費を API の単価に換算した目安。定額の契約の実際の請求ではない。詳しくは「使用量」</dd>
<dt>使用量の予算</dt><dd>1 日（7:00 から翌 7:00）の費用が予算に届いたら、次の日まで新しいタスクを投入しない。作業中のもの・検査・judge・p0 は止めない。毎日同じ額か曜日ごとかを、設定の「使用量の予算」で決める</dd>
<dt>着手待ちのチケット</dt><dd>依存や上限で、まだ手を付けていないチケットの数（保留にしたものは除く）。少なくなる（既定は 2 件を切る）と赤くなり、番人が知らせる。積まないとラインが空になる</dd>
<dt>ライン</dt><dd>スイッチがオンなら、順番待ちの作業を取り出して流している。オフ（一時停止）にすると新しく取り出さない。工場の更新の前に使う</dd>
</dl>
<h3>ホームの「判断待ち」と「工場の不調」</h3><dl>
<dt>判断待ち</dt><dd>プロダクトの判断（画面の案・マージ・工場が決めきれなかった点など）。受信トレイで答える。答える人はプロダクトの持ち主（オーナー）か、件ごとの宛先</dd>
<dt>工場の不調</dt><dd>番人（ラインを見張る仕組み）が見つけた、工場の側の止まり（作業の失敗・PR が CI や衝突で進めない・指示役の不在など）。工場の保守の担当（建設者）が直すもの。1 行を押すと、対象・次にすること・記録・やり直すボタンが出る。× で画面から消せる（止まり自体は解けない）。消したものは「消したもの」で見られ、戻せる。止まりが解けると自動で一覧から消える</dd>
<dt>作業の失敗</dt><dd>1 つの作業（ジョブ）が失敗した。作業ごとに 1 件出る。同じチケットで何度も失敗すると同じチケットの行が並ぶ。「やり直す」で同じ作業を積み直す</dd>
<dt>PR が進めない</dt><dd>工場の PR が衝突・CI 落ち・検査の指摘などで先に進めない</dd>
<dt>工場の更新</dt><dd>工場自身の新しい版への更新を、決まりにより自動では進めず、確認に回したもの</dd>
</dl>
<h3>ライン（チケットの流れ）</h3><dl>
<dt>依存待ち</dt><dd>先に片付けるチケットが残っている</dd><dt>投入待ち（上限）</dt><dd>依存は片付いたが、流量のチケットの上限で待っている</dd>
<dt>順番待ち</dt><dd>作業が積まれ、ワーカーが空くのを待っている</dd><dt>作業中</dt><dd>ワーカーが作業している</dd>
<dt>検査・判定・マージ待ち</dt><dd>PR ができ、CI・検査・judge・マージを待っている</dd><dt>対応が必要</dt><dd>判断や手当てを待っている</dd>
</dl><div class="m" style="margin-top:12px">画面は 10 秒ごとに自動で読み直す（タブを開いている間）</div>`;

// ---- ヘルプのリファレンス（/help/api・/help/cli）。中身は GET /api/reference（コードの説明から作る。reference.py）
// ---- ヘルプ: モバイルアプリの審査前の配布（ADR 0047）。Mac と Firebase の準備は人が 1 回だけ
const HELP_MOBILE=`<h2>モバイルアプリの配布（Android・Firebase App Distribution）</h2>
<p>工場が作ったモバイルアプリを、ストアの審査を通さずにテスターの端末へ配る（ADR 0047）。署名の鍵と Firebase へのログインは Mac にだけ置き、工場には渡さない。配るたびに、Mac の画面で人が「配布する」を押す。</p>
<h3>流れ</h3>
<ol><li><a href="/ship" data-nav>出荷</a>の「Android（審査前の配布・Firebase）」で「配布する」を押す</li>
<li>工場が Mac ランナーに頼む。Mac の画面に「工場の Mac ランナー」の窓が出るので、宛先とコミットを見て「配布する」を押す（5 分押さなければ何もせずにやめる）</li>
<li>Mac が main の最新を release でビルドし、Mac の鍵で署名して Firebase に上げる（数分）</li>
<li>テスターに Firebase から招待のメールが届く。初回は App Tester を入れ、そこから入れる</li></ol>
<h3>はじめに 1 回だけ: Firebase のサイト</h3>
<ol><li><a href="https://console.firebase.google.com" target="_blank" rel="noopener">Firebase コンソール</a>でプロジェクトを作る（Google アナリティクスはなしでよい）</li>
<li>「アプリを追加」→ Android。パッケージ名は工場が作る applicationId（rally の生徒アプリは <code>jp.rally.rally_student_app</code>）。google-services.json はダウンロードしなくてよい</li>
<li>左のメニューの「App Distribution」を開いて始める。「テスターとグループ」でグループ（例 <code>testers</code>）を作り、テスターのメールを足す</li>
<li>プロジェクトの設定 →「マイアプリ」の「アプリ ID」（<code>1:…:android:…</code>）を控え、<a href="/ship" data-nav>出荷</a>の「配布先」に入れて保存する（秘密ではない）</li></ol>
<h3>はじめに 1 回だけ: Mac（工場を動かしている Mac）</h3>
<ol><li><b>Android の道具</b>: Android Studio を入れる（<code>brew install --cask android-studio</code>）。起動して Android SDK・SDK Command-line Tools・Platform-Tools・Build-Tools を入れ、<code>flutter doctor --android-licenses</code> で同意する。<code>flutter doctor</code> の Android の行が ✓ になればよい</li>
<li><b>Firebase CLI</b>: <code>npm install -g firebase-tools</code> のあと <code>firebase login</code>（ブラウザで、Firebase のプロジェクトに触れる Google アカウントでログイン）。<code>firebase projects:list</code> にプロジェクトが出ればよい。node を mise などで入れているときは、node の版を替えると常駐のランナーから firebase が見えなくなるので、替えたら下の 4 をやり直す</li>
<li><b>署名の鍵</b>: factory-engine の orchestrator で <code>scripts/macrunner.sh android-key</code>。鍵は <code>~/.config/factory/android-upload.jks</code>、パスワードはキーチェーン（<code>factory-macrunner-android-keystore</code>）に入る。keytool が見つからないときは Android Studio の JDK を PATH に足す（<code>/Applications/Android Studio.app/Contents/jbr/Contents/Home/bin</code>）。<b>鍵はバックアップする</b>（なくすと、同じアプリの更新を配れない）</li>
<li><b>ランナーを入れ直す</b>: orchestrator で <code>scripts/macrunner.sh install</code>。<code>scripts/macrunner.sh status</code> の最後に「審査前の配布は閉じている」が出なければよい</li></ol>
<p class="m">fish など zsh 以外のシェルでも上のコマンドはそのまま打てる。Mac の設定は Mac にだけ残り、工場の DB には入らない。</p>
<h3>うまくいかないとき</h3>
<dl><dt>Mac で押されなかった</dt><dd>窓は 5 分で消え、何もしない。Mac の前にいるときにもう一度「配布する」を押す</dd>
<dt>ランナーに届かない・断られた</dt><dd>設定 →<a href="/settings/runners" data-nav>ランナー</a>で Mac ランナーが来ているかを見る。「開いていない」と出たら、上の Mac の準備の 3・4 か、配布先のアプリ ID を確かめる</dd>
<dt>署名で止まった</dt><dd>Java か鍵が見つからない。Android Studio が入っているか、<code>scripts/macrunner.sh status</code> を見る</dd>
<dt>アップロードで止まった</dt><dd>Firebase のログインが切れているか、配布先のアプリ ID が違う。Mac で <code>firebase login</code> と <code>firebase apps:list</code> を確かめる</dd></dl>
<p class="m">iOS（TestFlight）は Gen2 で足す。Apple Developer Program（有料）が要る。</p>`;
const HTABS=[["","画面の言葉"],["api","API と MCP"],["cli","コマンド（factoryctl）"],["mobile","モバイルの配布"]];
let REF=null,refQ="";
async function loadRef(){try{REF=await (await fetch("/api/reference")).json()}catch(e){put("ref-b",'<div class="m">リファレンスを読み込めませんでした</div>');return}refPut()}
const rrow=(h,d)=>`<div class="ref-r"><div>${h}</div><div class="d">${esc(d)}</div></div>`;
const plist=xs=>xs.length?`<ul>${xs.map(x=>`<li>${esc(x.replace(/^- /,""))}</li>`).join("")}</ul>`:"";
function apiSec(a){return `<section data-g><h3 id="ref-${a.id}">${esc(a.title)}</h3>${plist(a.intro)}${a.rows.map(r=>rrow(`<span class="ref-m ${r.method}">${r.method}</span><code>${esc(r.path)}</code>`,r.desc)).join("")}${plist(a.notes)}</section>`}
function mcpType(p){return p.enum?p.enum.join(" | "):p.type==="array"?(p.items&&p.items.type||"")+"[]":p.type||""}
function mcpSec(m){return `<section data-g><h3 id="ref-mcp">窓口の MCP の道具（${esc(m.server.name)} ${esc(m.server.version)}）</h3><pre>${esc(m.setup)}</pre>${m.tools.map(t=>{const ps=Object.entries((t.inputSchema||{}).properties||{}),rq=(t.inputSchema||{}).required||[];
 return `<div class="ref-t"><code><b>${esc(t.name)}</b></code><div class="d">${esc(t.description)}</div>${ps.length?`<dl>${ps.map(([k,p])=>`<dt><code>${esc(k)}</code>${rq.includes(k)?" 必須":""}</dt><dd><code>${esc(mcpType(p))}</code> ${esc(p.description||"")}</dd>`).join("")}</dl>`:""}</div>`}).join("")}</section>`}
function refBody(sub){const r=REF;
 if(sub==="api")return `<div class="toc">${r.api.map(a=>`<a href="#ref-${a.id}">${esc(a.title.split(/[（。]/)[0])}</a>`).join("")}<a href="#ref-mcp">窓口の MCP の道具</a></div>`+r.api.map(apiSec).join("")+mcpSec(r.mcp);
 const c=r.cli;return `<p>工場を動かしているマシンのターミナルで <code>${esc(c.run)} …</code> の形で打つ（compose のディレクトリなら <code>docker compose exec dispatcher factoryctl …</code> でも同じ）。下の表は factoryctl の使い方の説明（引数なしで打つと出るもの）から作っている</p>
  <div class="toc">${c.groups.map((g,i)=>`<a href="#ref-c${i}">${esc(g.title.split(/[（。:]/)[0])}</a>`).join("")}</div>`+
  c.groups.map((g,i)=>`<section data-g><h3 id="ref-c${i}">${esc(g.title.replace(/[:：]$/,""))}</h3>${g.rows.map(x=>rrow(`<code>${esc(x.cmd)}</code>`,x.desc)).join("")}${plist(g.notes)}</section>`).join("")}
function refFilter(){const q=refQ.trim().toLowerCase(),b=document.getElementById("ref-b");if(!b)return;let n=0;
 b.querySelectorAll("section[data-g]").forEach(g=>{let k=0;g.querySelectorAll(".ref-r,.ref-t").forEach(r=>{const hit=!q||r.textContent.toLowerCase().includes(q)||g.querySelector("h3").textContent.toLowerCase().includes(q);r.classList.toggle("hide",!hit);if(hit)k++});g.classList.toggle("hide",!!q&&!k);n+=k});
 put("ref-n",q?`${n} 件`:"")}
function refPut(){const [v,sub]=route();if(v!=="help"||!sub||!REF)return;put("ref-b",refBody(sub));refFilter();
 const el=location.hash&&document.getElementById(location.hash.slice(1));if(el)el.scrollIntoView({block:"start"})}   // 中身は後から来るので、/help/api#ref-mcp のような目印へはここで飛ぶ
document.addEventListener("input",e=>{if(e.target.id!=="ref-qi")return;refQ=e.target.value;refFilter()});

// ---- 受信トレイ（2 ペイン。選ぶまでは一覧が全面、選ぶと右に詳細）
// 受信トレイ（ADR 0059、#473）。判断待ち・お知らせ・答えた件で絞る。「製品」のワークベンチの既定は提供元（建設者）宛てを除く
const IB_TABS=[["open","判断待ち",i=>!i.answered&&i.kind!=="notice"],["notice","お知らせ",i=>!i.answered&&i.kind==="notice"],["answered","答えた件",i=>!!i.answered]];
const ibWhoNow=()=>ibWho===null?(wb==="product"?"!builder":""):ibWho;
const whoOk=i=>{const w=ibWhoNow();return !w||(w==="!builder"?i.who!=="builder":i.who===w)};
function ibItems(ib){const t=(IB_TABS.find(x=>x[0]===ibTab)||IB_TABS[0])[2];return ib.items.filter(i=>t(i)&&(!ibKind||i.kind===ibKind)&&whoOk(i))}
function ibFilters(ib){const opt=(m,v)=>Object.entries(m).map(([k,t])=>`<option value="${esc(k)}" ${k===v?"selected":""}>${esc(t)}</option>`).join(""),w=ibWhoNow();
 return `<div class="tabs">${IB_TABS.map(([k,n,f])=>`<button data-ibtab="${k}" class="${ibTab===k?"on":""}">${n} ${ib.items.filter(i=>f(i)&&whoOk(i)).length}</button>`).join("")}</div>
  <label class="m">種類 <select id="ib-kind"><option value="">すべて</option>${opt(ib.kinds,ibKind)}</select></label><label class="m">宛先 <select id="ib-who"><option value="">すべて</option><option value="!builder" ${w==="!builder"?"selected":""}>提供元宛てを除く</option>${opt(ib.who,w)}</select></label>`}
function ibList(ib,key){const xs=ibItems(ib);
 if(!xs.length)return `<div class="c-empty"><b>${{open:"判断待ちはありません",notice:"お知らせはありません",answered:"答えた件はありません"}[ibTab]||""}</b><span>新しく届いたら、ここと通知で知らせます</span></div>`;
 return xs.map(i=>`<a class="ir ${i.key===key?"sel":""}" href="${ibHref(i.key)}" data-nav><div class="t"><span class="who">${esc(ib.who[i.who]||i.who)}</span>${esc(i.title)}</div>
  <div class="m">${[esc(ib.kinds[i.kind]||i.kind),ago(i.age_sec),esc(i.ref)].filter(Boolean).join(" · ")}</div><div class="m m2">${esc(i.action)}</div></a>`).join("")}
function extLink(u){if(!u)return"";if(/^https:\/\//.test(u))return link(u,/github\.com\/[^/]+\/[^/]+\/pull\//.test(u)?"PR を開く":/github\.com/.test(u)?"GitHub で開く":"開く");
 if(/^\/review\//.test(u))return `<a href="${esc(u)}" target="_blank" rel="noopener">レビューページを開く</a>`;return""}
function ibDetail(ib,key){const i=ib.items.find(x=>x.key===key);
 const close=`<a class="x" href="/inbox" data-nav title="閉じる（Esc）" aria-label="閉じる">✕</a>`;
 if(!i)return `<div class="dh"><h2 class="g">メッセージが見つかりません</h2>${close}</div><div class="m">受信トレイにもうない（答えて閉じたか、解消した）。<a href="/inbox" data-nav>受信トレイに戻る</a></div>`;
 const xs=ibItems(ib),k=xs.findIndex(x=>x.key===key),prev=k>0?xs[k-1]:null,next=k>=0&&k<xs.length-1?xs[k+1]:null;
 const nav=`<span class="pn">${prev?`<a href="${ibHref(prev.key)}" data-nav>← 前</a>`:'<span class="off">← 前</span>'}${next?`<a href="${ibHref(next.key)}" data-nav>次 →</a>`:'<span class="off">次 →</span>'}</span>`;
 const meta=[esc(ib.who[i.who]||i.who),esc(ib.kinds[i.kind]||i.kind),ago(i.age_sec),esc(i.ref),extLink(i.url)].filter(Boolean).join(" · ");
 return `<div class="dh"><h2 class="g">${esc(i.title)}</h2><button data-share="${esc(i.key)}" title="AIエージェントとこの課題の判断をするためのプロンプトをクリップボードにコピーします。">プロンプト</button>${nav}${close}</div><div class="m">${meta}</div>${i.action?`<div class="ask">${esc(i.action)}</div>`:""}${detailView(i)}${ibBody(i)}`}
function ibBody(i){const n=0;
 if(i.key.startsWith("choice:")){const cs=i.key==="choice:exit_capture"?(S.choices||[]).filter(c=>c.kind==="exit_capture"):(S.choices||[]).filter(c=>"choice:"+c.id===i.key);
  if(i.answered||!cs.length)return answerView(i,n);
  return `${i.answer?`<div class="m">答えると: ${esc(i.answer.effect)}</div>`:""}${cs.map(choiceDetail).join("")}`}
 if(i.key.startsWith("release:")){const rid=i.key.slice(8),r=(S.releases||[]).find(x=>rid.startsWith(x.repo+"@"));
  return i.answered||!r?answerView(i,n):`${i.answer?`<div class="m">答えると: ${esc(i.answer.effect)}</div>`:""}${shipView([r])}`}
 return answerView(i,n)}
// 違和感の観点（ADR 0040 の決定 9）。印を束ねる鍵になる（文面では束ねない）。選ばなければ未分類。値は definitions/design_guideline.yaml の節の id（choice.VIEWPOINTS）
const VIEWS=[["foundations","基礎（色・文字・余白）"],["components","部品"],["patterns","画面の型と振る舞い"],["content","文言と表記"],["concept_tone","トーン＆マナー"],
 ["principles","デザインの原則"],["accessibility","アクセシビリティ"],["platforms","端末ごとの差"],["data_visualization","グラフと数字"],["governance","ガイドラインの運用"]];
const VIEW_SELECT=(cid,oid)=>`<select data-fview="${cid}" data-o="${esc(oid)}" title="違和感の観点（束ねるのに使う）"><option value="">観点（任意）</option>${VIEWS.map(([v,l])=>`<option value="${v}">${l}</option>`).join("")}</select>`;
// 画面の判断: 画面を実際の 1/4 の大きさで並べ（押すと拡大）、下に選んでいる画面への違和感と答え
function choiceDetail(c){const sub=c.subject||{},os=c.options||[];
 if((c.kind==="design_variants"||c.kind==="design_look")&&/^\d+$/.test(sub.uxspec||""))
  return `<div class="cd"><div class="t">${esc(KIND[c.kind]||c.kind)}: ${esc(c.question)}（${os.length} 案）</div><div class="m">${esc(c.context)}</div><div class="acts"><a class="btn" href="/review/${esc(sub.uxspec)}" target="_blank" rel="noopener">レビューページで比べて判断する</a></div></div>`;
 const k=Math.min(gi[c.id]||0,Math.max(os.length-1,0)),o=os[k]||{},exit=c.kind==="exit_capture",img=x=>`/api/choices/${c.id}/image/${esc(x.id)}`;
 const head=`<div class="t">${esc(KIND[c.kind]||c.kind)}: ${esc(c.question)}</div><div class="m">${[sub.pr_url?link(sub.pr_url,"PR"):"",sub.artifact_url?link(sub.artifact_url,"CI の成果物"):"",esc(sub.ticket||""),esc(c.context)].filter(Boolean).join(" · ")}</div>`;
 // 画像は実際の大きさの 1/4 で並べ、何枚あるかと画面の全体が一度に見えるようにする（11-08。前は 1 枚を幅いっぱいに出し、ウインドウを広げるほど大きくなった）。押すと拡大
 const tiles=os.map((x,j)=>`<div class="ti ${j===k?"on":""}">${x.image_key?`<a class="zm" href="${img(x)}" target="_blank" rel="noopener" data-zsrc="${img(x)}" data-zalt="${esc(x.id)}. ${esc(x.label)}" title="押すと拡大（別タブは ⌘/Ctrl＋クリック）"><img class="q" loading="lazy" alt="${esc(x.id)}. ${esc(x.label)}" src="${img(x)}"></a>`:'<span class="noimg">画像なし</span>'}<button class="th ${j===k?"on":""}" data-gi="${c.id}" data-k="${j}" title="この画面について答える">${esc(x.id)}. ${esc(x.label)}${x.id===c.recommended?"（推奨）":""}</button></div>`).join("");
 const per=`<div class="cap"><b>${esc(o.id)}. ${esc(o.label)}</b>${o.id===c.recommended?' <span class="m">（推奨）</span>':""} <span class="m">${k+1} / ${os.length} · ← → で前後の画面</span></div>${o.note?`<div class="m">${esc(o.note)}</div>`:""}
  <div class="acts">${exit?"":`<button class="go" data-ch="choose" data-c="${c.id}" data-o="${esc(o.id)}">この案にする</button>`}<input data-fnote="${c.id}" data-o="${esc(o.id)}" placeholder="この画面の違和感をコメント（Enter で送る。例: 余白が詰まって見える）" maxlength="300">${VIEW_SELECT(c.id,o.id)}<button data-ch="flag" data-c="${c.id}" data-o="${esc(o.id)}">違和感を送る</button></div>`;
 const tail=exit?`<div class="acts"><span class="m">全部見終えたら</span><button class="go" data-ch="seen" data-c="${c.id}">見た（ほかに違和感なし）</button></div>`
  :`<div class="acts"><input id="note-${c.id}" placeholder="コメント（理由・違和感。任意、戻すときは必須）" maxlength="300"><button class="danger" data-ch="return" data-c="${c.id}">どれも違う（戻す）</button></div>`;
 return `<div class="cd" id="choice-${c.id}">${head}<div class="m">画面 ${os.length} 枚。押すと拡大、名前を押すとその画面について答える</div><div class="gal" data-zl>${tiles}</div><div class="dock">${per}${tail}</div></div>`}
document.addEventListener("click",e=>{const b=e.target.closest("button[data-gi]");if(!b)return;gi[b.dataset.gi]=Number(b.dataset.k);force=true;render()});
document.addEventListener("click",e=>{const b=e.target.closest("button[data-ibtab]");if(!b)return;ibTab=b.dataset.ibtab;force=true;render()});
document.addEventListener("change",e=>{if(e.target.id==="ib-kind"||e.target.id==="ib-who"){if(e.target.id==="ib-kind")ibKind=e.target.value;else ibWho=e.target.value;force=true;render()}});
function stepGallery(d){const g=document.querySelector("#view .gal");if(!g)return false;const ts=[...g.querySelectorAll("button[data-gi]")];if(ts.length<2)return false;
 const id=ts[0].dataset.gi,k=((gi[id]||0)+d+ts.length)%ts.length;gi[id]=k;force=true;render();return true}
document.addEventListener("keydown",e=>{if(!Z.hidden||e.target.matches("input,textarea,select")||e.metaKey||e.ctrlKey||e.altKey)return;const [v,key]=route();if(v!=="inbox")return;
 if(e.key==="Escape"&&key){e.preventDefault();return go("/inbox")}
 if(e.key==="ArrowRight"||e.key==="ArrowLeft"){if(stepGallery(e.key==="ArrowRight"?1:-1))e.preventDefault()}});
// 答えたら次の未回答のメッセージへ（最後なら一覧へ）。実装後の画面のまとまりは、まだ見ていない問いが残る間は留まる
function advance(answeredKey){const [v,key]=route();if(v!=="inbox"||!S)return;
 if(key==="choice:exit_capture"&&(S.choices||[]).filter(c=>c.kind==="exit_capture").length>1)return;
 const xs=S.inbox.items.filter(i=>!i.answered),k=xs.findIndex(i=>i.key===key),rest=xs.filter(i=>i.key!==key&&i.key!==answeredKey);
 const next=rest.find((i,j)=>xs.indexOf(i)>k)||rest[0];go(next?ibHref(next.key):"/inbox")}

// ---- ライン
function srcLink(i){if(!i.ref)return"";const s=SRC[i.source]||i.source||"";const tag=s?`<span class="src">${esc(s)}</span>`:"";
 return i.url&&/^https:\/\//.test(i.url)?`<a href="${esc(i.url)}" target="_blank" rel="noopener" title="元のチケットを開く">${tag}${esc(i.ref)}</a>`:tag+esc(i.ref)}
const repoOf=i=>(i.ref||"").split("#")[0];
let lnRepo="",lnSrc="",lnView=(()=>{try{return localStorage.getItem("console.lineView")==="kanban"?"kanban":"list"}catch(e){return"list"}})();
function lineFilters(items){const repos=[...new Set(items.map(repoOf).filter(Boolean))].sort(),srcs=[...new Set(items.map(i=>i.source).filter(Boolean))].sort();
 const sel=(id,label,xs,v,name)=>`<label class="m">${label} <select id="${id}"><option value="">すべて</option>${xs.map(x=>`<option value="${esc(x)}" ${x===v?"selected":""}>${esc(name?name(x):x)}</option>`).join("")}</select></label>`;
 return `<div class="tabs"><button data-lnview="list" class="${lnView==="list"?"on":""}">一覧</button><button data-lnview="kanban" class="${lnView==="kanban"?"on":""}">かんばん</button></div>`+sel("ln-repo","リポジトリ",repos,lnRepo)+sel("ln-src","チケットの出どころ",srcs,lnSrc,x=>SRC[x]||x)}
document.addEventListener("click",e=>{const b=e.target.closest("button[data-lnview]");if(!b)return;lnView=b.dataset.lnview;try{localStorage.setItem("console.lineView",lnView)}catch(x){}force=true;render()});
document.addEventListener("change",e=>{if(e.target.id==="ln-repo"||e.target.id==="ln-src"){if(e.target.id==="ln-repo")lnRepo=e.target.value;else lnSrc=e.target.value;force=true;render()}});

// ---- 出荷（製品ごとに出荷先のカード。今は Web だけ。モバイルのストアや CDN は同じ形のカードで足す）
function targetsOf(r){return r.targets||[{kind:"web",label:"Web",release:r}]}
const PV_STATE={queued:"依頼した（工場が拾うのを待っている）",running:"配布中（Mac の画面の窓で「配布する」を押す）",distributed:"配った",failed:"配れなかった"};
const PV_STAGE={request:"Mac ランナーに届かない・断られた",attend:"Mac で押されなかった",build:"ビルド",sign:"署名",upload:"アップロード",timeout:"時間切れ",lost:"途中で止まった",checkout:"コミットの確かめ"};
function previewView(t){const it=t.items||[];
 const rows=it.map(i=>`<li><span class="m">${esc(String(i.at||"").slice(0,16).replace("T"," "))}</span> ${esc(PV_STATE[i.state]||i.state)}${i.build_number?` <b>${esc(i.build_number)}</b>`:""}${i.sha?` <span class="m">${esc(i.sha.slice(0,8))}</span>`:""}${i.state==="failed"?` <span class="m">— ${esc(PV_STAGE[i.stage]||i.stage||"")}${i.error?"："+esc(i.error).slice(0,120):""}</span>`:""}${i.notes?` <span class="m">「${esc(i.notes).slice(0,60)}」</span>`:""}</li>`).join("");
 const form=t.busy?`<div class="m">配布中は次の依頼を出せない。Mac の画面に窓が出ていたら「配布する」を押す（5 分で取り消し）</div>`
  :`<div><input id="pv-notes-${esc(t.app)}" placeholder="テスターに見せる変更点（任意）" maxlength="1000" style="width:min(100%,28em)"> <button class="go" data-preview="${esc(t.app)}">配布する（main の最新）</button></div>
   <div class="m">押すと Mac の画面に確かめの窓が出る。そこでもう一度「配布する」を押すと、署名して Firebase のテスターに配る</div>`;
 const tg=t.target,tid="pv-t-"+esc(t.app);
 const dest=`<details${tg?"":" open"}><summary class="m">配布先: ${tg?`Firebase のアプリ <code>${esc(tg.firebase_app_id)}</code>・グループ <code>${esc(tg.groups)}</code>`:"工場では未設定（Mac ランナーの設定を使う）"}</summary>
  <div><input id="${tid}-id" placeholder="Firebase の Android アプリの ID（1:…:android:…）" value="${esc(tg?tg.firebase_app_id:"")}" style="width:min(100%,24em)">
  <input id="${tid}-g" placeholder="テスターのグループ（例 testers）" value="${esc(tg?tg.groups:"testers")}" style="width:12em"> <button data-preview-target="${esc(t.app)}">保存</button></div>
  <div class="m">アプリの ID は Firebase のプロジェクトの設定で見られる。秘密ではない（署名の鍵と Firebase のログインは Mac にだけある）。空にして保存すると Mac の設定に戻る</div></details>`;
 return dest+form+(rows?`<ul class="pv">${rows}</ul>`:`<div class="m">まだ配っていない</div>`)}
document.addEventListener("click",e=>{const b=e.target.closest("button[data-preview-target]");if(!b)return;const app=b.dataset.previewTarget,id="pv-t-"+app;
 post("/api/distribute/target",{app,firebase_app_id:(document.getElementById(id+"-id")||{}).value||"",groups:(document.getElementById(id+"-g")||{}).value||""}).then(()=>{force=true})});
document.addEventListener("click",e=>{const b=e.target.closest("button[data-preview]");if(!b)return;const app=b.dataset.preview;
 const n=document.getElementById("pv-notes-"+app);b.disabled=true;post("/api/distribute",{app,notes:n?n.value:""}).then(ok=>{if(!ok)b.disabled=false;force=true})});
function shipPage(rs){if(!rs.length)return `<div class="m">出荷した製品はまだない</div>`;
 return rs.map(r=>`<section class="card"><h2>${esc(r.repo)}</h2><div class="targets">${targetsOf(r).map(t=>`<div class="target"><div class="th2">${esc(t.label)}</div>${t.kind==="web"?shipView([t.release]):t.kind==="preview"?previewView(t):`<div class="m">${esc(t.note||"")}</div>`}</div>`).join("")}</div></section>`).join("")
  +`<div class="m">出荷の指示は Claude からもできる（窓口の MCP）。モバイルの審査前の配布は ADR 0047。ストアや CDN などの出荷先は、ここに同じ形のカードで足していく</div>`}

// ---- 設定
function scaleForm(sc){if(!sc)return `<div class="m">流量の宣言が読めない</div>`;const d=sc.desired,r=sc.running,n=v=>v==null?"?":v;
 const L={workers:"ワーカー（レーンの数）",tickets:"同時に進めるチケット（レーンに載せる数）",governance:"委員会と知見の流れ（委員会どうしは常に 1 つずつ）",desk:"窓口"};
 return `<div class="m"><a href="/help#flow" data-nav>ワーカーとチケットの違い（ベルトコンベアの図）→</a></div><div class="m">${sc.at?`${esc(sc.by||"")} が ${esc(String(sc.at).slice(0,16).replace("T"," "))} に宣言${sc.reason?"（"+esc(sc.reason)+"）":""}`:"環境変数の値（宣言なし）"}。factoryctl line scale と同じ</div>
  <table class="tb"><tr><th></th><th>いま</th><th>宣言</th></tr>${Object.keys(L).map(k=>`<tr><td>${L[k]}</td><td>${n(r[k])}</td><td><input type="number" min="0" max="50" id="sc-${k}" value="${esc(d[k])}"></td></tr>`).join("")}</table>
  <div class="acts"><input id="sc-reason" placeholder="理由（任意）" maxlength="200"><button class="go" data-scale>宣言を変える</button></div>`}
document.addEventListener("click",e=>{if(!e.target.closest("button[data-scale]"))return;const body={reason:(document.getElementById("sc-reason")||{}).value||"操業コンソール"};
 for(const k of ["workers","tickets","governance","desk"]){const v=(document.getElementById("sc-"+k)||{}).value;if(v!==""&&v!=null)body[k]=Number(v)}post("/api/line/scale",body)});
// 使用量の予算（11-08 オーナー「全体で 1 日あたりの予算を決めるか、曜日べつで決めるか」）。宣言は usage.set_budget、supervisor が周回ごとに読む
const WD=[["mon","月"],["tue","火"],["wed","水"],["thu","木"],["fri","金"],["sat","土"],["sun","日"]];
function budgetForm(b){if(!b)return `<div class="m">予算の宣言が読めない</div>`;const m=b.mode==="weekday"?"weekday":"daily",v=x=>x==null?"":esc(x),h=b.day_start_hour;
 const who=b.at?`${esc(b.by||"")} が ${esc(String(b.at).slice(0,16).replace("T"," "))} に宣言${b.reason?"（"+esc(b.reason)+"）":""}`:`いまは環境変数の枠（平日 <code>${esc(b.env.weekday||"なし")}</code>、週末 <code>${esc(b.env.weekend||"なし")}</code>）。保存すると下の決め方に切り替わる`;
 return `<div class="m">1 日（${h}:00 から翌日の ${h}:00 まで）の費用（API 単価に換算）が予算に届いたら、次の日まで新しいタスクを投入しない。作業中のもの・検査・judge・p0 は止めない。factoryctl budget set と同じ</div>
  <div class="m">${who}</div><div class="kv"><span>今日の予算 <b>${b.today_usd==null?"なし":"$"+Number(b.today_usd).toLocaleString()}</b></span></div>
  <div class="acts"><label><input type="radio" name="bg-mode" value="daily" ${m==="daily"?"checked":""}> 毎日同じ額</label><label><input type="radio" name="bg-mode" value="weekday" ${m==="weekday"?"checked":""}> 曜日ごと</label></div>
  <div id="bg-daily" ${m==="daily"?"":"hidden"}><table class="tb"><tr><td>1 日の予算（ドル）</td><td><input type="number" min="0" step="1" id="bg-d" value="${v(b.daily_usd)}"></td></tr></table></div>
  <div id="bg-week" ${m==="weekday"?"":"hidden"}><div class="bg-wk">${WD.map(([k,n])=>`<label>${n} <input type="number" min="0" step="1" id="bg-${k}" value="${v((b.weekday_usd||{})[k])}"></label>`).join("")}</div><div class="m">曜日は 1 日が始まった日の曜日（月曜 ${h}:00〜火曜 ${h}:00 が月曜の予算）</div></div>
  <div class="acts"><input id="bg-reason" placeholder="理由（任意）" maxlength="200"><button class="go" data-budget>予算を変える</button></div>`}
document.addEventListener("change",e=>{if(e.target.name!=="bg-mode")return;const w=e.target.value==="weekday";document.getElementById("bg-daily").hidden=w;document.getElementById("bg-week").hidden=!w});
document.addEventListener("click",e=>{if(!e.target.closest("button[data-budget]"))return;const g=id=>(document.getElementById(id)||{}).value,num=x=>x===""||x==null?undefined:Number(x);
 const mode=(document.querySelector('input[name="bg-mode"]:checked')||{}).value||"daily",body={mode,reason:g("bg-reason")||"操業コンソール"};
 if(mode==="daily")body.daily_usd=num(g("bg-d"));else{body.weekday_usd={};for(const [k] of WD)body.weekday_usd[k]=num(g("bg-"+k))}
 post("/api/budget",body)});
// モード（11-11 オーナー「設定から切り替えられるように」）。並走・全自動・時間帯の設定に従う の 3 択と任意の期限。factoryctl mode set と同じ記録
const MODE_JA={attended:"並走モード",unattended:"全自動モード",schedule:"時間帯の設定に従う"},BASIS_JA={factoryctl:"切り替え",schedule:"時間帯の設定",default:"既定"};
function modeForm(m){if(!m)return `<div class="m">モードが読めない</div>`;const o=m.override,cur=o?o.mode:"schedule";
 const who=o?`${esc(o.by||"")} が ${esc(String(o.at||"").slice(0,16).replace("T"," "))} に切り替え${o.until?"（"+esc(String(o.until).slice(0,16).replace("T"," "))+" まで）":"（次に切り替えるまで）"}${o.reason?"。"+esc(o.reason):""}`
  :`切り替えはなし。時間帯の設定 <code>${esc(m.schedule||"なし")}</code>、既定は ${esc(MODE_JA[m.default]||m.default)}`;
 return `<div class="m">並走モード: 工場が決めきれなかった判断を受信トレイに回し、回答を待って流す。全自動モード: 決めてよい範囲の中では工場が自分で判断して流す（権限・秘密情報・決済・破壊的なスキーマ変更はどちらでも回す）。factoryctl mode set と同じ</div>
  <div class="kv"><span>いま <b>${esc(MODE_JA[m.mode]||m.mode)}</b>（決め手: ${esc(BASIS_JA[m.basis]||m.basis)}）</span></div><div class="m">${who}</div>
  <div class="acts">${["attended","unattended","schedule"].map(k=>`<label><input type="radio" name="md-mode" value="${k}" ${k===cur?"checked":""}> ${MODE_JA[k]}</label>`).join("")}</div>
  <div class="acts" id="md-until-row" ${cur==="schedule"?"hidden":""}><label class="m">期限（任意。過ぎたら時間帯の設定に戻る） <input type="datetime-local" id="md-until"></label></div>
  <div class="acts"><input id="md-reason" placeholder="理由（任意）" maxlength="200"><button class="go" data-mode>モードを変える</button></div>`}
document.addEventListener("change",e=>{if(e.target.name!=="md-mode")return;document.getElementById("md-until-row").hidden=e.target.value==="schedule"});
document.addEventListener("click",e=>{if(!e.target.closest("button[data-mode]"))return;const mode=(document.querySelector('input[name="md-mode"]:checked')||{}).value;if(!mode)return;
 const u=(document.getElementById("md-until")||{}).value,body={mode,reason:(document.getElementById("md-reason")||{}).value||"操業コンソール"};
 if(u&&mode!=="schedule")body.until=new Date(u).toISOString();post("/api/factory/mode",body)});
// 設定の「サービスとの連携」（ADR 0045 の追記）。工場の生産に使う間接のサービス（バックログ管理・デザインの資料・社内の文書など）の取り込み元。
// 鍵の値は出さない。鍵番（個人の権限とトークンの管理）ができるまでは、オンボーディングの判定の記録に書いた取り込み元を並べるだけ
function connView(cs){if(!cs.length)return '<div class="m">取り込み元の記録がない。オンボーディングの判定の記録（factoryctl readiness record）の sources に書くとここに出る</div>';
 const d=x=>x?esc(String(x).slice(0,10)):"—",bad=c=>c.state==="expired"||c.state==="revoked";
 return `<div class="rn"><table><tr><th>サービス</th><th>使い道</th><th>製品</th><th>取り込みの形</th><th>許可した人</th><th>範囲</th><th>状態</th><th>期限</th><th>最後に使った日</th></tr>${cs.map(c=>`<tr>
  <td><b>${esc(c.service)}</b></td><td>${esc(c.use||"")}</td><td><a href="/products/${esc(c.product)}" data-nav>${esc(c.product)}</a></td><td>${esc(c.kind_ja)}${c.received_at?`<div class="m">受け取った日 ${d(c.received_at)}</div>`:""}</td><td>${esc(c.granted_by||"—")}</td><td>${esc(c.scope||"—")}</td>
  <td><span class="dot ${bad(c)?"":"on"}"></span>${esc(c.state_ja)}</td><td>${d(c.expires_at)}</td><td>${d(c.last_used_at)}</td></tr>`).join("")}</table></div>
  <div class="m" style="margin-top:8px">鍵とトークンの値はここに出さない。つなぎ直す操作（OAuth の流れ）は鍵番ができてから足す。それまでは取り込み元を記録し直して更新する</div>`}
function settingsBody(sec,s){const f=s.factory;
 if(sec==="runners")return `<h2>ランナー</h2>${pairsView(s.runner_pairs||[])}${runnersView(s.runners||[])||'<div class="m">登録済みのランナーはない（工場の外の機械に入れた常駐プロセス。Mac なら scripts/macrunner.sh）</div>'}`;
 if(sec==="mode")return `<h2>モード</h2>${modeForm(f.mode_spec)}`;
 if(sec==="line")return `<h2>ラインと流量</h2><div class="acts">${lineView(f.line)}</div>${scaleForm(f.scale)}`;
 if(sec==="budget")return `<h2>使用量の予算</h2>${budgetForm(f.budget)}<div class="m"><a href="/usage" data-nav>日ごとの費用と予算（使用量）→</a></div>`;
 if(sec==="connectors")return `<h2>サービスとの連携</h2>${connView(s.connectors||[])}`;
 if(sec==="mac")return `<h2>Mac</h2>${f.host?`<div class="acts"><label class="stat sw" title="オンの間は工場の稼働中に Mac を眠らせない">起こしたまま <input type="checkbox" role="switch" id="keepawake" ${f.host.prevent_sleep?"checked":""}><b class="${f.host.sleep_prevented?"on":""}">${f.host.sleep_prevented?"起こしたまま":"スリープできる"}</b></label></div><div class="m">Mac 上の見張り（keep-awake.sh）が ${dur(f.host.agent_age_sec)}前に連絡</div>`:'<div class="m">Mac 上の見張り（keep-awake.sh）から連絡がない。Mac 以外で動いているか、見張りが止まっている</div>'}`;
 return `<h2>工場の版</h2><div class="kv"><span>Engine <b><code>${esc(s.engine_version||"unknown")}</code></b></span></div><div class="m">工場の更新は Issue にラベル factory:swap を付けて頼む（この画面からの更新は今後）</div>`}

// ---- 描画
// 骨組み（ADR 0059、#471）。PC は左の縦のメニュー、タブレットはアイコンだけ、スマホは下のタブ。並びはワークベンチで変わり、どのページにもたどれる
const ICON={home:'<path d="M4 11l8-7 8 7v9h-5v-6H9v6H4z"/>',inbox:'<path d="M4 5h16v14H4zM4 13h5l1 2h4l1-2h5"/>',ask:'<path d="M4 5h16v11H9l-5 4z"/>',
 products:'<path d="M4 7l8-4 8 4v10l-8 4-8-4zM4 7l8 4 8-4M12 11v10"/>',line:'<path d="M4 6h16M4 12h16M4 18h10"/>',live:'<path d="M3 12h4l3-8 4 16 3-8h4"/>',ship:'<path d="M3 15l9 5 9-5M12 3v12M8 7l4-4 4 4"/>',
 usage:'<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',settings:'<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
 help:'<circle cx="12" cy="12" r="10"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 2.5-3 4.5M12 17.5h.01"/>',more:'<circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/>'};
const icon=k=>`<svg viewBox="0 0 24 24" aria-hidden="true">${ICON[k]||ICON.more}</svg>`;
const PAGES={home:["/","ホーム"],inbox:["/inbox","受信トレイ"],ask:["/ask","問い合わせ"],live:["/live","事業部の様子"],products:["/products","製品"],line:["/line","ライン"],usage:["/usage","使用量"]};
// main は主のメニュー、それ以外は「すべてのページ」の下。tabs はスマホの下のタブ（最後に「その他」が付く）
const WORKBENCHES={factory:{name:"工場",main:["home","inbox","live","line","ask","products","usage"],tabs:["home","inbox","ask","products"]},
 product:{name:"製品",main:["home","inbox","ask","products","live","usage"],tabs:["home","inbox","ask","products"]}};
const store={get:k=>{try{return localStorage.getItem(k)}catch(e){return null}},set:(k,v)=>{try{localStorage.setItem(k,v)}catch(e){}}};
let wb=WORKBENCHES[store.get("console.workbench")]?store.get("console.workbench"):"factory";
function navItem(k,v,cls){const [h,t]=PAGES[k];return `<a href="${h}" data-nav data-v="${k}" title="${t}"${k===v?' aria-current="page"':""}${cls?` class="${cls}"`:""}>${icon(k)}<span class="lb">${t}</span>${k==="inbox"?'<span class="c-count" data-n="inbox"></span>':""}</a>`}
function buildNav(){const w=WORKBENCHES[wb],v=route()[0],rest=Object.keys(PAGES).filter(k=>!w.main.includes(k));
 document.getElementById("side").innerHTML=w.main.map(k=>navItem(k,v)).join("")+(rest.length?`<div class="c-sep"><span class="lb">すべてのページ</span></div>`+rest.map(k=>navItem(k,v)).join(""):"");
 document.getElementById("tabs").innerHTML=w.tabs.map(k=>navItem(k,v)).join("")+`<a href="#more" id="more-t" title="その他">${icon("more")}<span class="lb">その他</span></a>`;
 document.getElementById("tabs").style.gridTemplateColumns=`repeat(${w.tabs.length+1},1fr)`;
 document.getElementById("wb").innerHTML=Object.entries(WORKBENCHES).map(([k,x])=>`<option value="${k}"${k===wb?" selected":""}>${x.name}</option>`).join("");
 const others=Object.keys(PAGES).filter(k=>!w.tabs.includes(k));
 document.getElementById("more-b").innerHTML=`<div class="c-side">${others.map(k=>navItem(k,v)).join("")}<div class="c-sep">工場と使い方</div><a href="/settings" data-nav data-v="settings">${icon("settings")}<span class="lb">設定</span></a><a href="/help" data-nav data-v="help">${icon("help")}<span class="lb">ヘルプ</span></a></div>
  <label class="c-wb">ワークベンチ <select data-wb>${Object.entries(WORKBENCHES).map(([k,x])=>`<option value="${k}"${k===wb?" selected":""}>${x.name}</option>`).join("")}</select></label>`;
 setCounts()}
function setWorkbench(k){if(!WORKBENCHES[k])return;wb=k;store.set("console.workbench",k);buildNav();force=true;render()}
document.addEventListener("change",e=>{if(e.target.id==="wb"||e.target.matches("[data-wb]"))setWorkbench(e.target.value)});
function setCounts(){const n=(S&&S.inbox&&S.inbox.count)||0;document.querySelectorAll('.c-count[data-n="inbox"]').forEach(c=>{c.textContent=n||"";c.hidden=!n;c.classList.toggle("is-new",n>0)})}
const more=document.getElementById("more");
function moreOpen(on){more.hidden=!on;document.getElementById("more-t")?.setAttribute("aria-expanded",on)}
document.addEventListener("click",e=>{if(e.target.closest("#more-t")){e.preventDefault();moreOpen(more.hidden);return}if(!more.hidden&&(e.target===more||e.target.closest("#more a[data-nav]")))moreOpen(false)});
// 工場の状態（右上）。点の色＋一言。押すと指示役・モード・流量・費用・着手待ち・ラインの切り替え
const fsBtn=document.getElementById("fs"),fsPop=document.getElementById("fs-pop");
function fsOpen(on){fsPop.hidden=!on;fsBtn.setAttribute("aria-expanded",on)}
fsBtn.addEventListener("click",()=>fsOpen(fsPop.hidden));
document.addEventListener("click",e=>{if(!fsPop.hidden&&!e.target.closest("#fs-pop,#fs"))fsOpen(false)});
document.addEventListener("keydown",e=>{if(e.key==="Escape"){fsOpen(false);moreOpen(false)}});
function factoryState(s){const f=s.factory,hb=f.heartbeat_age_sec,l=f.line,al=((s.alerts||{}).open||[]).filter(a=>!a.inbox).length,mode=f.mode==="unattended"?"全自動モード":"並走モード";
 if(hb==null||hb>180)return["is-bad","止まっている",hb==null?"指示役が未起動":"指示役が "+dur(hb)+"前に停止"];
 if(f.qa_hold)return["is-bad","止まっている","QA が差し止め中"];
 if(l&&l.draining)return["is-bad","止まっている","ラインを一時停止中"];
 if(al)return["is-warn","注意",`不調 ${al} 件`];
 if(f.line_low_below>0&&f.planned<f.line_low_below)return["is-warn","注意",`着手待ちが ${f.planned} 件`];
 return["","動いている",mode]}
function factoryView(s){const [c,t,sub]=factoryState(s);fsBtn.className="c-factory "+c;fsBtn.innerHTML=`<i></i><b>${esc(t)}</b><span class="sub">· ${esc(sub)}</span>`;fsBtn.title=`工場の状態: ${t}（${sub}）。押すと詳しく`}
function skeleton(v,arg){
 if(v==="home")return HOME_ORDER[wb].map(k=>k==="notice"||k==="stats"?`<div id="h-${k}" class="h-${k}"></div>`:`<section class="card" id="h-${k}" hidden></section>`).join("");
 if(v==="inbox")return `<div class="ibx ${arg?"open":""}"><div class="ib-l"><div class="ib-f" id="ib-f"></div><div id="ib-list"></div></div><div class="ib-r" id="ib-d"></div></div>`;
 if(v==="ask")return askSkeleton(arg);
 if(v==="live")return liveSkeleton();
 if(v==="line")return `<div id="l-stops"></div><div class="filters" id="l-f"></div><div id="lanes"></div>`;
 if(v==="ship")return `<div id="ship"></div>`;
 if(v==="products")return arg?ppSkeleton(arg):`<div id="prod-b"><div class="m" style="margin-top:12px">読み込み中…</div></div>`;
 if(v==="usage")return `<div id="usage-b"><div class="m" style="margin-top:12px">読み込み中…</div></div>`;
 if(v==="help")return `<nav class="htabs">${HTABS.map(([k,n])=>`<a href="/help${k?"/"+k:""}" data-nav class="${k===arg?"on":""}">${n}</a>`).join("")}</nav>`+(arg==="mobile"?`<section class="card help">${HELP_MOBILE}</section>`:arg?`<section class="card help ref"><div class="ref-q"><input id="ref-qi" type="search" placeholder="絞り込み（例 inbox、drain、予算）" value="${esc(refQ)}"><span class="m" id="ref-n"></span></div><div id="ref-b"><div class="m">読み込み中…</div></div></section>`:`<section class="card help">${HELP}</section>`);
 return `<div class="set"><nav class="sn">${SETTINGS.map(([k,n])=>`<a href="/settings/${k}" data-nav class="${k===arg?"on":""}">${esc(n)}</a>`).join("")}${LATER.map(n=>`<span class="later" title="今後">${esc(n)}（今後）</span>`).join("")}</nav><section class="card" id="set-b"></section></div>`}
let force=false,usageAt=0,prodAt=0,prodOf=null,prodTgt="";
// 製品のページ（ADR 0045 の追記）。使用量と同じく、画面の中に中身（/products/body?p=<製品>）を読み込む。仕事の切り替えは画面の中だけ
async function loadProd(p){prodAt=Date.now();prodOf=p;try{const h=await (await fetch("/products/body"+(p?"?p="+enc(p):""))).text();put("prod-b",h);prodTab()}catch(e){put("prod-b",'<div class="m">製品のページを読み込めませんでした</div>')}}
function prodTab(){if(!prodTgt)return;const b=document.querySelector(`#prod-b button[data-tgt-b="${prodTgt}"]`);if(b)b.click()}
document.addEventListener("click",e=>{const b=e.target.closest("button[data-tgt-b]");if(!b)return;const t=b.dataset.tgtB;prodTgt=t;
 b.parentNode.querySelectorAll("button").forEach(x=>x.classList.toggle("on",x===b));b.closest("section").querySelectorAll("[data-tgt]").forEach(x=>x.hidden=x.dataset.tgt!==t)});
// 使用量（11-08）。前は別の 1 枚のページで、開くとタブが消えて戻れなかった。ほかのページと同じ画面の中に中身（/usage/body）を読み込む
async function loadUsage(){usageAt=Date.now();try{const h=await (await fetch("/usage/body")).text();put("usage-b",h)}catch(e){put("usage-b",'<div class="m">使用量を読み込めませんでした</div>')}}
function render(){if(!S)return;if(route()[0]==="ship")ppShipRedirect(S);const s=S,[v,arg]=route(),sig=v+"|"+wb+"|"+(v==="settings"||v==="help"?arg:"")+"|"+(v==="inbox"?(arg?"open":""):v==="ask"||v==="products"?arg:"");
 const ib=s.inbox||{count:0,items:[],who:{},kinds:{}};
 document.querySelectorAll("#side a[data-v],#tabs a[data-v],#more a[data-v]").forEach(a=>a.dataset.v===v?a.setAttribute("aria-current","page"):a.removeAttribute("aria-current"));
 document.getElementById("gear").classList.toggle("on",v==="settings");document.getElementById("help-i").classList.toggle("on",v==="help");
 const vw=document.getElementById("view");if(vw._sig!==sig){vw._sig=sig;vw.innerHTML=skeleton(v,arg);window.scrollTo(0,0)}
 if(v==="settings")document.querySelectorAll(".sn a").forEach(a=>a.classList.toggle("on",a.getAttribute("href")==="/settings/"+arg));
 const pairs=(s.runner_pairs||[]).length;
 put("banner",pairs&&!(v==="settings"&&arg==="runners")?`<a class="banner" href="/settings/runners" data-nav>新しいランナーの接続の依頼が ${pairs} 件。設定のランナーで許可か拒否を選ぶ →</a>`:"");
 if(v==="home"){const H={todo:todoView(ib),prod:prodCard(s.products),trend:trendView(s.trends),line:lineSummary(s.items),ship:shipSummary(s.releases||[]),notice:homeNotice(s),stats:homeStats(s)};
  for(const k of HOME_ORDER[wb]){const el=document.getElementById("h-"+k);if(!el)continue;el.hidden=!H[k];put("h-"+k,H[k]||"")}}
 else if(v==="inbox"){document.querySelector(".ibx").classList.toggle("open",!!arg);put("ib-f",ibFilters(ib));put("ib-list",ibList(ib,arg));
  if(!arg)put("ib-d","");else if(force||!busy("ib-d"))put("ib-d",ibDetail(ib,arg));
  const sel=document.querySelector(".ir.sel");if(sel&&force)sel.scrollIntoView({block:"nearest"})}
 else if(v==="line"){put("l-stops",alertsView(s.alerts));put("l-f",lineFilters(s.items));
  const xs=s.items.filter(i=>(!lnRepo||repoOf(i)===lnRepo)&&(!lnSrc||i.source===lnSrc));
  // ラインは状態ごとの一覧が既定（止まっているものが先頭）、かんばんは切り替え（ADR 0059、#476）
  document.getElementById("lanes").className=lnView==="kanban"?"kanban":"lnlist";
  put("lanes",lnView==="kanban"?LANES.map(([k,n])=>{const ys=xs.filter(i=>i.stage===k);return `<section class="lane"><h2><span>${n}</span><span>${ys.length}</span></h2>${ys.map(card).join("")||'<div class="m">なし</div>'}</section>`}).join("")
   :LANES.map(([k,n])=>{const ys=xs.filter(i=>i.stage===k);return ys.length?`<section class="card lnsec ${k}"><h2>${n}（${ys.length}）</h2>${ys.map(card).join("")}</section>`:""}).join("")||'<div class="c-empty"><b>ラインにチケットはありません</b></div>')}
 else if(v==="ask")askRender(arg)
 else if(v==="live")liveRender(s)
 else if(v==="ship"){if(force||!busy("ship"))put("ship",shipPage(s.releases||[]))}
 else if(v==="products"&&arg)ppRender(s,arg)
 else if(v==="products"){if(prodOf!==arg||Date.now()-prodAt>60000||!document.querySelector("#prod-b .prod"))loadProd(arg)}
 else if(v==="usage"){if(Date.now()-usageAt>60000||!document.querySelector("#usage-b .usage"))loadUsage()}
 else if(v==="help"){if(arg&&arg!=="mobile"&&!document.querySelector("#ref-b section")){if(REF)refPut();else loadRef()}}
 else if(force||!busy("set-b"))put("set-b",settingsBody(arg,s));
 force=false}
async function load(){if(document.hidden&&!force)return;let s;try{s=await (await fetch("/api/state")).json()}catch(e){toast("読み込めませんでした");return}
 S=s;levels=s.levels;const f=s.factory,hb=f.heartbeat_age_sec,ib=s.inbox||{count:0};
 put("hdr",[
  `<span class="stat ${hb==null||hb>180?"bad":""}" title="工場の進行役（supervisor）が動いているか。60 秒ごとの合図が 3 分途絶えると赤くなる。詳しくはヘルプ">指示役 <b>${hb==null?"未起動":hb>180?dur(hb)+"前に停止":"動いている"}</b></span>`,
  `<a class="stat" href="/settings/mode" data-nav title="並走モード: 決めきれない判断は受信トレイに回す。全自動モード: 決めてよい範囲は工場が自分で判断する。押すと設定で切り替えられる">モード <b>${f.mode==="unattended"?"全自動モード":"並走モード"}</b></a>`,
  scaleView(f.scale,f.line),
  `<span class="stat">今日の費用 <b>$${f.cost_today_usd.toFixed(2)}</b></span>`,
  `<span class="stat ${f.line_low_below>0&&f.planned<f.line_low_below?"bad":""}" title="まだ手を付けていないチケット（保留を除く）。${f.line_low_below} 件を切ると番人が知らせる">着手待ちのチケット <b>${f.planned}</b></span>`,
  lineView(f.line),holdView(f.qa_hold)].join(" "));
 setCounts();factoryView(s);
 document.title=(ib.count?`(${ib.count}) `:"")+"操業コンソール";
 render()}
buildNav();
load();setInterval(load,10000);document.addEventListener("visibilitychange",()=>{if(!document.hidden)load()});
