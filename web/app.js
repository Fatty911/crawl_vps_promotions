"use strict";

const stateNode = document.getElementById("state");
const bodyNode = document.getElementById("status-body");
const searchBox = document.getElementById("search-box");
const providerFilter = document.getElementById("provider-filter");
const outcomeFilter = document.getElementById("outcome-filter");
const regionFilter = document.getElementById("region-filter");
const currencyFilter = document.getElementById("currency-filter");
// 月化价格统一 CNY 展示的参考汇率（2026-08-11 快照，中间价近似值；
// 仅用于展示换算，不改原始金额；CNY 本身按 1:1 统一格式）
const CNY_RATES = { USD: 7.25, EUR: 7.9, CNY: 1 };

// 月化金额换算为 CNY 数值（无价格返回 null；未知货币按原值保守处理）。
// 2026-08-11：同一套餐多支付周期单价可能不同——优先取 price_points
// 最低月化（列表按周期升序，首项即最低），回退 monthly_amount。
function monthlyCnyValue(row) {
  const points = Array.isArray(row.price_points) ? row.price_points : null;
  if (points && points.length > 0 && points[0].monthly_amount !== null && points[0].monthly_amount !== undefined) {
    const rate = CNY_RATES[row.currency];
    const value = points[0].monthly_amount;
    return rate === undefined ? value : value * rate;
  }
  if (row.monthly_amount === null || row.monthly_amount === undefined) return null;
  const rate = CNY_RATES[row.currency];
  return rate === undefined ? row.monthly_amount : row.monthly_amount * rate;
}

// 支付周期中文名（2026-08-11：月化列标注多周期）
const PERIOD_LABELS = { monthly: "月付", quarterly: "季付", semiannual: "半年付", yearly: "年付" };

// 月化展示文本：多周期时标注最低价周期（如 "¥28.94 · 年付"）
function monthlyLabel(row) {
  const value = monthlyCnyValue(row);
  if (value === null) return null;
  const points = Array.isArray(row.price_points) ? row.price_points : null;
  if (points && points.length > 1 && points[0].billing_period && points[0].billing_period !== row.billing_period) {
    return `¥${value.toFixed(2)} · ${PERIOD_LABELS[points[0].billing_period] || points[0].billing_period}`;
  }
  return `¥${value.toFixed(2)}`;
}

// 多周期 tooltip：列出全部支付周期的月化单价
function pricePointsTitle(row) {
  const points = Array.isArray(row.price_points) ? row.price_points : null;
  if (!points || points.length <= 1) return "";
  const rate = CNY_RATES[row.currency];
  const parts = points.map((p) => {
    const label = PERIOD_LABELS[p.billing_period] || p.billing_period;
    const v = p.monthly_amount === null || p.monthly_amount === undefined ? p.monthly_amount : p.monthly_amount * (rate === undefined ? 1 : rate);
    return v === null || v === undefined ? `${label} 无价` : `${label} ¥${v.toFixed(2)}/月`;
  });
  return parts.join("  ·  ");
}
const billingFilter = document.getElementById("billing-filter");
const availabilityFilter = document.getElementById("availability-filter");
const routeFilter = document.getElementById("route-filter");
const optRamFilter = document.getElementById("opt-ram");
const optPriceFilter = document.getElementById("opt-price");
const reliabilityFilter = document.getElementById("reliability-filter");
const exportCsv = document.getElementById("export-csv");
const sortRulesNode = document.getElementById("sort-rules");
const sortAdd = document.getElementById("sort-add");
const sortClear = document.getElementById("sort-clear");
const historyLoad = document.getElementById("history-load");
const historyList = document.getElementById("history-list");
const historyPrev = document.getElementById("history-prev");
const historyNext = document.getElementById("history-next");
const historyPageNode = document.getElementById("history-page");
const trendTask = document.getElementById("trend-task");
const trendChart = document.getElementById("trend-chart");
const dealsList = document.getElementById("deals-list");
const HISTORY_PAGE_SIZE = 50;
let rows = [];
let historyRows = [];
let historyPage = 0;
let sortRules = [{field: "updated", dir: "desc"}];

// Sortable fields (Excel-style: field + direction). "specs" fields are
// read from row.specs (cpu / ram_gb / storage_gb).
const SORT_FIELDS = [
  ["updated", "更新时间"], ["provider", "服务商"], ["plan_name", "套餐"],
  ["region", "地区"], ["cpu", "CPU 核数"], ["ram_gb", "内存"],
  ["storage_gb", "硬盘大小"], ["disk_type", "硬盘类型"],
  ["amount", "原始金额"], ["monthly_amount", "月化(CNY)"], ["value_score", "性价比"],
  ["reliability", "可靠性"], ["oversell", "超售"], ["availability", "库存"],
];

// Routes that are fast for Beijing users (China-optimized premium lines).
const BEIJING_FAST_ROUTES = [
  "cn2 gia-e", "cn2 gia", "as9929", "cmin2", "cmi", "cug",
];

// True when any claimed route is a Beijing-fast premium route (CN2 GIA /
// AS9929 / CMIN2 / CMI / CUG — 中国优化精品线路，北京访问快).
function hasFastRoute(routes) {
  if (!routes || routes.length === 0) return false;
  return routes.some((route) => {
    const lower = String(route).toLowerCase();
    return BEIJING_FAST_ROUTES.some((fast) => lower.includes(fast));
  });
}

function setState(name, message) {
  stateNode.dataset.state = name;
  stateNode.textContent = message;
}

function safeOfferLink(row) {
  try {
    const target = new URL(row.product_url || row.url);
    const source = new URL(row.source_url || row.url);
    const sameDomain = target.hostname === source.hostname ||
      target.hostname.endsWith(`.${source.hostname}`) ||
      source.hostname.endsWith(`.${target.hostname}`);
    if (target.protocol !== "https:" || !sameDomain) return null;
    return target.href;
  } catch (_error) {
    return null;
  }
}

function cell(value) {
  const node = document.createElement("td");
  node.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
  return node;
}

function stars(score) {
  if (score === null || score === undefined || !Number.isFinite(Number(score))) return "—";
  const value = Number(score);
  const full = Math.round(value / 2);
  return "★".repeat(Math.max(0, Math.min(5, full))) + "☆".repeat(Math.max(0, 5 - Math.min(5, full)));
}

function oversellLabel(level) {
  const map = {none: "无", low: "低", medium: "中", high: "高"};
  return map[String(level || "").toLowerCase()] || String(level || "—");
}

function specValue(row, field) {
  const specs = row.specs || {};
  const value = specs[field];
  return value === null || value === undefined ? null : Number(value);
}

function diskType(row) {
  // Infer disk type from plan name + plan keywords (nvme/ssd/hdd).
  const haystack = `${row.plan_name || ""} ${(row.plan_tokens || []).join(" ")}`.toLowerCase();
  if (/\bnvme\b|nvme/i.test(haystack)) return "NVMe";
  if (/\bssd\b/i.test(haystack)) return "SSD";
  if (/\bhdd\b/i.test(haystack)) return "HDD";
  return null;
}

function diskLabel(row) {
  const type = diskType(row);
  const gb = specValue(row, "storage_gb");
  const size = gb === null ? "" : `${gb}GB`;
  if (type && size) return `${type} ${size}`;
  return type || size || null;
}

function routeCell(routes) {
  const node = document.createElement("td");
  if (!routes || routes.length === 0) {
    node.textContent = "—";
    return node;
  }
  routes.forEach((route) => {
    const badge = document.createElement("span");
    badge.className = "route-badge";
    badge.textContent = route;
    // Beijing-fast premium routes get a highlight (CN2 GIA / AS9929 / CMIN2
    // / CMI / CUG / CN2 — 中国优化精品线路，北京访问快).
    if (hasFastRoute([route])) {
      badge.classList.add("route-fast");
      badge.title = "北京访问快（中国优化精品线路）";
    }
    node.append(badge);
    node.append(document.createTextNode(" "));
  });
  return node;
}

function numeric(row, field) {
  if (field === "cpu" || field === "ram_gb" || field === "storage_gb") {
    const value = specValue(row, field);
    return row.outcome === "success" && value !== null ? value : Number.POSITIVE_INFINITY;
  }
  const value = Number(row[field]);
  return row.outcome === "success" && Number.isFinite(value) ? value : Number.POSITIVE_INFINITY;
}

function sortKey(row, field) {
  switch (field) {
    case "cpu":
    case "ram_gb":
    case "storage_gb": {
      const value = specValue(row, field);
      return value === null ? Number.POSITIVE_INFINITY : value;
    }
    case "disk_type":
      return diskType(row) || "~";
    case "provider":
    case "plan_name":
    case "region":
    case "oversell":
    case "availability":
      return String(row[field] || "~");
    case "updated":
      return timestamp(row);
    default:
      return numeric(row, field);
  }
}

function timestamp(row) {
  const value = Date.parse(row.finished_at || row.checked_at || "");
  return Number.isFinite(value) ? value : 0;
}

function matchSearch(row, needle) {
  if (!needle) return true;
  const haystack = [
    row.provider, row.plan_name, row.region,
    (row.provider_claimed_routes || []).join(" "),
    row.reliability_note, String(row.reliability || ""), row.oversell,
    String(row.value_score || ""), diskLabel(row) || "",
  ].join(" ").toLowerCase();
  return needle.split(/\s+/).every((part) => haystack.includes(part));
}

// Excel-style multi-level sort: apply rules in order, each with its own
// direction; later rules only break ties.
function compareRows(left, right) {
  for (const rule of sortRules) {
    const a = sortKey(left, rule.field);
    const b = sortKey(right, rule.field);
    let cmp;
    if (typeof a === "number" && typeof b === "number") {
      cmp = a - b;
    } else {
      cmp = String(a).localeCompare(String(b), "zh-CN");
    }
    if (cmp !== 0) return rule.dir === "asc" ? cmp : -cmp;
  }
  return 0;
}

function render() {
  bodyNode.replaceChildren();
  const region = regionFilter.value.trim().toLowerCase();
  const needle = searchBox.value.trim().toLowerCase();
  const minReliability = Number(reliabilityFilter.value || 0);
  const visible = rows.filter((row) =>
    matchSearch(row, needle) &&
    (!providerFilter.value || row.provider === providerFilter.value) &&
    (!outcomeFilter.value || row.outcome === outcomeFilter.value) &&
    (!region || String(row.region || "").toLowerCase().includes(region)) &&
    (!currencyFilter.value || row.currency === currencyFilter.value) &&
    (!billingFilter.value || row.billing_period === billingFilter.value) &&
    (!availabilityFilter.value || row.availability === availabilityFilter.value) &&
    (!routeFilter.value || (row.provider_claimed_routes || []).includes(routeFilter.value)) &&
    // 默认筛选（2026-08-11 用户要求）：线路不含优化线路（CN2 GIA /
    // AS9929 / CMIN2 / CMI / CUG）时，内存必须 ≥4GB；手动选择线路后
    // 该默认约束自动失效（用户意图优先），也可取消勾选。
    (routeFilter.value || !optRamFilter.checked ||
      hasFastRoute(row.provider_claimed_routes) ||
      Number(specValue(row, "ram_gb")) >= 4) &&
    // 默认筛选（2026-08-11 用户要求）：默认隐藏月化 >¥200 的套餐；
    // 手动选择线路后约束自动失效（用户意图优先），也可取消勾选。
    (routeFilter.value || !optPriceFilter.checked ||
      (() => { const v = monthlyCnyValue(row); return v === null || v <= 200; })()) &&
    (Number(row.reliability || 0) >= minReliability)
  );
  visible.sort(compareRows);

  visible.forEach((row) => {
    const tr = document.createElement("tr");
    const plan = cell(row.plan_name);
    const href = safeOfferLink(row);
    if (href) {
      const link = document.createElement("a");
      link.href = href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = row.plan_name;
      plan.replaceChildren(link);
    }
    const raw = row.amount === null ? null : `${row.amount} ${row.currency} / ${row.billing_period}`;
    // 月化价格统一按 CNY 展示（2026-08-11 用户要求：格式统一为 ¥xx.xx）；
    // 多支付周期时标注最低价周期 + tooltip 列出全部周期月化单价
    const monthlyNode = cell(monthlyLabel(row));
    const pointsTitle = pricePointsTitle(row);
    if (pointsTitle) {
      monthlyNode.title = pointsTitle;
    }
    const reason = row.rejection_reason || row.block_reason || "";
    const diag = row.browser_diag || "";
    const reasonEl = document.createElement("td");
    reasonEl.textContent = reason || diag || "—";
    if (diag && reason) {
      reasonEl.textContent = `${reason} · ${diag}`;
      reasonEl.title = `浏览器诊断：${diag}`;
    } else if (diag) {
      reasonEl.title = `浏览器诊断：${diag}`;
    }
    const cpu = specValue(row, "cpu");
    const ram = specValue(row, "ram_gb");
    const ramLabel = ram === null ? null : `${ram}GB`;
    tr.append(cell(row.provider), plan, cell(row.region), cell(row.outcome),
      cell(cpu === null ? null : `${cpu} 核`), cell(ramLabel), cell(diskLabel(row)),
      routeCell(row.provider_claimed_routes),
      cell(raw), monthlyNode, cell(stars(row.value_score)), cell(stars(row.reliability)),
      cell(oversellLabel(row.oversell)), cell(row.availability), reasonEl);
    bodyNode.append(tr);

  });
}

function addOptions(select, values) {
  [...new Set(values.filter((value) => value))].sort().forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    select.append(option);
  });
}

async function readJson(path) {
  const response = await fetch(path, {cache: "no-store"});
  if (!response.ok) throw new Error("payload unavailable");
  return response.json();
}

async function historyExists() {
  try {
    const history = await fetch("data/price_history.json", {cache: "no-store"});
    if (!history.ok) return false;
    const events = await history.json();
    return Array.isArray(events) && events.length > 0;
  } catch (_error) {
    return false;
  }
}

function renderHistory() {
  historyList.replaceChildren();
  const pages = Math.max(1, Math.ceil(historyRows.length / HISTORY_PAGE_SIZE));
  historyPage = Math.min(historyPage, pages - 1);
  const start = historyPage * HISTORY_PAGE_SIZE;
  historyRows.slice(start, start + HISTORY_PAGE_SIZE).forEach((row) => {
    const item = document.createElement("li");
    item.textContent = `${row.provider} · ${row.plan_name} · ${row.amount} ${row.currency} / ${row.billing_period} · ${row.observed_at}`;
    historyList.append(item);
  });
  historyPageNode.textContent = historyRows.length ? `${historyPage + 1} / ${pages}` : "无历史";
  historyPrev.disabled = historyPage === 0;
  historyNext.disabled = historyPage + 1 >= pages;
}

async function loadHistory() {
  try {
    const response = await fetch("data/price_history.json", {cache: "no-store"});
    if (!response.ok) throw new Error("history unavailable");
    const events = await response.json();
    historyRows = Array.isArray(events)
      ? events.slice().sort((left, right) => Date.parse(right.observed_at) - Date.parse(left.observed_at))
      : [];
    historyPage = 0;
    renderHistory();
    historyLoad.disabled = true;
    // Populate trend selector from history task ids.
    const taskIds = [...new Set(historyRows.map((row) => row.task_id).filter(Boolean))].sort();
    trendTask.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "选择套餐";
    trendTask.append(placeholder);
    taskIds.forEach((id) => {
      const option = document.createElement("option");
      option.value = id;
      option.textContent = id;
      trendTask.append(option);
    });
  } catch (_error) {
    historyPageNode.textContent = "历史加载失败";
  }
}

function renderTrend(taskId) {
  trendChart.replaceChildren();
  if (!taskId) {
    const hint = document.createElement("p");
    hint.textContent = "加载历史后选择套餐查看 180 天价格趋势";
    trendChart.append(hint);
    return;
  }
  const points = historyRows
    .filter((row) => row.task_id === taskId && row.amount !== null && row.amount !== undefined)
    .sort((left, right) => Date.parse(left.observed_at) - Date.parse(right.observed_at));
  if (points.length < 2) {
    const hint = document.createElement("p");
    hint.textContent = "该套餐历史数据不足（至少 2 个价格点）";
    trendChart.append(hint);
    return;
  }
  const width = 720;
  const height = 200;
  const pad = 30;
  const amounts = points.map((row) => Number(row.amount));
  const minAmount = Math.min(...amounts);
  const maxAmount = Math.max(...amounts);
  const span = Math.max(0.0001, maxAmount - minAmount);
  const firstTime = Date.parse(points[0].observed_at);
  const lastTime = Date.parse(points[points.length - 1].observed_at);
  const timeSpan = Math.max(1, lastTime - firstTime);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${taskId} 价格趋势`);
  const polyline = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  const coords = points.map((row) => {
    const x = pad + ((Date.parse(row.observed_at) - firstTime) / timeSpan) * (width - 2 * pad);
    const y = height - pad - ((Number(row.amount) - minAmount) / span) * (height - 2 * pad);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  polyline.setAttribute("points", coords.join(" "));
  polyline.setAttribute("fill", "none");
  polyline.setAttribute("stroke", "#4c8bf5");
  polyline.setAttribute("stroke-width", "2");
  svg.append(polyline);
  // First/last labels.
  const firstLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
  firstLabel.setAttribute("x", pad);
  firstLabel.setAttribute("y", height - 8);
  firstLabel.setAttribute("font-size", "11");
  firstLabel.textContent = `${points[0].observed_at.slice(0, 10)} ${points[0].amount}${points[0].currency || ""}`;
  const lastLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
  lastLabel.setAttribute("x", width - pad - 180);
  lastLabel.setAttribute("y", height - 8);
  lastLabel.setAttribute("font-size", "11");
  lastLabel.textContent = `${points[points.length - 1].observed_at.slice(0, 10)} ${points[points.length - 1].amount}${points[points.length - 1].currency || ""}`;
  svg.append(firstLabel, lastLabel);
  trendChart.append(svg);
}

function exportCsvRows(rowsToExport) {
  const header = ["provider", "plan_name", "region", "outcome", "cpu", "ram_gb",
    "storage_gb", "disk_type", "routes", "amount", "currency",
    "billing_period", "monthly_amount", "value_score", "reliability", "oversell",
    "availability", "rejection_reason"];
  const escape = (value) => {
    const text = value === null || value === undefined ? "" : String(value);
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  const lines = [header.join(",")];
  rowsToExport.forEach((row) => {
    const cpu = specValue(row, "cpu");
    const ram = specValue(row, "ram_gb");
    const gb = specValue(row, "storage_gb");
    const values = [
      row.provider, row.plan_name, row.region, row.outcome,
      cpu === null ? "" : cpu, ram === null ? "" : ram,
      gb === null ? "" : gb, diskType(row) || "",
      (row.provider_claimed_routes || []).join(" "),
      row.amount, row.currency, row.billing_period, row.monthly_amount,
      row.value_score, row.reliability, row.oversell,
      row.availability, row.rejection_reason,
    ];
    lines.push(values.map(escape).join(","));
  });
  const blob = new Blob(["\uFEFF" + lines.join("\n")], {type: "text/csv;charset=utf-8"});
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "vps-promotions.csv";
  link.click();
  URL.revokeObjectURL(link.href);
}

function renderSortRules() {
  sortRulesNode.replaceChildren();
  sortRules.forEach((rule, index) => {
    const row = document.createElement("div");
    row.className = "sort-rule";
    const label = document.createElement("span");
    label.textContent = `第${index + 1}关键字`;
    const fieldSelect = document.createElement("select");
    fieldSelect.className = "sort-field";
    SORT_FIELDS.forEach(([value, labelText]) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = labelText;
      if (value === rule.field) option.selected = true;
      fieldSelect.append(option);
    });
    fieldSelect.addEventListener("change", () => {
      rule.field = fieldSelect.value;
      render();
    });
    const dirButton = document.createElement("button");
    dirButton.type = "button";
    dirButton.className = "sort-dir";
    dirButton.textContent = rule.dir === "asc" ? "↑ 升序" : "↓ 降序";
    dirButton.addEventListener("click", () => {
      rule.dir = rule.dir === "asc" ? "desc" : "asc";
      renderSortRules();
      render();
    });
    const removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.className = "sort-remove";
    removeButton.textContent = "×";
    removeButton.title = "删除该关键字";
    removeButton.addEventListener("click", () => {
      sortRules.splice(index, 1);
      if (sortRules.length === 0) sortRules = [{field: "updated", dir: "desc"}];
      renderSortRules();
      render();
    });
    row.append(label, fieldSelect, dirButton, removeButton);
    sortRulesNode.append(row);
  });
}

function renderDeals(deals) {
  dealsList.replaceChildren();
  if (!deals || !Array.isArray(deals.entries) || deals.entries.length === 0) {
    const item = document.createElement("li");
    item.textContent = "暂无情报（RSS 抓取失败或为空）";
    dealsList.append(item);
    return;
  }
  deals.entries.forEach((entry) => {
    const item = document.createElement("li");
    const link = document.createElement("a");
    link.href = entry.link;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = entry.title;
    const meta = document.createElement("span");
    meta.className = "deals-meta";
    meta.textContent = ` [${entry.source_label}] ${(entry.published || "").slice(0, 16)}`;
    item.append(link, meta);
    dealsList.append(item);
  });
}

async function loadDeals() {
  try {
    const deals = await readJson("data/deals.json");
    renderDeals(deals);
  } catch (_error) {
    const item = document.createElement("li");
    item.textContent = "情报数据不可用";
    dealsList.append(item);
  }
}

async function load() {
  try {
    const manifest = await readJson("manifest.json");
    const audit = await readJson("audit.json");
    rows = await readJson("data/status.json");
    if (manifest.schema_version !== 4 || manifest.mode !== "live" || audit.structure_status !== "pass") {
      setState("structure-blocked", "structure-blocked：本轮结构审计未通过");
      return;
    }
    if (!Array.isArray(rows) || rows.length === 0) {
      const hasHistory = await historyExists();
      setState(hasHistory ? "history-only" : "empty",
        hasHistory ? "history-only：只有历史记录" : "empty：本轮确实无状态");
      return;
    }
    setState(audit.product_status === "pass" ? "ready" : "live-blocked",
      audit.product_status === "pass" ? "本轮产品门通过" : "live-blocked：售罄、阻断和失败已完整展示");
    document.getElementById("batch-summary").textContent = `${manifest.batch_id} · ${manifest.source_sha}`;
    addOptions(providerFilter, rows.map((row) => row.provider));
    addOptions(outcomeFilter, rows.map((row) => row.outcome));
    addOptions(currencyFilter, rows.map((row) => row.currency));
    addOptions(billingFilter, rows.map((row) => row.billing_period));
    addOptions(availabilityFilter, rows.map((row) => row.availability));
    addOptions(routeFilter, rows.flatMap((row) => row.provider_claimed_routes || []));
    renderSortRules();
    render();
    initHScroll();
    loadDeals();
  } catch (_error) {
    setState("structure-blocked", "structure-blocked：公开数据加载或校验失败");
  }
}

[searchBox, providerFilter, outcomeFilter, regionFilter, currencyFilter, billingFilter,
  availabilityFilter, routeFilter, reliabilityFilter].forEach((node) =>
  node.addEventListener("input", render));
optRamFilter.addEventListener("change", render);
optPriceFilter.addEventListener("change", render);

// 浮动横向滚动条（2026-08-11 用户要求）：横向滚动条原生位于表格容器
// 底部，表格很长时必须竖向滚到底才能看到。改为鼠标进入表格范围时，
// 在视口底部浮现一个浮动横向滚动条（fixed），与容器 scrollLeft 双向
// 同步，可拖动；离开表格范围后隐藏。
function initHScroll() {
  const wrap = document.querySelector(".table-wrap");
  if (!wrap || wrap.dataset.hscroll) return;
  wrap.dataset.hscroll = "1";
  const bar = document.createElement("div");
  bar.className = "hscroll-float";
  const track = document.createElement("div");
  track.className = "hscroll-track";
  const thumb = document.createElement("div");
  thumb.className = "hscroll-thumb";
  track.append(thumb);
  bar.append(track);
  document.body.append(bar);
  const maxScroll = () => wrap.scrollWidth - wrap.clientWidth;
  function sync() {
    if (maxScroll() <= 0) { bar.style.display = "none"; return; }
    const maxThumb = track.clientWidth - thumb.offsetWidth;
    const pct = maxScroll() > 0 ? wrap.scrollLeft / maxScroll() : 0;
    thumb.style.left = `${Math.max(0, Math.min(1, pct)) * maxThumb}px`;
  }
  wrap.addEventListener("mouseenter", () => { sync(); bar.style.display = "block"; });
  wrap.addEventListener("mousemove", sync);
  wrap.addEventListener("mouseleave", () => { bar.style.display = "none"; });
  wrap.addEventListener("scroll", sync);
  let drag = null;
  thumb.addEventListener("mousedown", (e) => {
    drag = { startX: e.clientX, startLeft: wrap.scrollLeft };
    bar.classList.add("dragging");
    e.preventDefault();
  });
  window.addEventListener("mousemove", (e) => {
    if (!drag) return;
    const maxThumb = track.clientWidth - thumb.offsetWidth;
    const ratio = maxThumb > 0 ? (e.clientX - drag.startX) / maxThumb : 0;
    wrap.scrollLeft = drag.startLeft + ratio * maxScroll();
    sync();
  });
  window.addEventListener("mouseup", () => {
    if (drag) { drag = null; bar.classList.remove("dragging"); }
  });
}
sortAdd.addEventListener("click", () => {
  if (sortRules.length >= 4) return;
  sortRules.push({field: "updated", dir: "desc"});
  renderSortRules();
  render();  // re-sort immediately (Excel-style tiebreaker applies now)
});
sortClear.addEventListener("click", () => {
  sortRules = [{field: "updated", dir: "desc"}];
  renderSortRules();
  render();
});
exportCsv.addEventListener("click", () => {
  const needle = searchBox.value.trim().toLowerCase();
  const rowsToExport = rows.filter((row) => matchSearch(row, needle));
  exportCsvRows(rowsToExport);
});
historyLoad.addEventListener("click", loadHistory);
historyPrev.addEventListener("click", () => {
  historyPage -= 1;
  renderHistory();
});
historyNext.addEventListener("click", () => {
  historyPage += 1;
  renderHistory();
});
trendTask.addEventListener("change", () => renderTrend(trendTask.value));
load();
