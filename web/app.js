"use strict";

const stateNode = document.getElementById("state");
const bodyNode = document.getElementById("status-body");
const cardsNode = document.getElementById("cards");
const searchBox = document.getElementById("search-box");
const providerFilter = document.getElementById("provider-filter");
const outcomeFilter = document.getElementById("outcome-filter");
const regionFilter = document.getElementById("region-filter");
const currencyFilter = document.getElementById("currency-filter");
const billingFilter = document.getElementById("billing-filter");
const availabilityFilter = document.getElementById("availability-filter");
const routeFilter = document.getElementById("route-filter");
const reliabilityFilter = document.getElementById("reliability-filter");
const sortOrder = document.getElementById("sort-order");
const exportCsv = document.getElementById("export-csv");
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

function numeric(row, field) {
  const value = Number(row[field]);
  return row.outcome === "success" && Number.isFinite(value) ? value : Number.POSITIVE_INFINITY;
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
    String(row.value_score || ""),
  ].join(" ").toLowerCase();
  return needle.split(/\s+/).every((part) => haystack.includes(part));
}

function render() {
  bodyNode.replaceChildren();
  cardsNode.replaceChildren();
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
    (Number(row.reliability || 0) >= minReliability)
  );
  visible.sort(sortOrder.value === "monthly"
    ? (left, right) => numeric(left, "monthly_amount") - numeric(right, "monthly_amount")
    : sortOrder.value === "amount"
      ? (left, right) => numeric(left, "amount") - numeric(right, "amount")
      : sortOrder.value === "value"
        ? (left, right) => numeric(left, "value_score") - numeric(right, "value_score")
        : sortOrder.value === "reliability"
          ? (left, right) => numeric(left, "reliability") - numeric(right, "reliability")
          : (left, right) => timestamp(right) - timestamp(left));

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
    const monthly = row.monthly_amount === null ? null : `${row.monthly_amount} ${row.currency}`;
    tr.append(cell(row.provider), plan, cell(row.region), cell(row.outcome), cell(raw),
      cell(monthly), cell(stars(row.value_score)), cell(stars(row.reliability)),
      cell(oversellLabel(row.oversell)), cell(row.availability), cell(row.rejection_reason || row.block_reason));
    bodyNode.append(tr);

    const card = document.createElement("article");
    const title = document.createElement("h2");
    title.textContent = `${row.provider} · ${row.plan_name}`;
    const detail = document.createElement("p");
    detail.textContent = `${row.region} · ${row.outcome} · ${raw || "无本轮价格"}`;
    const meta = document.createElement("p");
    meta.className = "card-meta";
    meta.textContent = `性价比 ${stars(row.value_score)} · 可靠性 ${stars(row.reliability)} · 超售 ${oversellLabel(row.oversell)}`;
    if (row.reliability_note) {
      meta.title = row.reliability_note;
    }
    card.append(title, detail, meta);
    cardsNode.append(card);
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
  const header = ["provider", "plan_name", "region", "outcome", "amount", "currency",
    "billing_period", "monthly_amount", "value_score", "reliability", "oversell",
    "availability", "rejection_reason"];
  const escape = (value) => {
    const text = value === null || value === undefined ? "" : String(value);
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  const lines = [header.join(",")];
  rowsToExport.forEach((row) => {
    lines.push(header.map((field) => escape(row[field])).join(","));
  });
  const blob = new Blob(["\uFEFF" + lines.join("\n")], {type: "text/csv;charset=utf-8"});
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "vps-promotions.csv";
  link.click();
  URL.revokeObjectURL(link.href);
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
    render();
    loadDeals();
  } catch (_error) {
    setState("structure-blocked", "structure-blocked：公开数据加载或校验失败");
  }
}

[searchBox, providerFilter, outcomeFilter, regionFilter, currencyFilter, billingFilter,
  availabilityFilter, routeFilter, reliabilityFilter, sortOrder].forEach((node) =>
  node.addEventListener("input", render));
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
