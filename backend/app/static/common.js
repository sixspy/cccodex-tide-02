async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + " " + r.status);
  return r.json();
}
async function postJSON(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? "{}" : JSON.stringify(body),
  });
  const t = await r.text();
  let data;
  try { data = JSON.parse(t); } catch { data = { 原文: t }; }
  if (!r.ok) throw new Error((data && (data.错误 || data.detail)) || t || String(r.status));
  return data;
}
function 行(html) {
  const tr = document.createElement("tr");
  tr.innerHTML = html;
  return tr;
}
function 徽章(文, 类) {
  return `<span class="徽章 ${类 || ""}">${文}</span>`;
}
function 空态(文) {
  return `<p class="说明条">${文}</p>`;
}
function 格式概览(o) {
  return [
    ["观测点", o["观测点"] || 0],
    ["在缓冲", o["在缓冲"] || 0],
    ["窗切片", o["窗切片"] || 0],
    ["迟到", o["迟到"] || 0],
  ];
}
function 渲染水位(el, overview) {
  if (!el) return;
  el.innerHTML = "";
  for (const [标, 值] of 格式概览(overview || {})) {
    const d = document.createElement("div");
    d.className = "水卡";
    d.innerHTML = `<div class="数">${值}</div><div class="标">${标}</div>`;
    el.appendChild(d);
  }
}
function 窗规一行(rules) {
  if (!rules) return "窗规未加载";
  return `${rules["当前窗号"] || "?"} · ${rules["窗起"] || ""} → ${rules["窗止"] || ""} · 窗长秒 ${rules["窗长秒"] || "?"}`;
}
function 装载摘要(report) {
  if (!report) return "";
  return `入流 ${report["入流"] || 0} / 入缓冲 ${report["入缓冲"] || 0} / 迟到 ${report["迟到"] || 0} / 驳回 ${report["驳回"] || 0}`;
}
function 关窗摘要(report) {
  if (!report) return "";
  return `窗 ${report["窗号"] || "?"} · 成片 ${report["成片数"] || 0} · 缓冲消耗 ${report["缓冲消耗"] || 0}`;
}
function 贯验摘要(report) {
  if (!report) return "";
  return `${report["是否对齐"] ? "对齐" : "撕裂"} · 在缓冲 ${report["在缓冲"]} · 切片事件 ${report["切片事件"]} · 迟到 ${report["迟到"]}`;
}
function 安全文本(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}
function 再裁组(只读) {
  if (!只读) return "<p class=\"说明条\">再裁状态未加载</p>";
  const 最新 = 只读.最新;
  const 态 = 只读.启用
    ? `<span class="徽章 好">已启用 · ${只读.形态}</span>`
    : `<span class="徽章">未启用（基线事件时入窗）</span>`;
  const 脏 = 只读.现行缓冲含窗外点
    ? `<span class="徽章 危">现行缓冲含窗外点 ${只读.脏行数}</span>`
    : `<span class="徽章 好">现行缓冲无窗外点</span>`;
  const 账 = 最新
    ? `<div class="说明">最新裁净：批次 ${最新.批次} · 暂存 ${最新.暂存数} · 入窗 ${最新.入窗数} · 剔除 ${最新.剔除数} · 归档键 ${最新.归档键 || "—"} · 指纹 ${最新.指纹 || "—"} · ${最新.valid ? "valid" : "未收口：" + (最新.收口原因 || "中途失败")}</div>`
    : `<div class="说明">尚无裁净账。加载样例后关窗可见。</div>`;
  return `<div>${态} ${脏}</div><div class="说明">策略：${只读.策略} · 窗内 ${只读.窗内笔数} · 暂存待裁 ${只读.暂存待裁} · 归档 ${只读.归档笔数}（${只读.归档键数} 键）</div>${账}<div class="说明">下一步：${只读.下一步}</div>`;
}
window.潮切 = {
  getJSON, postJSON, 行, 徽章, 空态, 格式概览, 渲染水位,
  窗规一行, 装载摘要, 关窗摘要, 贯验摘要, 安全文本, 再裁组,
};
