"use strict";

const stateNode = document.getElementById("state");
const bodyNode = document.getElementById("status-body");
const cardsNode = document.getElementById("cards");
const providerFilter = document.getElementById("provider-filter");
const outcomeFilter = document.getElementById("outcome-filter");
const regionFilter = document.getElementById("region-filter");
const currencyFilter = document.getElementById("currency-filter");
const billingFilter = document.getElementById("billing-filter");
const availabilityFilter = document.getElementById("availability-filter");
const sortOrder = document.getElementById("sort-order");
const historyLoad = document.getElementById("history-load");
const historyList = document.getElementById("history-list");
const historyPrev = document.getElementById("history-prev");
const historyNext = document.getElementById("history-next");
const historyPageNode = document.getElementById("history-page");
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

function numeric(row, field) {
  const value = Number(row[field]);
  return row.outcome === "success" && Number.isFinite(value) ? value : Number.POSITIVE_INFINITY;
}

function timestamp(row) {
  const value = Date.parse(row.finished_at || row.checked_at || "");
  return Number.isFinite(value) ? value : 0;
}

function render() {
  bodyNode.replaceChildren();
  cardsNode.replaceChildren();
  const region = regionFilter.value.trim().toLowerCase();
  const visible = rows.filter((row) =>
    (!providerFilter.value || row.provider === providerFilter.value) &&
    (!outcomeFilter.value || row.outcome === outcomeFilter.value) &&
    (!region || String(row.region || "").toLowerCase().includes(region)) &&
    (!currencyFilter.value || row.currency === currencyFilter.value) &&
    (!billingFilter.value || row.billing_period === billingFilter.value) &&
    (!availabilityFilter.value || row.availability === availabilityFilter.value)
  );
  visible.sort(sortOrder.value === "monthly"
    ? (left, right) => numeric(left, "monthly_amount") - numeric(right, "monthly_amount")
    : sortOrder.value === "amount"
      ? (left, right) => numeric(left, "amount") - numeric(right, "amount")
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
      cell(monthly), cell(row.availability), cell(row.rejection_reason || row.block_reason));
    bodyNode.append(tr);

    const card = document.createElement("article");
    const title = document.createElement("h2");
    title.textContent = `${row.provider} · ${row.plan_name}`;
    const detail = document.createElement("p");
    detail.textContent = `${row.region} · ${row.outcome} · ${raw || "无本轮价格"}`;
    card.append(title, detail);
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
  } catch (_error) {
    historyPageNode.textContent = "历史加载失败";
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
    render();
  } catch (_error) {
    setState("structure-blocked", "structure-blocked：公开数据加载或校验失败");
  }
}

[providerFilter, outcomeFilter, regionFilter, currencyFilter, billingFilter,
  availabilityFilter, sortOrder].forEach((node) => node.addEventListener("input", render));
historyLoad.addEventListener("click", loadHistory);
historyPrev.addEventListener("click", () => {
  historyPage -= 1;
  renderHistory();
});
historyNext.addEventListener("click", () => {
  historyPage += 1;
  renderHistory();
});
load();
