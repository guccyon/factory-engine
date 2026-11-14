// 部品のカタログのページの動き（テーマの切り替えと、部品の見本の動き）
document.querySelectorAll("[data-theme-set]").forEach(b => b.addEventListener("click", () => {
  const t = b.dataset.themeSet;
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
}));
var b=document.getElementById('cb');b.onclick=function(){if(b.dataset.armed==='true'){b.textContent='戻しました';b.dataset.armed='false';return;}b.dataset.armed='true';b.textContent='本当に戻す（もう一度押す）';setTimeout(function(){if(b.dataset.armed==='true'){b.dataset.armed='false';b.textContent='本番を 0.21.0 に戻す';}},5000);};
