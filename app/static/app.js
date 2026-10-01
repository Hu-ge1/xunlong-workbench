(() => {
  "use strict";

  const API_TIMEOUT = 30000;
  const DEFAULT_CODE = "002354";
  const DEFAULT_RULEBOOK_VERSION = "rulebook-v1-2026.07.18";
  const VIEWS = new Set(["overview", "intel", "overnight", "screener", "oversold", "yijiner", "dixi", "maifu", "stock", "review", "backtest", "automation", "settings"]);
  const DIMENSION_LABELS = {
    trend: "趋势",
    volume_price: "量价",
    volume: "量价",
    fund: "资金",
    market: "大盘",
    orderbook: "盘口",
    volatility: "波动",
    turnover: "换手",
    valuation: "估值",
    position: "位置",
    gap: "缺口",
    ma5: "MA5",
    kdj: "KDJ",
    macd: "MACD",
    ma10: "MA10"
  };

  const state = {
    view: "overview",
    loaded: new Set(),
    overview: null,
    intel: null,
    overnight: null,
    screener: { mode: "dragon", data: null, context: {}, filter: "", signal: "", live: null, liveBusy: false },
    oversold: null,
    yijiner: null,
    dixi: null,
    maifu: null,
    maifuNews: { filter: "all", keyword: "" },
    stock: { code: DEFAULT_CODE, analysis: null, deep: null, tab: "analysis", requestId: 0 },
    review: null,
    backtests: null,
    jobs: [],
    settings: null,
    sources: [],
    health: { checked: false, status: "unknown" },
    chart: { candles: [], hoverIndex: -1, bound: false },
    autoRefreshBusy: false
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  function initIcons() {
    if (window.lucide && typeof window.lucide.createIcons === "function") {
      window.lucide.createIcons({ attrs: { "stroke-width": 1.8 } });
    }
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function firstDefined(...values) {
    return values.find((value) => value !== undefined && value !== null && value !== "");
  }

  function asArray(value) {
    if (Array.isArray(value)) return value;
    if (value && typeof value === "object") return Object.values(value);
    return [];
  }

  function unwrap(payload) {
    if (!payload || typeof payload !== "object") return payload ?? {};
    if (payload.data && typeof payload.data === "object") return payload.data;
    if (payload.result && typeof payload.result === "object" && !Array.isArray(payload.result)) return payload.result;
    return payload;
  }

  function toNumber(value, fallback = 0) {
    if (typeof value === "string") value = value.replaceAll(",", "").replace("%", "");
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function formatNumber(value, digits = 0) {
    if (value === undefined || value === null || value === "") return "--";
    const number = toNumber(value, NaN);
    if (!Number.isFinite(number)) return String(value);
    return number.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  function formatCompact(value) {
    const number = toNumber(value, NaN);
    if (!Number.isFinite(number)) return "--";
    const absolute = Math.abs(number);
    if (absolute >= 1e8) return `${(number / 1e8).toFixed(2)}亿`;
    if (absolute >= 1e4) return `${(number / 1e4).toFixed(1)}万`;
    return formatNumber(number, absolute < 10 && absolute % 1 ? 2 : 0);
  }

  function formatPercent(value, digits = 2) {
    if (value === undefined || value === null || value === "") return "--";
    const number = toNumber(value, NaN);
    if (!Number.isFinite(number)) return "--";
    return `${number > 0 ? "+" : ""}${number.toFixed(digits)}%`;
  }

  function formatDateTime(value) {
    if (!value) return "--";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false
    }).format(date);
  }

  function formatQuoteTime(value) {
    if (!value) return "--";
    const compact = String(value).match(/^\d{8}(\d{2})(\d{2})(\d{2})$/);
    if (compact) return `${compact[1]}:${compact[2]}:${compact[3]}`;
    const formatted = formatDateTime(value);
    return formatted === "--" ? String(value) : formatted.split(" ").pop();
  }

  function localDate(date = new Date()) {
    const offset = date.getTimezoneOffset() * 60000;
    return new Date(date.getTime() - offset).toISOString().slice(0, 10);
  }

  function changeClass(value) {
    const number = toNumber(value, 0);
    return number > 0 ? "up" : number < 0 ? "down" : "muted";
  }

  function signalMeta(value, score) {
    const normalized = String(value ?? "").toLowerCase();
    const numeric = toNumber(score, NaN);
    if (normalized.includes("强多") || normalized.includes("strong") || normalized.includes("fire") || numeric >= 80) {
      return { key: "strong", label: "强多", icon: "flame" };
    }
    if (normalized.includes("偏多") || normalized.includes("bull") || normalized.includes("多") || numeric >= 65) {
      return { key: "bullish", label: "偏多", icon: "trending-up" };
    }
    if (normalized.includes("强空") || normalized.includes("bear") || normalized.includes("空") || (Number.isFinite(numeric) && numeric < 40)) {
      return { key: "bearish", label: normalized.includes("强") ? "强空" : "偏空", icon: "trending-down" };
    }
    return { key: "neutral", label: "中性", icon: "minus" };
  }

  // Keep API values stable while presenting the current rulebook terminology.
  function strategyText(value) {
    return String(value ?? "")
      .replaceAll("四维技术评分", "规则库综合")
      .replaceAll("十维评分", "规则库综合")
      .replaceAll("多维评分", "规则库综合")
      .replaceAll("技术评分", "规则库综合")
      .replaceAll("技术筛选", "规则库综合")
      .replaceAll("技术指标", "规则库因子")
      .replaceAll("竞价推送", "事件确认")
      .replaceAll("竞价筛选", "事件确认")
      .replaceAll("竞价强度", "事件确认")
      .replaceAll("竞价寻龙", "事件确认")
      .replaceAll("竞价", "事件确认")
      .replaceAll("昨日首板", "沪深主板")
      .replaceAll("首板池", "沪深主板池")
      .replaceAll("一进二", "事件确认");
  }

  function strategyModeLabel(value) {
    const raw = value && typeof value === "object"
      ? firstDefined(value.label, value.name, value.mode, value.strategy_mode, value.type)
      : value;
    const normalized = String(raw ?? "").toLowerCase();
    if (normalized.includes("auction") || normalized.includes("event") || normalized.includes("竞价") || normalized.includes("事件")) return "竞价验证";
    if (normalized.includes("value") || normalized.includes("价值")) return "价值型";
    if (normalized.includes("growth") || normalized.includes("成长")) return "成长型";
    if (normalized.includes("trend") || normalized.includes("趋势")) return "趋势型";
    if (normalized.includes("dragon") || normalized.includes("擒龙")) return "擒龙模式";
    if (normalized.includes("balanced") || normalized.includes("综合")) return "规则库综合";
    if (normalized.includes("technical") || normalized.includes("rulebook") || normalized.includes("score") || normalized.includes("规则")) return "规则库综合";
    return strategyText(raw) || "--";
  }

  function riskStatusMeta(candidate) {
    const explicit = firstDefined(
      candidate.risk_status,
      candidate.risk_state,
      candidate.risk_label,
      candidate.risk_level,
      candidate.risk_result,
      candidate.risk
    );
    const pass = firstDefined(candidate.risk_pass, candidate.risk_ok, candidate.risk_approved);
    const rejected = firstDefined(candidate.risk_rejected, candidate.risk_blocked);
    if (explicit === undefined && pass === undefined && rejected === undefined) return null;
    const riskObject = explicit && typeof explicit === "object" ? explicit : null;
    const objectRejected = riskObject && firstDefined(riskObject.hard_veto, riskObject.rejected, riskObject.blocked);
    const objectPass = riskObject && firstDefined(riskObject.eligible, riskObject.pass, riskObject.ok);
    const scalarLabel = (value) => {
      if (!value || typeof value !== "object") return value;
      return firstDefined(value.label, value.name, value.status, value.state, value.reason, value.message, value.code);
    };
    const objectLabel = riskObject && scalarLabel(firstDefined(
      riskObject.status,
      riskObject.label,
      riskObject.state,
      riskObject.reason,
      riskObject.message
    ));
    const explicitLabel = scalarLabel(explicit);
    const flags = (riskObject ? asArray(firstDefined(riskObject.hard_flags, riskObject.soft_flags, riskObject.flags, [])) : [])
      .map(scalarLabel)
      .filter((value) => value !== undefined && value !== null && value !== "")
      .map((value) => String(value));
    const text = `${String(objectLabel ?? explicitLabel ?? "")} ${flags.join(" ")}`.toLowerCase();
    if (rejected === true || objectRejected === true || pass === false || objectPass === false || /否决|拒绝|阻断|高风险|不通过|reject|block|fail|danger/.test(text)) {
      return { label: strategyText(flags[0] || objectLabel || explicitLabel || "风险否决"), className: "bearish" };
    }
    if (pass === true || objectPass === true || /通过|安全|低风险|正常|pass|safe|ok/.test(text)) {
      return { label: strategyText(objectLabel || explicitLabel || "风险通过"), className: "bullish" };
    }
    return { label: strategyText(objectLabel || explicitLabel || "风险观察"), className: "warning" };
  }

  function coverageValue(candidate, context = {}) {
    const snapshot = candidate.snapshot && typeof candidate.snapshot === "object" ? candidate.snapshot : {};
    const rulebook = candidate.rulebook && typeof candidate.rulebook === "object"
      ? candidate.rulebook
      : snapshot.rulebook && typeof snapshot.rulebook === "object" ? snapshot.rulebook : {};
    const rulebookCoverage = rulebook.data_coverage && typeof rulebook.data_coverage === "object"
      ? firstDefined(rulebook.data_coverage.selected_mode, rulebook.data_coverage.coverage, rulebook.data_coverage.ratio)
      : undefined;
    const raw = firstDefined(
      candidate.coverage,
      candidate.coverage_pct,
      candidate.coverage_rate,
      candidate.coverage_ratio,
      candidate.market_coverage,
      candidate.universe_coverage,
      candidate.data_coverage,
      rulebookCoverage,
      context.coverage,
      context.coverage_pct,
      context.coverage_rate
    );
    if (raw && typeof raw === "object") {
      const covered = firstDefined(raw.covered, raw.scanned, raw.count, raw.numerator);
      const total = firstDefined(raw.total, raw.universe, raw.denominator);
      if (covered !== undefined && total !== undefined && toNumber(total, 0) > 0) return toNumber(covered, 0) / toNumber(total, 1);
      return firstDefined(raw.pct, raw.percent, raw.rate, raw.ratio, raw.selected_mode, raw.coverage, raw.value);
    }
    return raw;
  }

  function formatCoverage(value) {
    if (value === undefined || value === null || value === "") return "--";
    if (typeof value === "string" && value.includes("/")) return value;
    const number = toNumber(value, NaN);
    if (!Number.isFinite(number)) return strategyText(value);
    const ratio = Math.abs(number) <= 1 ? number * 100 : number;
    return `${ratio.toFixed(ratio % 1 ? 1 : 0)}%`;
  }

  function scoreClass(score) {
    const value = toNumber(score, 0);
    if (Math.abs(value) <= 12) return value >= 8 ? "high" : value >= 3 ? "mid" : "low";
    return value >= 75 ? "high" : value >= 55 ? "mid" : "low";
  }

  function setUpdatedAt(value, delayed = false) {
    const element = $("#updated-at");
    if (!element) return;
    if (!value) {
      element.textContent = "数据时间未知";
      return;
    }
    element.textContent = `${delayed ? "数据延迟" : "数据时间"} ${formatDateTime(value)}`;
    element.classList.toggle("delayed", Boolean(delayed));
    element.dataset.tooltip = delayed ? "当前展示的是可获得的最近数据，不是本次请求时间" : "这里显示行情源时间，不是页面刷新时间";
  }

  function updateTradingState(market = {}) {
    const chip = $("#trading-chip");
    if (!chip) return;
    const session = String(market.session || "").toLowerCase();
    const labels = {
      preopen: "盘前",
      auction: "集合竞价",
      morning: "交易中",
      midday: "午间休市",
      afternoon: "交易中",
      postclose: "已收盘",
      weekend: "非交易日"
    };
    const live = Boolean(market.realtime) && ["auction", "morning", "afternoon"].includes(session);
    const delayed = Boolean(market.data_delayed || market.stale);
    chip.innerHTML = `<span class="status-dot ${live ? "good" : delayed ? "bad" : ""}"></span><span>${escapeHtml(delayed ? "行情延迟" : labels[session] || "交易状态未知")}</span>`;
  }

  function updateHealth(status, detail) {
    const dot = $("#sidebar-health-dot");
    const label = $("#sidebar-health");
    const sub = $("#sidebar-health-detail");
    if (!dot || !label || !sub) return;
    const normalized = typeof status === "boolean" ? (status ? "ok" : "error") : String(status || "unknown").toLowerCase();
    const degraded = normalized === "degraded" || normalized === "warning";
    const ok = normalized === "ok" || normalized === "healthy" || normalized === "connected";
    dot.className = `status-dot ${degraded ? "warn" : ok ? "good" : normalized === "unknown" ? "" : "bad"}`;
    label.textContent = degraded ? "数据服务降级" : ok ? "数据服务正常" : normalized === "unknown" ? "数据服务检测中" : "数据服务异常";
    sub.textContent = detail || (degraded ? "部分数据正在使用降级来源" : ok ? "接口与数据源响应正常" : normalized === "unknown" ? "等待首次响应" : "请检查后端服务");
    state.health = { checked: normalized !== "unknown", status: normalized, detail: sub.textContent };
  }

  function applyHealthSnapshot(snapshot) {
    if (!snapshot || typeof snapshot !== "object") return;
    const provider = snapshot.provider && typeof snapshot.provider === "object" ? snapshot.provider : snapshot;
    const status = firstDefined(snapshot.status, provider.status, snapshot.ok === false ? "error" : undefined, "unknown");
    const reasons = asArray(firstDefined(provider.degraded_reasons, snapshot.degraded_reasons, []));
    const detail = firstDefined(
      reasons[0],
      status === "ok" ? "接口与数据源响应正常" : undefined,
      status === "error" ? "核心数据请求暂时失败，请稍后重试" : undefined
    );
    updateHealth(status, detail);
  }

  let healthRequestBusy = false;

  async function loadHealth() {
    if (healthRequestBusy) return;
    healthRequestBusy = true;
    try {
      let lastError = null;
      for (let attempt = 0; attempt < 2; attempt += 1) {
        try {
          const health = await api("/api/health", { timeout: 15000, healthCheck: true });
          applyHealthSnapshot(health);
          return;
        } catch (error) {
          lastError = error;
          if (attempt === 0) await new Promise((resolve) => window.setTimeout(resolve, 700));
        }
      }
      updateHealth("error", lastError?.message || "健康检查失败，请稍后重试");
    } finally {
      healthRequestBusy = false;
    }
  }

  function renderDataSource(market, indexes) {
    const chip = $("#data-source-chip");
    if (!chip) return;
    const source = String(firstDefined(market.source, market.index_source, "")).toLowerCase();
    const quoteDate = firstDefined(
      asArray(indexes).find((item) => item && item.quote_time)?.quote_time,
      market.as_of
    );
    const dateText = quoteDate ? String(quoteDate).replace(/[^\d]/g, "").slice(4, 8).replace(/(\d{2})(\d{2})/, "$1/$2") : "--";
    let label = "数据源待确认";
    let tooltip = "市场数据来源尚未返回";
    let tone = "neutral";
    const delayed = Boolean(market.data_delayed || market.stale);
    if (source.includes("fallback") || delayed) {
      label = `数据延迟 · ${dateText}`;
      tooltip = `当前行情数据时间 ${quoteDate || "未知"}，系统没有把请求时间冒充为行情时间`;
      tone = "warning";
    } else if (source.includes("tdx")) {
      label = `盘后通达信 · ${dateText}`;
      tooltip = `本地通达信盘后日线，数据日期 ${quoteDate || "未知"}`;
      tone = "local";
    } else if (source.includes("tencent")) {
      label = market.realtime ? `腾讯实时 · ${formatDateTime(quoteDate).slice(-8)}` : `腾讯最新收盘 · ${dateText}`;
      tooltip = `腾讯行情源时间 ${quoteDate || "未知"}`;
      tone = market.realtime ? "live" : "local";
    }
    chip.className = `data-source-chip ${tone}`;
    chip.dataset.tooltip = tooltip;
    chip.innerHTML = `<i data-lucide="${tone === "live" ? "radio" : tone === "warning" ? "triangle-alert" : "database"}" aria-hidden="true"></i><span>${escapeHtml(label)}</span>`;
  }

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timeout = Number.isFinite(options.timeout) ? options.timeout : API_TIMEOUT;
    const timer = window.setTimeout(() => controller.abort(), timeout);
    const init = {
      method: options.method || "GET",
      headers: { Accept: "application/json", ...(options.headers || {}) },
      signal: controller.signal,
      cache: "no-store"
    };
    if (options.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    try {
      const response = await fetch(path, init);
      const type = response.headers.get("content-type") || "";
      const payload = type.includes("application/json") ? await response.json() : await response.text();
      if (!response.ok) {
        const message = payload && typeof payload === "object"
          ? firstDefined(payload.detail, payload.message, payload.error)
          : payload;
        const requestError = new Error(message || `请求失败 (${response.status})`);
        requestError.status = response.status;
        throw requestError;
      }
      return unwrap(payload);
    } catch (error) {
      const message = error.name === "AbortError" ? "请求超时，请稍后重试" : error.message;
      if ((error.name === "AbortError" || !error.status) && !options.healthCheck) {
        updateHealth("error", message);
      }
      throw new Error(message);
    } finally {
      window.clearTimeout(timer);
    }
  }

  function stateMarkup(type, title, detail, retryView, compact = false) {
    const icon = type === "loading" ? "" : type === "error" ? "circle-alert" : "inbox";
    const action = retryView
      ? `<button class="button secondary compact" type="button" data-retry-view="${escapeHtml(retryView)}"><i data-lucide="refresh-cw"></i><span>重试</span></button>`
      : "";
    return `<div class="state-box${compact ? " compact-state" : ""}">
      ${type === "loading" ? '<span class="spinner" aria-hidden="true"></span>' : `<i data-lucide="${icon}" aria-hidden="true"></i>`}
      <strong>${escapeHtml(strategyText(title))}</strong>
      ${detail ? `<p>${escapeHtml(strategyText(detail))}</p>` : ""}
      ${action}
    </div>`;
  }

  function setState(target, type, title, detail, retryView, compact = false) {
    const element = typeof target === "string" ? $(target) : target;
    if (!element) return;
    element.innerHTML = stateMarkup(type, title, detail, retryView, compact);
    initIcons();
  }

  function setButtonBusy(button, busy, label = "处理中") {
    if (!button) return;
    if (busy) {
      button.dataset.originalHtml = button.innerHTML;
      button.disabled = true;
      button.innerHTML = `<span class="spinner" aria-hidden="true"></span><span>${escapeHtml(label)}</span>`;
    } else {
      button.disabled = false;
      if (button.dataset.originalHtml) button.innerHTML = button.dataset.originalHtml;
      delete button.dataset.originalHtml;
      initIcons();
    }
  }

  function showToast(kind, title, message) {
    const region = $("#toast-region");
    if (!region) return;
    const icon = kind === "error" ? "circle-alert" : kind === "warning" ? "triangle-alert" : "circle-check";
    const toast = document.createElement("div");
    toast.className = "toast";
    toast.innerHTML = `<i data-lucide="${icon}"></i><div><strong>${escapeHtml(title)}</strong><span>${escapeHtml(message || "")}</span></div><button type="button" aria-label="关闭"><i data-lucide="x"></i></button>`;
    toast.querySelector("button").addEventListener("click", () => toast.remove());
    region.appendChild(toast);
    initIcons();
    window.setTimeout(() => toast.remove(), 5200);
  }

  function renderMetrics(target, items) {
    const element = typeof target === "string" ? $(target) : target;
    if (!element) return;
    if (!items.length) {
      element.innerHTML = stateMarkup("empty", "暂无统计", "当前接口没有返回汇总数据", null, true);
      return;
    }
    element.innerHTML = items.map((item) => `<div class="metric-item">
      <div class="metric-label"><span>${escapeHtml(strategyText(item.label))}</span><i data-lucide="${item.icon || "activity"}"></i></div>
      <div class="metric-value ${item.className || ""}">${escapeHtml(item.value)}</div>
      <div class="metric-detail">${escapeHtml(strategyText(item.detail || "--"))}</div>
    </div>`).join("");
    initIcons();
  }

  function normalizeCandidate(candidate, index = 0, context = {}) {
    candidate = candidate && typeof candidate === "object" ? candidate : {};
    const metadata = candidate.metadata && typeof candidate.metadata === "object" ? candidate.metadata : {};
    const snapshot = candidate.snapshot && typeof candidate.snapshot === "object" ? candidate.snapshot : {};
    const rulebook = candidate.rulebook && typeof candidate.rulebook === "object"
      ? candidate.rulebook
      : snapshot.rulebook && typeof snapshot.rulebook === "object" ? snapshot.rulebook : {};
    const technical = snapshot.technical && typeof snapshot.technical === "object" ? snapshot.technical : {};
    const rulebookRisk = rulebook.risk && typeof rulebook.risk === "object" ? rulebook.risk : undefined;
    const scores = candidate.scores || candidate.score_breakdown || candidate.dimensions || candidate.breakdown || {};
    // Dragon-mode breakdowns can deliberately leave unavailable factors as null.
    // Do not treat null as an object while normalizing the legacy table shape.
    const scoreArray = Array.isArray(scores) ? scores : Object.entries(scores).map(([key, value]) => ({
      key,
      score: value && typeof value === "object" ? value.score : value
    }));
    const getDimension = (name) => {
      const item = scoreArray.find((entry) => String(firstDefined(entry.key, entry.name, entry.dimension, "")).toLowerCase() === name);
      return item ? firstDefined(item.score, item.value, item.points) : firstDefined(candidate[name], scores[name]);
    };
    const technicalNumber = (value) => (value === undefined || value === null || value === "" ? undefined : formatNumber(value, 2));
    const technicalKdj = [technical.kdj_k, technical.kdj_d, technical.kdj_j];
    const technicalMacd = [technical.macd_dif, technical.macd_dea, technical.macd_hist];
    const kdjText = technicalKdj.every((value) => value !== undefined && value !== null)
      ? `K ${formatNumber(technical.kdj_k, 1)} / D ${formatNumber(technical.kdj_d, 1)} / J ${formatNumber(technical.kdj_j, 1)}`
      : undefined;
    const macdText = technicalMacd.every((value) => value !== undefined && value !== null)
      ? `DIF ${formatNumber(technical.macd_dif, 2)} / DEA ${formatNumber(technical.macd_dea, 2)} / 柱 ${formatNumber(technical.macd_hist, 2)}`
      : undefined;
    const totalScore = firstDefined(candidate.total_score, candidate.score, candidate.total, candidate.points, 0);
    const strategyMode = firstDefined(
      candidate.strategy_mode,
      candidate.strategyMode,
      candidate.mode,
      candidate.strategy,
      candidate.selected_mode_label,
      candidate.selected_mode,
      metadata.strategy_mode,
      metadata.rulebook_mode,
      rulebook.selected_mode_label,
      rulebook.selected_mode,
      context.strategy_mode,
      context.mode,
      context.run_type
    );
    const riskStatus = firstDefined(
      candidate.risk_status,
      candidate.risk_state,
      candidate.risk_label,
      candidate.risk_level,
      candidate.risk_result,
      candidate.risk,
      metadata.risk_status,
      rulebookRisk,
      context.risk_status,
      context.risk_state
    );
    return {
      rank: firstDefined(candidate.rank, index + 1),
      code: String(firstDefined(candidate.code, candidate.stock_code, candidate.symbol, "")).replace(/^(sh|sz)/i, ""),
      name: firstDefined(candidate.name, candidate.stock_name, candidate.security_name, "未知股票"),
      industry: firstDefined(candidate.industry, candidate.sector, candidate.board, "其他"),
      boards: firstDefined(candidate.consecutive_boards, candidate.board_count, snapshot.consecutive_boards, snapshot.board_count, candidate.boards, null),
      score: toNumber(totalScore, 0),
      signal: firstDefined(candidate.signal, candidate.label, candidate.direction, ""),
      ma5: firstDefined(technicalNumber(technical.ma5), getDimension("ma5"), candidate.ma5_score, "--"),
      kdj: firstDefined(kdjText, getDimension("kdj"), candidate.kdj_score, "--"),
      macd: firstDefined(macdText, getDimension("macd"), candidate.macd_score, "--"),
      ma10: firstDefined(technicalNumber(technical.ma10), getDimension("ma10"), candidate.ma10_score, "--"),
      gap: firstDefined(candidate.gap_pct, candidate.gap, candidate.auction_gap, null),
      change: firstDefined(candidate.change_pct, candidate.pct_change, candidate.change, null),
      auction_price: firstDefined(candidate.auction_price, candidate.live_auction && candidate.live_auction.auction_price, null),
      auction_amount: firstDefined(candidate.auction_amount, candidate.live_auction && candidate.live_auction.auction_amount, null),
      auction_to_float_mcap_pct: firstDefined(candidate.auction_to_float_mcap_pct, snapshot.auction_to_float_mcap_pct, null),
      matched_volume_lots: firstDefined(candidate.matched_volume_lots, candidate.live_auction && candidate.live_auction.matched_volume_lots, null),
      unmatched_direction: firstDefined(candidate.unmatched_direction, candidate.live_auction && candidate.live_auction.unmatched_direction, "--"),
      unmatched_volume_lots: firstDefined(candidate.unmatched_volume_lots, candidate.live_auction && candidate.live_auction.unmatched_volume_lots, null),
      quote_time: firstDefined(candidate.quote_time, candidate.data_as_of, candidate.live_auction && candidate.live_auction.quote_time, null),
      auction_source: firstDefined(candidate.auction_source, candidate.source, candidate.live_auction && candidate.live_auction.source, ""),
      live_status: firstDefined(candidate.live_status, candidate.live_auction && candidate.live_auction.live_status, ""),
      live_available: Boolean(firstDefined(candidate.available, candidate.live_auction && candidate.live_auction.available, false)),
      pushed: Boolean(firstDefined(candidate.pushed, candidate.is_pushed, candidate.recommended, candidate.decision === "push", false)),
      reason: firstDefined(candidate.reason, candidate.decision_reason, candidate.summary, candidate.evidence, candidate.trigger, "符合当前筛选条件"),
      strategy_mode: strategyMode,
      risk_status: riskStatus,
      risk_pass: firstDefined(candidate.risk_pass, candidate.risk_ok, candidate.risk_approved, rulebookRisk && rulebookRisk.eligible, metadata.risk_pass, context.risk_pass),
      risk_rejected: firstDefined(candidate.risk_rejected, candidate.risk_blocked, rulebookRisk && rulebookRisk.hard_veto, metadata.risk_rejected, context.risk_rejected),
      coverage: coverageValue(candidate, { ...context, ...metadata })
    };
  }

  function candidateTable(candidates, options = {}) {
    if (!candidates.length) return stateMarkup("empty", "暂无候选", "本次运行没有股票通过筛选", null, true);
    const compact = Boolean(options.compact);
    const showMeta = options.showMeta !== false;
    const rows = candidates.map((item, index) => normalizeCandidate(item, index, options.context || {}));
    const showStrategyMode = showMeta && rows.some((item) => item.strategy_mode !== undefined && item.strategy_mode !== null && item.strategy_mode !== "");
    const showRiskStatus = showMeta && rows.some((item) => riskStatusMeta(item));
    const showCoverage = showMeta && rows.some((item) => item.coverage !== undefined && item.coverage !== null && item.coverage !== "");
    const showLiveAuction = Boolean(options.liveAuction);
    const optionalHeaders = `${showStrategyMode ? '<th class="mobile-hide">策略模式</th>' : ""}${showRiskStatus ? '<th class="mobile-hide">风险状态</th>' : ""}${showCoverage ? '<th class="numeric mobile-hide">覆盖率</th>' : ""}`;
    return `<table>
      <thead><tr><th>排名</th><th>股票</th><th class="numeric">连板</th><th>行业</th><th class="numeric">总分</th><th>信号</th>${compact ? "" : '<th class="numeric">MA5</th><th class="numeric">KDJ</th><th class="numeric">MACD</th><th class="numeric">MA10</th>'}${optionalHeaders}${showLiveAuction ? '<th class="numeric">竞价价</th><th class="numeric">竞价额</th><th class="numeric">竞价/流通市值</th><th class="numeric mobile-hide">未匹配</th><th class="mobile-hide">时间</th>' : ""}<th class="numeric">Gap</th><th class="numeric">涨跌</th><th>状态</th><th class="mobile-hide">入选依据</th><th aria-label="操作"></th></tr></thead>
      <tbody>${rows.map((candidate) => {
        const signal = signalMeta(candidate.signal, candidate.score);
        const risk = riskStatusMeta(candidate);
        return `<tr data-stock-code="${escapeHtml(candidate.code)}">
          <td>${escapeHtml(candidate.rank)}</td>
          <td class="stock-cell"><strong>${escapeHtml(candidate.name)}</strong><small>${escapeHtml(candidate.code)}</small></td>
          <td class="numeric">${candidate.boards === null || candidate.boards === undefined || candidate.boards === "" ? "--" : `${formatNumber(candidate.boards, 0)}板`}</td>
          <td>${escapeHtml(candidate.industry)}</td>
          <td class="numeric"><span class="score-pill ${scoreClass(candidate.score)}">${formatNumber(candidate.score)}</span></td>
          <td><span class="status-badge ${signal.key}">${escapeHtml(signal.label)}</span></td>
          ${compact ? "" : `<td class="numeric">${escapeHtml(candidate.ma5)}</td><td class="numeric">${escapeHtml(candidate.kdj)}</td><td class="numeric">${escapeHtml(candidate.macd)}</td><td class="numeric">${escapeHtml(candidate.ma10)}</td>`}
          ${showStrategyMode ? `<td class="mobile-hide">${escapeHtml(strategyModeLabel(candidate.strategy_mode))}</td>` : ""}
          ${showRiskStatus ? `<td class="mobile-hide">${risk ? `<span class="status-badge ${risk.className}">${escapeHtml(risk.label)}</span>` : "--"}</td>` : ""}
          ${showCoverage ? `<td class="numeric mobile-hide">${formatCoverage(candidate.coverage)}</td>` : ""}
          ${showLiveAuction ? `<td class="numeric" title="${escapeHtml([candidate.live_status, candidate.auction_source].filter(Boolean).join(" · "))}">${formatNumber(candidate.auction_price, 2)}</td><td class="numeric">${formatCompact(candidate.auction_amount)}</td><td class="numeric">${candidate.auction_to_float_mcap_pct == null ? "--" : `${formatNumber(candidate.auction_to_float_mcap_pct, 2)}%`}</td><td class="numeric mobile-hide">${candidate.unmatched_volume_lots ? `${escapeHtml(candidate.unmatched_direction)} ${formatCompact(candidate.unmatched_volume_lots)}手` : "--"}</td><td class="mobile-hide">${escapeHtml(formatQuoteTime(candidate.quote_time))}</td>` : ""}
          <td class="numeric ${changeClass(candidate.gap)}">${formatPercent(candidate.gap)}</td>
          <td class="numeric ${changeClass(candidate.change)}">${formatPercent(candidate.change)}</td>
          <td><span class="status-badge ${candidate.pushed ? "bullish" : "neutral"}">${candidate.pushed ? "已推送" : "候选"}</span></td>
          <td class="reason-cell mobile-hide" title="${escapeHtml(strategyText(candidate.reason))}">${escapeHtml(strategyText(candidate.reason))}</td>
          <td><button class="icon-button" type="button" aria-label="打开 ${escapeHtml(candidate.name)}" data-tooltip="打开个股分析"><i data-lucide="chevron-right"></i></button></td>
        </tr>`;
      }).join("")}</tbody>
    </table>`;
  }

  function bindCandidateRows(root) {
    $$('[data-stock-code]', root).forEach((row) => {
      row.addEventListener("click", () => openStock(row.dataset.stockCode));
      row.addEventListener("keydown", (event) => {
        if (event.key === "Enter") openStock(row.dataset.stockCode);
      });
      row.tabIndex = 0;
    });
  }

  function renderMarketStrip(indexes) {
    const element = $("#market-strip");
    if (!element) return;
    if (!indexes.length) {
      element.innerHTML = '<div class="market-strip-placeholder">指数数据暂不可用</div>';
      return;
    }
    element.innerHTML = indexes.slice(0, 8).map((item) => {
      const change = firstDefined(item.change_pct, item.pct_change, item.change, 0);
      return `<div class="index-ticker"><span><span class="ticker-name">${escapeHtml(firstDefined(item.name, item.index_name, item.code, "指数"))}</span><strong>${formatNumber(firstDefined(item.price, item.value, item.close), 2)}</strong></span><span class="ticker-change ${changeClass(change)}">${formatPercent(change)}</span></div>`;
    }).join("");
  }

  function extractCandidates(data) {
    return asArray(firstDefined(
      data.candidates,
      data.stocks,
      data.items,
      data.results,
      data.latest_run && data.latest_run.candidates,
      data.run && data.run.candidates,
      []
    ));
  }

  function runContext(data = {}, run = {}, summary = {}) {
    const metadata = firstDefined(data.metadata, run.metadata, {}) || {};
    const coverage = firstDefined(
      data.coverage,
      data.coverage_pct,
      data.coverage_rate,
      data.market_coverage,
      run.coverage,
      run.coverage_pct,
      run.coverage_rate,
      metadata.coverage,
      metadata.coverage_pct,
      metadata.coverage_rate,
      metadata.coverage_ratio,
      metadata.rulebook_coverage,
      summary.coverage,
      summary.coverage_pct,
      summary.coverage_rate
    );
    return {
      strategy_mode: firstDefined(data.strategy_mode, data.strategyMode, data.mode, data.run_type, data.rulebook_mode, run.strategy_mode, run.strategyMode, run.mode, run.run_type, run.rulebook_mode, metadata.rulebook_mode),
      risk_status: firstDefined(data.risk_status, data.risk_state, data.risk, run.risk_status, run.risk_state, run.risk, metadata.risk_status, metadata.risk),
      risk_pass: firstDefined(data.risk_pass, run.risk_pass, metadata.risk_pass),
      risk_rejected: firstDefined(data.risk_rejected, run.risk_rejected, metadata.risk_rejected),
      coverage
    };
  }

  async function loadOverview(force = false) {
    if (state.loaded.has("overview") && !force) return;
    renderMetrics("#overview-metrics", [
      { label: "加载中", value: "--", detail: "正在汇总市场数据", icon: "loader-circle" },
      { label: "加载中", value: "--", detail: "正在读取筛选任务", icon: "loader-circle" },
      { label: "加载中", value: "--", detail: "正在读取候选", icon: "loader-circle" },
      { label: "加载中", value: "--", detail: "正在读取消息状态", icon: "loader-circle" }
    ]);
    setState("#overview-candidates", "loading", "正在加载候选", "", null, true);
    setState("#overview-message", "loading", "正在加载消息", "", null, true);
    setState("#market-breadth", "loading", "正在加载市场宽度", "", null, true);
    setState("#overview-runtime", "loading", "正在加载任务状态", "", null, true);
    try {
      const data = await api("/api/overview");
      state.overview = data || {};
      state.loaded.add("overview");
      renderOverview(state.overview);
      const market = firstDefined(data.market, data.market_snapshot, {});
      setUpdatedAt(firstDefined(market.data_as_of, market.as_of, data.data_as_of), Boolean(market.data_delayed || market.stale));
    } catch (error) {
      setState("#overview-candidates", "error", "候选加载失败", error.message, "overview");
      setState("#overview-message", "error", "消息状态加载失败", error.message, "overview");
      setState("#market-breadth", "error", "市场数据加载失败", error.message, "overview");
      setState("#overview-runtime", "error", "任务状态加载失败", error.message, "overview");
      renderMetrics("#overview-metrics", []);
      showToast("error", "总览加载失败", error.message);
    }
  }

  function renderOverview(data) {
    const market = firstDefined(data.market, data.market_snapshot, data.market_summary, {});
    const run = firstDefined(data.latest_run, data.screen_run, data.run, {});
    const summary = firstDefined(data.summary, data.stats, run.summary, {});
    const candidateContext = runContext(data, run, summary);
    const candidates = extractCandidates({ ...data, candidates: firstDefined(data.candidates, run.candidates) })
      .map((item, index) => normalizeCandidate(item, index, candidateContext));
    const indexes = asArray(firstDefined(data.indexes, market.indexes, market.indices, data.market_indices, []));
    const marketScore = firstDefined(market.signal_score, market.score, market.index_score, summary.market_score, 0);
    const marketSignal = signalMeta(firstDefined(market.label, market.signal, market.direction), marketScore);
    const firstBoards = firstDefined(summary.first_boards, summary.yesterday_first_boards, run.first_boards, run.first_board_count, run.total, 0);
    const candidateCount = firstDefined(summary.candidates, summary.candidate_count, run.candidate_count, candidates.length);
    const pushCount = firstDefined(summary.pushed, summary.push_count, run.push_count, candidates.filter((item) => normalizeCandidate(item).pushed).length);

    const system = data.system && typeof data.system === "object" ? data.system : {};
    applyHealthSnapshot(system);

    renderMetrics("#overview-metrics", [
      { label: "上证信号", value: `${formatNumber(marketScore)} 分`, detail: marketSignal.label, icon: marketSignal.icon, className: marketSignal.key === "bearish" ? "down" : marketSignal.key === "neutral" ? "" : "up" },
      { label: "沪深主板", value: formatNumber(firstDefined(summary.total, summary.scanned, run.universe_count, firstBoards)), detail: `覆盖率 ${formatCoverage(candidateContext.coverage) === "--" ? "待返回" : formatCoverage(candidateContext.coverage)}`, icon: "database" },
      { label: "当前候选", value: formatNumber(candidateCount), detail: `规则库门槛 ${formatNumber(firstDefined(summary.min_score, run.min_score, 0))}`, icon: "scan-search" },
      { label: "今日推送", value: formatNumber(pushCount), detail: pushCount ? "消息已生成" : "宁错过，不做错", icon: "send", className: pushCount ? "up" : "" }
    ]);

    renderDataSource(market, indexes);
    updateTradingState(market);
    renderMarketStrip(indexes);
    const runTradeDate = firstDefined(run.trade_date, run.date);
    const marketTradeDate = firstDefined(market.trade_date, data.trade_date);
    const mismatch = runTradeDate && marketTradeDate && String(runTradeDate).slice(0, 10) !== String(marketTradeDate).slice(0, 10);
    $("#overview-subtitle").textContent = `${mismatch ? "候选池与行情日期不一致 · " : ""}行情 ${formatDateTime(firstDefined(market.data_as_of, market.as_of))}${runTradeDate ? ` · 候选池 ${String(runTradeDate).slice(0, 10)}` : ""}`;
    renderFunnel(firstDefined(run.funnel, run.metadata && run.metadata.funnel, data.funnel, summary.funnel, {}), { firstBoards, candidateCount, pushCount, summary, run, context: candidateContext });

    const candidatesElement = $("#overview-candidates");
    candidatesElement.innerHTML = candidateTable(candidates.slice(0, 8), { compact: true, showMeta: false, context: candidateContext });
    bindCandidateRows(candidatesElement);

    renderMessage(firstDefined(data.message, data.message_preview, run.message, run.message_preview, {}), pushCount, candidates);
    renderBreadth(market);
    renderRuntime(asArray(firstDefined(data.jobs, data.runtime, data.tasks, [])), asArray(firstDefined(data.data_sources, data.sources, [])));
    initIcons();
  }

  function renderFunnel(raw, context) {
    const element = $("#overview-funnel");
    const funnel = Array.isArray(raw) ? raw : [];
    const map = raw && !Array.isArray(raw) ? raw : {};
    const steps = funnel.length ? funnel.map((item, index) => ({
      label: strategyText(firstDefined(item.label, item.name, `阶段 ${index + 1}`)),
      value: firstDefined(item.value, item.count, 0),
      detail: strategyText(firstDefined(item.detail, item.description, ""))
    })) : [
      { label: "沪深主板", value: firstDefined(map.market, map.total_market, context.summary.total_market, context.run.universe_count, 0), detail: "排除创业板、科创板和北交所" },
      { label: "事件确认", value: context.firstBoards, detail: "全市场事件条件确认" },
      { label: "前置过滤", value: firstDefined(map.prefilter, map.pre_filtered, context.summary.pre_filtered, 0), detail: "量比与市值" },
      { label: "风险闸门", value: firstDefined(map.gates, map.filtered, context.run.filtered_count, 0), detail: "硬条件通过" },
      { label: "规则库综合", value: context.candidateCount, detail: "规则库因子评分" },
      { label: "最终推送", value: context.pushCount, detail: "最多 3 只" }
    ];
    element.innerHTML = steps.slice(0, 6).map((item, index) => `<button class="funnel-step${index === steps.length - 1 ? " active" : ""}" type="button" data-funnel-step="${index}"><span>${escapeHtml(strategyText(item.label))}</span><strong>${formatNumber(item.value)}</strong><small>${escapeHtml(strategyText(item.detail))}</small></button>`).join("");
    $("#funnel-meta").textContent = firstDefined(context.run.finished_at, context.run.updated_at)
      ? `完成于 ${formatDateTime(firstDefined(context.run.finished_at, context.run.updated_at))}`
      : "最新交易日";
  }

  function renderMessage(message, pushCount, candidates) {
    const element = $("#overview-message");
    const text = typeof message === "string"
      ? message
      : firstDefined(message.content, message.text, message.body, message.summary, "");
    const title = typeof message === "object" ? firstDefined(message.title, message.subject, "寻龙工作台 · 今日选股") : "寻龙工作台 · 今日选股";
    const fallback = pushCount
      ? `从候选中选出 ${pushCount} 只股票\n${candidates.filter((item) => normalizeCandidate(item).pushed).map((item) => {
          const row = normalizeCandidate(item);
          return `· ${row.name} ${row.score} 分`;
        }).join("\n")}`
      : "今日无推送，宁错过不做错。";
    element.innerHTML = `<div class="message-preview"><div class="message-bubble"><div class="message-title"><i data-lucide="message-square-text"></i><span>${escapeHtml(strategyText(title))}</span></div><div class="message-body">${escapeHtml(strategyText(text || fallback))}</div><div class="message-disclaimer">仅供研究，不构成投资建议</div></div></div>`;
    const status = $("#message-channel-status");
    const sent = Boolean(typeof message === "object" && firstDefined(message.sent, message.delivered, message.status === "sent", false));
    status.className = `status-badge ${sent ? "bullish" : "neutral"}`;
    status.textContent = sent ? "已投递" : "预览";
  }

  function renderBreadth(market) {
    const breadth = firstDefined(market.breadth, market);
    const rise = toNumber(firstDefined(breadth.rise_count, breadth.up_count, breadth.advancers, breadth.rising), 0);
    const fall = toNumber(firstDefined(breadth.fall_count, breadth.down_count, breadth.decliners, breadth.falling), 0);
    const flat = toNumber(firstDefined(breadth.flat_count, breadth.unchanged, breadth.flat), 0);
    const observedTotal = rise + fall + flat;
    if (!observedTotal) {
      setState("#market-breadth", "empty", "市场宽度暂不可用", "当前数据源未返回上涨、下跌和平盘家数", null, true);
      $("#breadth-meta").textContent = "等待有效市场宽度数据";
      return;
    }
    const total = observedTotal;
    const score = firstDefined(breadth.temperature, breadth.score, market.temperature, market.breadth_score, Math.round((rise / total) * 100));
    const sectors = asArray(firstDefined(breadth.sectors, breadth.top_sectors, market.sectors, market.top_sectors, market.industries, []));
    $("#market-breadth").innerHTML = `<div class="breadth-layout">
      <div class="breadth-score"><strong>${formatNumber(score)}</strong><span>市场温度</span></div>
      <div><div class="breadth-bar" aria-label="涨跌家数分布"><span class="rise" style="width:${(rise / total) * 100}%"></span><span class="flat" style="width:${(flat / total) * 100}%"></span><span class="fall" style="width:${(fall / total) * 100}%"></span></div><div class="breadth-legend"><span class="up">上涨 ${formatNumber(rise)}</span><span>平盘 ${formatNumber(flat)}</span><span class="down">下跌 ${formatNumber(fall)}</span></div></div>
    </div>
    ${sectors.length ? `<div class="sector-list">${sectors.slice(0, 6).map((sector) => {
      const change = firstDefined(sector.change_pct, sector.change, sector.pct_change, 0);
      return `<div class="sector-item"><span>${escapeHtml(firstDefined(sector.name, sector.industry, "板块"))}</span><strong class="${changeClass(change)}">${formatPercent(change)}</strong></div>`;
    }).join("")}</div>` : ""}`;
    $("#breadth-meta").textContent = firstDefined(market.regime, market.summary, `${rise} 涨 / ${fall} 跌`);
  }

  function renderRuntime(jobs, sources) {
    const combined = [
      ...jobs.slice(0, 3).map((job) => ({
        name: firstDefined(job.name, job.title, job.id, "定时任务"),
        detail: firstDefined(job.last_run_at, job.last_run, job.next_run_at, "等待运行"),
        status: firstDefined(job.status, job.last_status, job.enabled === false ? "disabled" : "ok")
      })),
      ...sources.slice(0, 3).map((source) => ({
        name: firstDefined(source.name, source.source, source.id, "数据源"),
        detail: firstDefined(source.latency_ms !== undefined ? `${source.latency_ms} ms` : null, source.updated_at, source.message, "已连接"),
        status: firstDefined(source.status, source.healthy === false ? "error" : "ok")
      }))
    ];
    const element = $("#overview-runtime");
    if (!combined.length) {
      element.innerHTML = stateMarkup("empty", "暂无运行状态", "任务与数据源未返回状态", null, true);
      return;
    }
    element.innerHTML = combined.map((item) => {
      const status = String(item.status).toLowerCase();
      const failed = status.includes("fail") || status.includes("error");
      const degraded = status.includes("degraded") || status.includes("warning");
      const running = status.includes("run") || status.includes("pending");
      const disabled = status.includes("disabled") || status.includes("off");
      const dot = failed ? "bad" : degraded || running ? "warn" : disabled ? "" : "good";
      const label = failed ? "异常" : degraded ? "降级" : running ? "运行中" : disabled ? "已停用" : "正常";
      return `<div class="runtime-row"><div class="runtime-main"><span class="status-dot ${dot}"></span><span><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(formatDateTime(item.detail) === "--" ? item.detail : formatDateTime(item.detail))}</small></span></div><span class="status-badge ${failed ? "bearish" : degraded || running ? "warning" : "neutral"}">${label}</span></div>`;
    }).join("");
  }

  async function runOverviewScreener() {
    navigate("screener", true, false);
    await runScreener();
  }

  function overnightBoardCards(items, label) {
    const rows = asArray(items);
    if (!rows.length) return `<div class="empty-state"><strong>${escapeHtml(label)}</strong><span>暂无可用板块数据</span></div>`;
    return `<div class="rotation-list">${rows.map((row) => `<div class="rotation-row"><div><strong>${escapeHtml(row.name || "--")}</strong><small>${escapeHtml(row.state || row.selection_reason || "轮动观察")}</small></div><span class="change ${toNumber(row.return_10d, 0) >= 0 ? "up" : "down"}">${formatNumber(row.return_10d, 2)}%</span></div>`).join("")}</div>`;
  }

  function renderOvernightLegacy(data) {
    const run = data.run || data;
    const metadata = run.metadata || data.metadata || {};
    const plan = metadata.overnight_board_plan || {};
    const candidates = extractCandidates(run);
    renderMetrics("#overnight-metrics", [
      { label: "观察板块", value: formatNumber(asArray(plan.observation_boards).length), detail: "10日轮动初选", icon: "radar" },
      { label: "复核板块", value: formatNumber(asArray(plan.review_boards).length), detail: "逻辑与状态复核", icon: "filter" },
      { label: "重点板块", value: formatNumber(asArray(plan.focus_boards).length), detail: "仅在这些板块内选股", icon: "layers" },
      { label: "隔夜候选", value: formatNumber(candidates.length), detail: "次日 09:20-09:25 验证", icon: "moon-star" }
    ]);
    $("#overnight-source").textContent = plan.rotation_source || "通达信板块指数";
    const changes = asArray(metadata.second_round_changes);
    $("#overnight-round-status").textContent = metadata.pipeline_stage === "overnight_second_round" ? "第二轮已完成" : "等待用户补充";
    $("#overnight-round-changes").innerHTML = changes.length
      ? `<div class="diagnosis-list">${changes.map((item) => `<div class="diagnosis-item"><strong>${escapeHtml(item.change)} ${escapeHtml(item.name || item.code)}</strong><span>${escapeHtml(item.reason || "--")}</span></div>`).join("")}</div>`
      : "";
    $("#overnight-board-funnel").innerHTML = [
      `<section><h3>观察 ${asArray(plan.observation_boards).length}</h3>${overnightBoardCards(plan.observation_boards, "观察板块")}</section>`,
      `<section><h3>复核 ${asArray(plan.review_boards).length}</h3>${overnightBoardCards(plan.review_boards, "复核板块")}</section>`,
      `<section><h3>重点 ${asArray(plan.focus_boards).length}</h3>${overnightBoardCards(plan.focus_boards, "重点板块")}</section>`
    ].join("");
    const laggards = asArray(plan.laggard_boards);
    $("#overnight-laggards").innerHTML = laggards.length
      ? laggards.map((row) => `<section class="risk-board"><h3>${escapeHtml(row.name || "--")}</h3><strong class="change down">${formatNumber(row.return_10d, 2)}%</strong><small>10日累计跌幅 | 单日最低 ${formatNumber(row.min_daily_pct, 2)}%</small></section>`).join("")
      : stateMarkup("empty", "暂无可用跌幅数据", "通达信板块指数日线不足时不作风险排序。", null, true);
    const context = runContext(data, run, data.summary || {});
    if (!candidates.length) {
      $("#overnight-results").innerHTML = stateMarkup("empty", "暂无隔夜候选", "没有重点板块内标的通过当前条件。", null, true);
    } else {
      const groups = new Map();
      asArray(plan.technical_board_groups).forEach((item) => {
        groups.set(item.name, { return5d: item.return_5d, parent: item.parent, rows: [] });
      });
      candidates.forEach((item) => {
        const overnight = item.overnight || (item.snapshot || {}).overnight || {};
        const board = overnight.board || item.industry || "未分类";
        if (!groups.has(board)) groups.set(board, { return5d: overnight.board_5d_return, rows: [] });
        groups.get(board).rows.push(item);
      });
      const sections = [...groups.entries()].sort((a, b) => String(a[1].parent || "").localeCompare(String(b[1].parent || ""), "zh") || String(a[0]).localeCompare(String(b[0]), "zh")).map(([board, group]) => {
        group.rows.sort((a, b) => toNumber((a.overnight || a.snapshot?.overnight || {}).board_rank, 999) - toNumber((b.overnight || b.snapshot?.overnight || {}).board_rank, 999));
        return `<section class="overnight-board-group"><header><strong>${escapeHtml(board)}</strong><span class="change ${toNumber(group.return5d, 0) >= 0 ? "up" : "down"}">5日 ${formatNumber(group.return5d, 2)}%</span></header><ol>${group.rows.length ? group.rows.map((item, index) => {
          const overnight = item.overnight || (item.snapshot || {}).overnight || {};
          return `<li data-stock-code="${escapeHtml(item.code)}"><span>${formatNumber(overnight.board_rank || index + 1, 0)}</span><strong>${escapeHtml(item.name)}</strong></li>`;
        }).join("") : '<li class="empty-industry"><strong>暂无符合条件标的</strong></li>'}</ol></section>`;
      });
      $("#overnight-results").innerHTML = `<div class="overnight-board-groups">${sections.join("")}</div>`;
    }
    bindCandidateRows($("#overnight-results"));
    initIcons();
  }

  async function loadOvernightLegacy(force = false) {
    if (state.loaded.has("overnight") && !force) {
      renderOvernightLegacy(state.overnight || {});
      return;
    }
    setState("#overnight-results", "loading", "正在读取隔夜候选池", "", null);
    try {
      const data = await api("/api/screener/runs/latest?run_type=overnight");
      state.overnight = data || {};
      state.loaded.add("overnight");
      renderOvernightLegacy(state.overnight);
    } catch (error) {
      setState("#overnight-results", "empty", "暂未建立隔夜候选池", "盘后运行“建立隔夜池”后将在此显示。", null, true);
    }
  }

  async function runOvernight() {
    const button = $("#overnight-run");
    setButtonBusy(button, true, "建立中");
    try {
      const data = await api("/api/screener/run", { method: "POST", body: { mode: "overnight", limit: toNumber(state.settings?.scan_limit, 80) }, timeout: 180000 });
      state.overnight = data || {};
      state.loaded.add("overnight");
      renderOvernight(state.overnight);
      showToast("success", "隔夜候选池已建立", `已保留 ${extractCandidates(data).length} 只观察标的`);
    } catch (error) {
      showToast("error", "隔夜候选池建立失败", error.message);
    } finally {
      setButtonBusy(button, false);
    }
  }

  async function submitOvernightReview(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form).entries());
    if (!values.board || !values.title) {
      showToast("warning", "缺少复核信息", "请至少填写关联板块和新闻标题或判断。");
      return;
    }
    const button = form.querySelector("button[type=submit]");
    setButtonBusy(button, true, "复核中");
    try {
      const run = state.overnight || {};
      const data = await api("/api/candidates/review", {
        method: "POST",
        body: { run_id: run.id, updates: [values], notes: values.notes || "" },
        timeout: 180000
      });
      state.overnight = data || {};
      state.loaded.add("overnight");
      renderOvernight(state.overnight);
      form.reset();
      showToast("success", "第二轮复核完成", "仅复核原隔夜候选池，未扩张全市场。");
    } catch (error) {
      showToast("error", "第二轮复核失败", error.message);
    } finally {
      setButtonBusy(button, false);
    }
  }

  function renderResearchPool(data) {
    const items = asArray(data.items);
    const analyzed = items.filter((item) => item.analysis && Object.keys(item.analysis).length).length;
    const latest = items.map((item) => item.analyzed_at).filter(Boolean).sort().at(-1);
    renderMetrics("#research-metrics", [
      { label: "自选个股", value: formatNumber(items.length), detail: "由你手动添加", icon: "notebook-tabs" },
      { label: "已完成分析", value: formatNumber(analyzed), detail: "新闻面 + 技术面 + 财报面", icon: "scan-search" },
      { label: "待分析", value: formatNumber(items.length - analyzed), detail: "可单只或批量更新", icon: "clock-3" },
      { label: "最近更新", value: latest ? formatDateTime(latest) : "--", detail: "分析结果按需刷新", icon: "refresh-cw" }
    ]);
    const container = $("#research-results");
    if (!items.length) {
      container.innerHTML = stateMarkup("empty", "研究池还是空的", "输入一只沪深主板股票代码，系统会为它建立三面研究卡片。", null, true);
      initIcons();
      return;
    }
    const indicatorLabels = {
      ma5: "MA5", ma10: "MA10", ma20: "MA20", rsi14: "RSI14",
      macd_dif: "MACD DIF", macd_dea: "MACD DEA", macd_hist: "MACD柱", volume_ratio: "量比"
    };
    container.innerHTML = items.map((item) => {
      const analysis = item.analysis || {};
      const news = analysis.news || {};
      const technical = analysis.technical || {};
      const financial = analysis.financial || {};
      const newsItems = asArray(news.items).slice(0, 4);
      const technicalMetrics = Object.entries(technical.indicators || {});
      const financialMetrics = asArray(financial.metrics).slice(0, 8);
      const toneClass = (tone) => String(tone).includes("正") ? "positive" : String(tone).includes("负") ? "negative" : "neutral";
      const evidenceText = (row) => typeof row === "string" ? row : firstDefined(row.label, row.reason, row.name, row.text, "指标信号");
      return `<article class="research-card" data-research-code="${escapeHtml(item.code)}">
        <header class="research-card-header">
          <div><span class="research-code">${escapeHtml(item.code)}</span><h2>${escapeHtml(item.name || item.code)}</h2>${item.note ? `<p>${escapeHtml(item.note)}</p>` : ""}</div>
          <div class="research-card-side"><small>${item.analyzed_at ? `更新于 ${escapeHtml(formatDateTime(item.analyzed_at))}` : "尚未分析"}</small><div class="research-actions"><button class="button secondary compact" type="button" data-research-action="analyze" data-code="${escapeHtml(item.code)}"><i data-lucide="scan-search"></i><span>${item.analyzed_at ? "重新分析" : "开始分析"}</span></button><button class="icon-button danger" type="button" title="移出研究池" aria-label="移出研究池" data-research-action="remove" data-code="${escapeHtml(item.code)}"><i data-lucide="trash-2"></i></button></div></div>
        </header>
        ${item.analyzed_at ? `<div class="research-faces">
          <section class="research-face"><div class="research-face-title"><i data-lucide="newspaper"></i><h3>新闻面</h3><span>${formatNumber(news.positive || 0)} 正 / ${formatNumber(news.negative || 0)} 负</span></div><p class="face-summary">${escapeHtml(news.conclusion || "暂无新闻结论")}</p>${newsItems.length ? `<div class="news-clue-list">${newsItems.map((row) => `<div class="news-clue"><span class="tone-badge ${toneClass(row.tone)}">${escapeHtml(row.tone || "中性")}</span><div><strong>${escapeHtml(row.title || "未命名新闻")}</strong><small>${escapeHtml(formatDateTime(row.time))}</small></div></div>`).join("")}</div>` : '<p class="face-empty">近期暂无有效新闻线索</p>'}</section>
          <section class="research-face"><div class="research-face-title"><i data-lucide="chart-no-axes-combined"></i><h3>技术面</h3><span>${escapeHtml(technical.label || "中性")} · ${formatNumber(technical.score || 0)}分</span></div><p class="face-summary">${escapeHtml(technical.summary || "暂无技术面摘要")}</p>${technicalMetrics.length ? `<div class="metric-chip-list">${technicalMetrics.map(([key, value]) => `<span class="metric-chip"><small>${escapeHtml(indicatorLabels[key] || key)}</small><strong>${escapeHtml(formatNumber(value, 2))}</strong></span>`).join("")}</div>` : '<p class="face-empty">技术指标暂不可用</p>'}${asArray(technical.evidence).length ? `<ul class="face-evidence">${asArray(technical.evidence).slice(0, 4).map((row) => `<li>${escapeHtml(evidenceText(row))}</li>`).join("")}</ul>` : ""}</section>
          <section class="research-face"><div class="research-face-title"><i data-lucide="landmark"></i><h3>财报面</h3><span>${escapeHtml(firstDefined(financial.report_date, financial.period, "最新可得"))}</span></div><p class="face-summary">${escapeHtml(financial.conclusion || "暂无财报面结论")}</p>${financialMetrics.length ? `<div class="metric-chip-list">${financialMetrics.map((metric) => `<span class="metric-chip"><small>${escapeHtml(metric.label || metric.key || "指标")}</small><strong>${escapeHtml(formatNumber(metric.value, 2))}${escapeHtml(metric.unit || "")}</strong></span>`).join("")}</div>` : '<p class="face-empty">财务指标暂不可用</p>'}${financial.warning ? `<p class="face-warning"><i data-lucide="triangle-alert"></i>${escapeHtml(financial.warning)}</p>` : ""}</section>
        </div>` : '<div class="research-pending"><i data-lucide="scan-search"></i><span>已加入研究池，点击“开始分析”生成新闻面、技术面和财报面结果。</span></div>'}
      </article>`;
    }).join("");
    $$('[data-research-action="analyze"]', container).forEach((button) => button.addEventListener("click", () => analyzeResearchStock(button.dataset.code, button)));
    $$('[data-research-action="remove"]', container).forEach((button) => button.addEventListener("click", () => removeResearchStock(button.dataset.code, button)));
    initIcons();
  }

  async function loadOvernight(force = false) {
    if (state.loaded.has("overnight") && !force) {
      renderResearchPool(state.overnight || {});
      return;
    }
    setState("#research-results", "loading", "正在读取自选研究池", "", null);
    try {
      const data = await api("/api/research-pool");
      state.overnight = data || {};
      state.loaded.add("overnight");
      renderResearchPool(state.overnight);
    } catch (error) {
      setState("#research-results", "error", "无法读取自选研究池", error.message, "overnight");
    }
  }

  async function addResearchStock(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form).entries());
    const code = String(values.code || "").trim().match(/\d{6}/)?.[0];
    if (!code) {
      showToast("warning", "股票代码不正确", "请输入6位沪深主板股票代码。");
      return;
    }
    const button = form.querySelector('button[type="submit"]');
    setButtonBusy(button, true, "添加并分析中");
    try {
      await api("/api/research-pool", { method: "POST", body: { code, note: String(values.note || "").trim() }, timeout: 60000 });
      try {
        await api(`/api/research-pool/${encodeURIComponent(code)}/analyze`, { method: "POST", timeout: 180000 });
        showToast("success", "三面分析已完成", `${code} 已加入自选研究池。`);
      } catch (analysisError) {
        showToast("warning", "个股已添加，分析暂未完成", analysisError.message);
      }
      form.reset();
      await loadOvernight(true);
    } catch (error) {
      showToast("error", "添加失败", error.message);
    } finally {
      setButtonBusy(button, false);
    }
  }

  function extractResearchCodes(text) {
    return [...new Set((String(text || "").match(/(?<!\d)\d{6}(?!\d)/g) || []))];
  }

  function previewResearchBatch() {
    const codes = extractResearchCodes($("#research-batch-text").value);
    const mainBoard = codes.filter((code) => /^(?:000|001|002|003|600|601|603|605)\d{3}$/.test(code));
    const element = $("#research-batch-preview");
    element.textContent = codes.length
      ? `识别到 ${codes.length} 个不重复代码，其中约 ${mainBoard.length} 个沪深主板代码；导入时会再次核验行情。`
      : "等待粘贴股票代码，单次最多200只";
    element.classList.toggle("ready", Boolean(codes.length));
  }

  async function importResearchBatch(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form).entries());
    const text = String(values.text || "").trim();
    if (!extractResearchCodes(text).length) {
      showToast("warning", "没有识别到股票代码", "请粘贴包含6位股票代码的列表。");
      return;
    }
    const button = $("#research-batch-import");
    setButtonBusy(button, true, "正在导入");
    try {
      const result = await api("/api/research-pool/batch", {
        method: "POST",
        body: { text, note: String(values.note || "").trim() },
        timeout: 90000
      });
      const accepted = asArray(result.accepted_codes);
      let analyzed = 0;
      let analysisFailed = 0;
      if (values.analyze === "on" && accepted.length) {
        for (let index = 0; index < accepted.length; index += 1) {
          const label = button.querySelector("span:last-child");
          if (label) label.textContent = `分析 ${index + 1}/${accepted.length}`;
          try {
            await api(`/api/research-pool/${encodeURIComponent(accepted[index])}/analyze`, { method: "POST", timeout: 180000 });
            analyzed += 1;
          } catch (error) {
            analysisFailed += 1;
          }
        }
      }
      const rejected = asArray(result.rejected);
      const rejectedText = rejected.length
        ? `；过滤 ${rejected.length} 只（${rejected.slice(0, 6).map((item) => item.code).join("、")}${rejected.length > 6 ? "等" : ""}）`
        : "";
      const analysisText = values.analyze === "on" ? `；分析成功 ${analyzed} 只${analysisFailed ? `、失败 ${analysisFailed} 只` : ""}` : "";
      showToast(
        rejected.length || analysisFailed ? "warning" : "success",
        "批量导入完成",
        `新增 ${formatNumber(result.added_count)} 只，已存在并更新 ${formatNumber(result.updated_count)} 只${rejectedText}${analysisText}。`
      );
      form.reset();
      previewResearchBatch();
      await loadOvernight(true);
    } catch (error) {
      showToast("error", "批量导入失败", error.message);
    } finally {
      setButtonBusy(button, false);
    }
  }

  async function analyzeResearchStock(code, button) {
    setButtonBusy(button, true, "分析中");
    try {
      await api(`/api/research-pool/${encodeURIComponent(code)}/analyze`, { method: "POST", timeout: 180000 });
      showToast("success", "分析已更新", `${code} 的三面研究结果已刷新。`);
      await loadOvernight(true);
    } catch (error) {
      showToast("error", "分析失败", error.message);
    } finally {
      setButtonBusy(button, false);
    }
  }

  async function removeResearchStock(code, button) {
    setButtonBusy(button, true, "移除中");
    try {
      await api(`/api/research-pool/${encodeURIComponent(code)}`, { method: "DELETE" });
      showToast("success", "已移出研究池", `${code} 不再显示在自选研究池中。`);
      await loadOvernight(true);
    } catch (error) {
      showToast("error", "移除失败", error.message);
      setButtonBusy(button, false);
    }
  }

  async function analyzeAllResearch() {
    const items = asArray(state.overnight?.items);
    if (!items.length) {
      showToast("warning", "研究池为空", "请先添加至少一只沪深主板股票。");
      return;
    }
    const button = $("#research-analyze-all");
    setButtonBusy(button, true, `分析 0/${items.length}`);
    let completed = 0;
    let failed = 0;
    for (const item of items) {
      try {
        await api(`/api/research-pool/${encodeURIComponent(item.code)}/analyze`, { method: "POST", timeout: 180000 });
        completed += 1;
      } catch (error) {
        failed += 1;
      }
      const label = button.querySelector("span:last-child");
      if (label) label.textContent = `分析 ${completed + failed}/${items.length}`;
    }
    setButtonBusy(button, false);
    await loadOvernight(true);
    showToast(failed ? "warning" : "success", "批量分析完成", `成功 ${completed} 只，失败 ${failed} 只。`);
  }

  async function loadScreener(force = false) {
    if (state.loaded.has("screener") && !force) {
      renderScreener(state.screener.data || {});
      return;
    }
    setState("#screener-results", "loading", "正在读取最新选股结果", "", null);
    renderMetrics("#screener-metrics", []);
    try {
      const data = await api(`/api/screener/runs/latest?run_type=${encodeURIComponent(state.screener.mode)}`);
      state.screener.data = data || {};
      state.screener.live = null;
      state.loaded.add("screener");
      renderScreener(state.screener.data);
      setUpdatedAt(firstDefined(data.finished_at, data.updated_at, new Date()));
      await refreshScreenerAuction(true);
    } catch (error) {
      setState("#screener-results", "error", "无法读取选股结果", error.message, "screener");
      showToast("error", "选股结果加载失败", error.message);
    }
  }

  function updateAuctionSyncStatus(payload = null, error = null) {
    const element = $("#auction-sync-status");
    if (!element) return;
    element.classList.remove("syncing", "final", "error");
    const dot = element.querySelector(".status-dot");
    const label = element.querySelector("span:last-child");
    if (error) {
      element.classList.add("error");
      label.textContent = "竞价同步异常";
      element.title = error.message || String(error);
      return;
    }
    if (!payload) {
      label.textContent = "竞价同步准备中";
      element.title = "候选池竞价行情每 3 秒同步一次";
      return;
    }
    if (payload.session === "auction" || payload.session === "final") element.classList.add("syncing");
    else if (payload.frozen) element.classList.add("final");
    if (dot) dot.classList.toggle("good", payload.session === "auction");
    const time = formatQuoteTime(payload.updated_at);
    const availability = payload.candidate_count
      ? ` · ${formatNumber(payload.available_count || 0)}/${formatNumber(payload.candidate_count)} 条有效`
      : "";
    label.textContent = `${payload.session_label || "竞价行情"}${availability}${time !== "--" ? ` · ${time}` : ""}`;
    const sources = Array.isArray(payload.sources) && payload.sources.length ? payload.sources.join("、") : (payload.source || "--");
    element.title = `${payload.note || "近实时竞价参考"} 数据源：${sources}`;
  }

  function localAuctionSyncWindow() {
    const now = new Date();
    const weekday = now.getDay();
    const minute = now.getHours() * 60 + now.getMinutes();
    // 09:25 后多留一分钟，拿到交易所撮合后的最终开盘价再停止。
    return weekday >= 1 && weekday <= 5 && minute >= 9 * 60 + 15 && minute < 9 * 60 + 26;
  }

  function mergeLiveAuction(payload) {
    const data = state.screener.data;
    if (!data || !Array.isArray(data.candidates)) return;
    const liveByCode = new Map(asArray(payload.candidates).map((item) => [String(item.code), item]));
    data.candidates = data.candidates.map((candidate) => {
      const code = String(firstDefined(candidate.code, candidate.stock_code, candidate.symbol, "")).replace(/^(sh|sz)/i, "");
      const live = liveByCode.get(code);
      return live ? { ...candidate, ...live, live_auction: live } : candidate;
    });
  }

  async function refreshScreenerAuction(force = false) {
    if (document.hidden || state.view !== "screener" || state.screener.liveBusy) return;
    if (!force && !localAuctionSyncWindow()) return;
    const data = state.screener.data || {};
    const runId = firstDefined(data.id, data.run_id, data.run && data.run.id, data.latest_run && data.latest_run.id);
    if (!runId || !extractCandidates(data).length) {
      updateAuctionSyncStatus(null);
      return;
    }
    state.screener.liveBusy = true;
    try {
      const payload = await api(`/api/screener/runs/${encodeURIComponent(runId)}/live-auction`, { timeout: 10000 });
      state.screener.live = payload;
      mergeLiveAuction(payload);
      updateAuctionSyncStatus(payload);
      renderScreenerRows(extractCandidates(data).map((item, index) => normalizeCandidate(item, index, state.screener.context || {})));
    } catch (error) {
      updateAuctionSyncStatus(state.screener.live, error);
    } finally {
      state.screener.liveBusy = false;
    }
  }

  function renderScreener(data) {
    const summary = firstDefined(data.summary, data.stats, {});
    const context = runContext(data, data.run || data.latest_run || {}, summary);
    state.screener.context = context;
    const candidates = extractCandidates(data).map((item, index) => normalizeCandidate(item, index, context));
    const pushed = candidates.filter((item) => item.pushed).length;
    const actualMode = String(data.run_type || state.screener.mode || "technical");
    state.screener.mode = actualMode;
    $$("#screener-mode button").forEach((button) => button.classList.toggle("active", button.dataset.mode === actualMode));
    renderMetrics("#screener-metrics", [
      { label: "沪深主板", value: formatNumber(firstDefined(summary.total, summary.scanned, data.total, data.universe_count, 0)), detail: `排除非主板 ${formatNumber(firstDefined(data.metadata && data.metadata.excluded_non_main_board_count, data.metadata && data.metadata.legacy_non_main_removed_count, 0))} 只`, icon: "database" },
      { label: "风险过滤", value: formatNumber(firstDefined(summary.pre_filtered, summary.filtered, data.first_board_count, data.metadata && data.metadata.scan_limit, 0)), detail: "排除 ST、停牌与风险标的", icon: "filter" },
      { label: "规则库综合有效", value: formatNumber(firstDefined(summary.scored, candidates.length, 0)), detail: "完成规则库因子评分", icon: "gauge" },
      { label: "强势候选", value: formatNumber(firstDefined(summary.strong, candidates.filter((item) => item.score >= 75).length)), detail: `规则库门槛 ${formatNumber(firstDefined(summary.min_score, data.min_score, 0))} 分`, icon: "flame", className: "up" },
      { label: "已推送", value: formatNumber(firstDefined(summary.pushed, pushed)), detail: pushed ? "消息已投递" : "当前无推送", icon: "send" }
    ]);
    renderScreenerRows(candidates);
    $("#screener-subtitle").textContent = strategyText(firstDefined(data.description, data.mode_label, `最近运行：${formatDateTime(firstDefined(data.finished_at, data.updated_at))}`));
  }

  function renderScreenerRows(candidates) {
    const query = state.screener.filter.trim().toLowerCase();
    const signalFilter = state.screener.signal;
    const filtered = candidates.filter((item) => {
      const signal = signalMeta(item.signal, item.score).key;
      const haystack = `${item.code} ${item.name} ${item.industry} ${strategyModeLabel(item.strategy_mode)} ${item.risk_status || ""}`.toLowerCase();
      return (!query || haystack.includes(query)) && (!signalFilter || signal === signalFilter);
    });
    const element = $("#screener-results");
    element.innerHTML = candidateTable(filtered, { context: state.screener.context || {}, liveAuction: true });
    bindCandidateRows(element);
    initIcons();
  }

  async function runScreener() {
    const button = $("#screener-run");
    setButtonBusy(button, true, "扫描中");
    try {
      const data = await api("/api/screener/run", {
        method: "POST",
        body: { mode: state.screener.mode, limit: toNumber(state.settings?.scan_limit, 80) },
        // A full-market scan reads a K-line confirmation batch from local TDX.
        // It is expected to take longer than lightweight dashboard requests.
        timeout: 180000
      });
      state.screener.data = data || {};
      state.screener.live = null;
      state.loaded.add("screener");
      renderScreener(state.screener.data);
      await refreshScreenerAuction(true);
      showToast("success", "扫描完成", `已生成 ${extractCandidates(data).length} 条候选记录`);
    } catch (error) {
      showToast("error", "扫描失败", error.message);
    } finally {
      setButtonBusy(button, false);
    }
  }

  function oversoldQuery() {
    const params = new URLSearchParams({
      limit: "80",
      drawdown_min: String(toNumber($("#oversold-drawdown").value, 12)),
      volume_multiple: String(toNumber($("#oversold-volume").value, 2)),
      min_amount: String(toNumber($("#oversold-amount").value, 10000) * 10000),
      amount_multiple: String(toNumber($("#oversold-amount-ratio").value, 1.5)),
      profile: $("#oversold-profile").value,
      wave: $("#oversold-wave").value,
      triggered_only: $("#oversold-triggered").checked ? "true" : "false",
      market_filter: $("#oversold-market-filter").checked ? "true" : "false",
      min_reward_risk: String(toNumber($("#oversold-rr").value, 1.5)),
      require_ths_hot: $("#oversold-ths-hot").checked ? "true" : "false",
      require_positive_dde: $("#oversold-positive-dde").checked ? "true" : "false"
    });
    return params.toString();
  }

  async function loadOversold(force = false) {
    if (state.oversold && state.loaded.has("oversold") && !force) {
      renderOversold(state.oversold);
      return;
    }
    setState("#oversold-results", "loading", "正在扫描沪深主板", "识别市场风格、P0/R0、倍量突破与3–9日二波结构", null);
    renderMetrics("#oversold-metrics", [
      { label: "扫描中", value: "--", detail: "读取本地通达信日线", icon: "loader-circle" },
      { label: "扫描中", value: "--", detail: "执行主板与风险过滤", icon: "loader-circle" },
      { label: "扫描中", value: "--", detail: "定位已知低点 P0 与压力 R0", icon: "loader-circle" },
      { label: "扫描中", value: "--", detail: "确认倍量一波与缩量二波", icon: "loader-circle" }
    ]);
    try {
      const data = await api(`/api/screener/oversold-rebound?${oversoldQuery()}`, { timeout: 90000 });
      state.oversold = data || {};
      state.loaded.add("oversold");
      renderOversold(state.oversold);
      setUpdatedAt(firstDefined(data.generated_at, new Date()));
    } catch (error) {
      setState("#oversold-results", "error", "超跌反弹筛选失败", error.message, "oversold");
      renderMetrics("#oversold-metrics", []);
      showToast("error", "超跌反弹筛选失败", error.message);
    }
  }

  function renderOversold(data) {
    const funnel = data.funnel || {};
    const thresholds = data.thresholds || {};
    const market = data.market_style || {};
    const rows = asArray(data.rows);
    renderMetrics("#oversold-metrics", [
      { label: "A股输入", value: formatNumber(funnel.input), detail: "本地全市场日线", icon: "database" },
      { label: "沪深主板", value: formatNumber(funnel.main_board), detail: "已排除非主板与风险名称", icon: "filter" },
      { label: "同花顺主板人气", value: formatNumber(funnel.ths_hot_mainboard), detail: `人气池共${formatNumber(funnel.ths_hot_total)}只`, icon: "flame" },
      { label: "资金启动", value: formatNumber(funnel.capital_ready), detail: "成交额放大、阳线上涨且大单净流入", icon: "badge-dollar-sign" },
      { label: "正式触发", value: formatNumber(funnel.matched), detail: "再通过P0/R0与均线确认", icon: "rotate-ccw", className: funnel.matched ? "up" : "" }
    ]);
    $("#oversold-as-of").textContent = `${data.trade_date || "日期未知"} | ${data.source === "local_tdx_postclose" ? "本地通达信盘后" : data.source || "数据源未知"}`;
    $("#oversold-result-meta").textContent = `${formatNumber(rows.length)} 只 | 同花顺人气 | 成交额≥${formatNumber(thresholds.min_amount / 100000000, 1)}亿 | 放大≥${formatNumber(thresholds.amount_multiple, 1)}倍`;
    const styleCard = $("#oversold-market-style");
    styleCard.className = `oversold-style-card ${market.supportive ? "supportive" : "unsupported"}`;
    styleCard.innerHTML = `<div class="style-primary"><i data-lucide="${market.supportive ? "circle-check-big" : "triangle-alert"}"></i><span><strong>${escapeHtml(market.label || "市场风格未评估")}</strong><small>${escapeHtml(market.definition || "先判断资金是否偏好低位超跌修复")}</small></span></div><div><b>${formatNumber(market.score, 1)} / 20</b><small>市场风格得分</small></div><div><b>${formatNumber(market.oversold_share, 1)}%</b><small>强势样本超跌占比</small></div><div><b>${formatNumber(market.repair_breadth)}</b><small>超跌修复广度</small></div>`;
    $("#oversold-method").innerHTML = asArray(data.methodology).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
    const vetoes = Object.entries(data.veto_stats || {}).sort((a, b) => b[1] - a[1]);
    $("#oversold-vetoes").innerHTML = vetoes.length
      ? vetoes.map(([reason, count]) => `<li>${escapeHtml(reason)} — <strong>${formatNumber(count)}</strong> 只被否决</li>`).join("")
      : "<li>本次扫描无否决记录</li>";
    $("#oversold-risk-note").textContent = `${data.threshold_status || "经验阈值，待回测。"} ${data.disclaimer || ""}`;
    renderOversoldRows(rows);
    initIcons();
  }

  function renderOversoldRows(rows) {
    const element = $("#oversold-results");
    if (!rows.length) {
      element.innerHTML = stateMarkup("empty", "当前没有完成资金启动确认的同花顺人气票", "系统不会为了凑数量放宽门槛；可等待同花顺人气、成交额放大、大单净流入与P0/R0同时满足。", null, true);
      return;
    }
    element.innerHTML = `<table>
      <thead><tr><th>排名</th><th>股票</th><th>同花顺人气原因</th><th class="numeric">评分</th><th>波段/阶段</th><th class="numeric">当日</th><th class="numeric">量/额放大</th><th class="numeric">大单净量</th><th class="numeric mobile-hide">P0 / R0</th><th class="numeric mobile-hide">MA20</th><th class="numeric mobile-hide">盈亏比</th><th class="mobile-hide">资金证据</th><th class="mobile-hide">止损/压力</th><th aria-label="操作"></th></tr></thead>
      <tbody>${rows.map((item) => {
        const stageClass = item.triggered ? "bullish" : "warning";
        const evidence = ["同花顺人气", `成交额${formatNumber(item.amount_multiple, 2)}倍`, "大单净流入", item.above_ma5 ? "站上MA5" : "MA5下方", item.above_ma20 ? "站上MA20" : "MA20下方", item.industry_hot_count >= 2 ? `同行业${item.industry_hot_count}只` : "板块联动弱"].map((label) => `<span>${escapeHtml(label)}</span>`).join("");
        return `<tr data-stock-code="${escapeHtml(item.code)}">
          <td>${formatNumber(item.rank)}</td>
          <td class="stock-cell"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.code)}</small></td>
          <td><strong>${escapeHtml(item.ths_hot_reason || "同花顺强势股")}</strong><small>人气池序号 ${formatNumber(item.ths_hot_rank)}</small></td>
          <td class="numeric"><span class="score-pill ${scoreClass(item.score)}">${formatNumber(item.score, 0)}</span></td>
          <td><span class="status-badge ${stageClass}">${escapeHtml(item.stage)}</span></td>
          <td class="numeric ${changeClass(item.change_pct)}">${formatPercent(item.change_pct)}</td>
          <td class="numeric"><span class="price-levels"><span>量 <strong>${formatNumber(item.volume_multiple, 2)}x</strong></span><span>额 <strong>${formatNumber(item.amount_multiple, 2)}x</strong></span></span></td>
          <td class="numeric ${toNumber(item.ths_large_order_net_ratio) > 0 ? "up" : "down"}">${formatNumber(item.ths_large_order_net_ratio, 2)}</td>
          <td class="numeric mobile-hide"><span class="price-levels"><span>P0 <strong>${formatNumber(item.p0, 2)}</strong></span><span>R0 <strong>${formatNumber(item.r0, 2)}</strong></span></span></td>
          <td class="numeric mobile-hide ${item.above_ma20 ? "up" : "down"}">${formatNumber(item.ma20, 2)}</td>
          <td class="numeric mobile-hide">${formatNumber(item.reward_risk, 2)}R</td>
          <td class="mobile-hide"><span class="evidence-list">${evidence}</span></td>
          <td class="mobile-hide"><span class="price-levels"><span>止损 <strong>${formatNumber(item.invalidation, 2)}</strong></span><span>压力 <strong>${formatNumber(item.resistance, 2)}</strong></span>${item.adjustment_days ? `<span>调整 <strong>${formatNumber(item.adjustment_days)}日</strong></span>` : ""}</span></td>
          <td><button class="icon-button" type="button" aria-label="打开 ${escapeHtml(item.name)}" data-tooltip="打开个股分析"><i data-lucide="chevron-right"></i></button></td>
        </tr>`;
      }).join("")}</tbody>
    </table>`;
    bindCandidateRows(element);
  }

  async function runYijinerScan() {
    const button = $("#yijiner-run");
    setButtonBusy(button, true);
    try {
      const data = await api("/api/yijiner/scan", { method: "POST", timeout: 120000, body: {} });
      state.yijiner = data;
      state.loaded.add("yijiner");
      renderYijiner(data);
      setUpdatedAt(firstDefined(data.generated_at, new Date()));
      showToast("success", "一进二扫描完成", `候选 ${asArray(data.rows).length} 只`);
    } catch (error) {
      showToast("error", "一进二扫描失败", error.message);
      await loadYijiner(true);
    } finally {
      setButtonBusy(button, false);
    }
  }

  async function loadYijiner(force = false) {
    if (state.yijiner && state.loaded.has("yijiner") && !force) {
      renderYijiner(state.yijiner);
      return;
    }
    setState("#yijiner-results", "loading", "正在读取最近一次一进二扫描", "昨日首板 ∩ 今日竞价爆量 ∩ 评分卡", null);
    renderMetrics("#yijiner-metrics", []);
    try {
      const data = await api("/api/yijiner/runs/latest", { timeout: API_TIMEOUT });
      state.yijiner = data.run || null;
      state.loaded.add("yijiner");
      renderYijiner(state.yijiner);
    } catch (error) {
      setState("#yijiner-results", "error", "一进二数据加载失败", error.message, "yijiner");
      showToast("error", "一进二数据加载失败", error.message);
    }
  }

  const YIJINER_TIER_CLASS = { S: "bullish", A: "bullish", B: "warning", C: "neutral", D: "neutral" };

  function renderYijiner(run) {
    const element = $("#yijiner-results");
    if (!run) {
      element.innerHTML = stateMarkup("empty", "还没有一进二扫描记录", "点击右上角「运行扫描」立即执行一次；交易日 09:27 也会自动运行。", null, true);
      renderMetrics("#yijiner-metrics", []);
      $("#yijiner-result-meta").textContent = "--";
      return;
    }
    const rows = asArray(run.rows);
    const funnel = run.funnel || run.metadata && run.metadata.funnel || {};
    const tiers = run.tier_counts || (run.metadata || {}).tier_counts || {};
    const market = run.market_state || (run.metadata || {}).market_state || {};
    const universeCount = run.universe_count ?? funnel.main_board ?? rows.length;
    const prevFirstBoards = run.first_board_count ?? funnel.prev_first_boards ?? 0;
    const prevLimitUp = run.prev_limit_up_count ?? market.prev_limit_up_count ?? 0;
    const decisionCount = run.candidate_decision_count ?? rows.filter((item) => item.decision === "candidate").length;
    const auctionCount = run.auction_available_count ?? market.auction_quotes_count ?? rows.filter((item) => item.auction_available).length;
    renderMetrics("#yijiner-metrics", [
      { label: "主板股票池", value: formatNumber(universeCount), detail: "沪深主板（排除 ST/退市）", icon: "database" },
      { label: "昨日涨停 / 首板", value: `${formatNumber(prevLimitUp)} / ${formatNumber(prevFirstBoards)}`, detail: "收盘涨幅≥9.8% 且为第一块板", icon: "flame" },
      { label: "有竞价数据", value: formatNumber(auctionCount), detail: "FFD 09:25 终态 → 腾讯 → 东财降级", icon: "clock" },
      { label: "S/A 级爆量", value: formatNumber((tiers.S || 0) + (tiers.A || 0)), detail: `S ${formatNumber(tiers.S || 0)} · A ${formatNumber(tiers.A || 0)} · B ${formatNumber(tiers.B || 0)} · C ${formatNumber(tiers.C || 0)} · D ${formatNumber(tiers.D || 0)}`, icon: "zap" },
      { label: "候选标的", value: formatNumber(decisionCount), detail: "爆量档位+竞价健康度达标", icon: "target", className: decisionCount ? "up" : "" }
    ]);
    const stateCard = $("#yijiner-market-state");
    const breadth = Number(market.auction_breadth || 0);
    const healthy = breadth >= 0;
    stateCard.className = `oversold-style-card ${healthy ? "supportive" : "unsupported"}`;
    stateCard.innerHTML = `<div class="style-primary"><i data-lucide="${healthy ? "circle-check-big" : "triangle-alert"}"></i><span><strong>竞价市场宽度 ${breadth >= 0 ? "+" : ""}${breadth}（高开-低开家数差）</strong><small>${run.trade_date || "日期未知"} · ${formatNumber(market.auction_quotes_count || 0)} 只首板有竞价数据 · ${run.source || "数据源未知"}</small></span></div><div><b>${formatNumber(run.first_board_count ?? 0)}</b><small>昨日首板</small></div><div><b>${formatNumber(auctionCount)}</b><small>有竞价数据</small></div><div><b>${formatNumber(decisionCount)}</b><small>候选标的</small></div>`;
    $("#yijiner-method").innerHTML = asArray(run.methodology).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
    $("#yijiner-risk-note").textContent = `${run.threshold_status || "经验阈值，待回测。"} ${run.disclaimer || ""}`;
    $("#yijiner-result-meta").textContent = `${run.trade_date || "日期未知"} | ${formatNumber(rows.length)} 只 | 总分 = 首板强势度 55% + 竞价延续 45%`;
    if (!rows.length) {
      element.innerHTML = stateMarkup("empty", "当前没有通过筛选的一进二候选", "昨日无首板、竞价数据不可用或爆量未达 C 档以上时，列表为空。", null, true);
      initIcons();
      return;
    }
    element.innerHTML = `<table>
      <thead><tr><th>排名</th><th>股票</th><th class="numeric">连板</th><th>行业/题材</th><th>爆量档</th><th class="numeric">评分</th><th class="numeric">竞价高开</th><th class="numeric">竞价额/市值</th><th class="numeric">竞价量比</th><th class="numeric mobile-hide">首板时间</th><th class="numeric mobile-hide">炸板</th><th class="numeric mobile-hide">流通市值</th><th class="numeric mobile-hide">首板分</th><th class="numeric mobile-hide">竞价分</th><th class="mobile-hide">风险旗标</th><th class="mobile-hide">决策</th></tr></thead>
      <tbody>${rows.map((item) => {
        const tier = String(item.tier || "");
        const tierClass = YIJINER_TIER_CLASS[tier] || "neutral";
        const flags = asArray(item.risk_flags);
        const snap = item.snapshot || {};
        const openCount = firstDefined(item.open_count, snap.open_count);
        const firstLimitTime = firstDefined(item.first_limit_time, snap.first_limit_time);
        const reason = firstDefined(item.limit_reason, snap.limit_reason);
        const missing = (item.breakdown && item.breakdown.first_board && item.breakdown.first_board.capital.missing) || [];
        const flagText = [...flags, ...missing.map((m) => `缺数据：${m}`)].map((flag) => `<span>${escapeHtml(flag)}</span>`).join("");
        const decisionClass = item.decision === "candidate" ? "bullish" : "neutral";
        return `<tr data-stock-code="${escapeHtml(item.code)}">
          <td>${formatNumber(item.rank)}</td>
          <td class="stock-cell"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.code)}</small></td>
          <td class="numeric">${item.boards === null || item.boards === undefined ? "--" : `${formatNumber(item.boards, 0)}板`}</td>
          <td>${escapeHtml(item.industry || "其他")}${reason ? `<small title="${escapeHtml(reason)}">${escapeHtml(reason)}</small>` : ""}</td>
          <td><span class="status-badge ${tierClass}">${tier ? `${escapeHtml(tier)} 级` : "无"}</span><small>${item.auction_amount ? escapeHtml(formatNumber(item.auction_amount_to_mcap_pct, 2) + "% · " + (item.limit_up_probability_label || "待确认")) : "无竞价额"}</small></td>
          <td class="numeric"><span class="score-pill ${scoreClass(item.score)}">${formatNumber(item.score, 0)}</span></td>
          <td class="numeric ${toNumber(item.auction_change_pct) > 0 ? "up" : "down"}">${item.auction_change_pct == null ? "--" : formatPercent(item.auction_change_pct)}</td>
          <td class="numeric">${item.auction_amount ? formatNumber(item.auction_amount_to_mcap_pct, 2) + "%" : "--"}</td>
          <td class="numeric">${item.auction_amount ? formatNumber(item.auction_amount_ratio_pct, 2) + "%" : "--"}</td>
          <td class="numeric mobile-hide">${firstLimitTime ? escapeHtml(String(firstLimitTime).slice(0, 5)) : "--"}</td>
          <td class="numeric mobile-hide ${toNumber(openCount) === 0 ? "up" : "down"}">${openCount == null ? "--" : formatNumber(openCount, 0)}</td>
          <td class="numeric mobile-hide">${formatNumber(toNumber(firstDefined(item.float_mcap, snap.float_mcap)) / 100000000, 1)}亿</td>
          <td class="numeric mobile-hide">${formatNumber(item.first_board_score, 0)}</td>
          <td class="numeric mobile-hide">${item.auction_stage_score == null ? "--" : formatNumber(item.auction_stage_score, 0)}</td>
          <td class="mobile-hide"><span class="evidence-list">${flagText || "<span>无</span>"}</span></td>
          <td class="mobile-hide"><span class="status-badge ${decisionClass}">${item.decision === "candidate" ? "候选" : "观察"}</span></td>
        </tr>`;
      }).join("")}</tbody>
    </table>`;
    bindCandidateRows(element);
    initIcons();
  }

  async function loadDixiAuction() {
    const panel = $("#dixi-auction-panel");
    panel.hidden = false;
    setState("#dixi-auction-results", "loading", "正在读取昨日计划的今日竞价", "符合昨晚思路才执行", null);
    try {
      const data = await api("/api/dixi/auction-check", { timeout: 60000 });
      renderDixiAuction(data);
    } catch (error) {
      setState("#dixi-auction-results", "error", "竞价验证失败", error.message, "dixi");
      showToast("error", "竞价验证失败", error.message);
    }
  }

  function renderDixiAuction(data) {
    const rows = asArray(data.rows);
    $("#dixi-auction-meta").textContent = `计划日 ${data.plan_trade_date || "--"} | 验证时间 ${String(data.checked_at || "").slice(11, 16)} | ${data.quotes_available ? "竞价数据已获取" : "竞价数据不可用"}`;
    if (!rows.length) {
      $("#dixi-auction-results").innerHTML = stateMarkup("empty", "昨天没有低吸计划", "先点「生成计划」，次日再来做竞价验证。", null, true);
      initIcons();
      return;
    }
    const clsMap = { bullish: "up", warning: "down", down: "down", neutral: "" };
    $("#dixi-auction-results").innerHTML = `<table>
      <thead><tr><th>股票</th><th>昨晚买点</th><th class="numeric">计划评分</th><th class="numeric">竞价高开</th><th>验证结论</th></tr></thead>
      <tbody>${rows.map((item) => `<tr data-stock-code="${escapeHtml(item.code)}">
        <td class="stock-cell"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.code)}</small></td>
        <td>${escapeHtml(item.buy_point || "--")}</td>
        <td class="numeric">${formatNumber(item.score, 0)}</td>
        <td class="numeric ${clsMap[item.verdict_class] || ""}">${item.auction_change_pct == null ? "--" : formatPercent(item.auction_change_pct)}</td>
        <td><span class="status-badge ${item.verdict_class || "neutral"}">${escapeHtml(item.verdict)}</span></td>
      </tr>`).join("")}</tbody></table>`;
    initIcons();
  }

  async function runDixiScan() {
    const button = $("#dixi-run");
    setButtonBusy(button, true);
    try {
      const data = await api("/api/dixi/scan", { method: "POST", timeout: 120000, body: {} });
      state.dixi = data;
      state.loaded.add("dixi");
      renderDixi(data);
      setUpdatedAt(firstDefined(data.generated_at, new Date()));
      showToast("success", "低吸计划已生成", `观察池 ${asArray(data.rows).length} 只`);
    } catch (error) {
      showToast("error", "低吸计划生成失败", error.message);
      await loadDixi(true);
    } finally {
      setButtonBusy(button, false);
    }
  }

  async function loadDixi(force = false) {
    if (state.dixi && state.loaded.has("dixi") && !force) {
      renderDixi(state.dixi);
      return;
    }
    setState("#dixi-results", "loading", "正在读取最近一次低吸计划", "趋势+人气+回调反包（素衣模式）", null);
    renderMetrics("#dixi-metrics", []);
    try {
      const data = await api("/api/dixi/runs/latest", { timeout: API_TIMEOUT });
      state.dixi = data.run || null;
      state.loaded.add("dixi");
      renderDixi(state.dixi);
    } catch (error) {
      setState("#dixi-results", "error", "低吸计划加载失败", error.message, "dixi");
      showToast("error", "低吸计划加载失败", error.message);
    }
  }

  const DIXI_BUYPOINT_CLASS = { "回调反包": "bullish", "均线低吸": "warning", "急跌拉回": "bullish", "急跌观察": "warning" };

  function renderDixi(run) {
    const element = $("#dixi-results");
    if (!run) {
      element.innerHTML = stateMarkup("empty", "还没有低吸计划记录", "点击右上角「生成计划」立即执行一次；交易日 15:10 也会自动生成。", null, true);
      renderMetrics("#dixi-metrics", []);
      $("#dixi-result-meta").textContent = "--";
      return;
    }
    const rows = asArray(run.rows);
    const funnel = run.funnel || {};
    const universeCount = run.universe_count ?? funnel.main_board ?? rows.length;
    const amountQualified = run.amount_qualified_count ?? funnel.amount_qualified ?? 0;
    const decisionCount = run.candidate_decision_count ?? rows.filter((item) => item.decision === "candidate").length;
    const triggered = run.triggered_count ?? funnel.triggered ?? 0;
    const shrink = Boolean(run.market_shrink);
    renderMetrics("#dixi-metrics", [
      { label: "主板股票池", value: formatNumber(universeCount), detail: "沪深主板（排除 ST/退市）", icon: "database" },
      { label: "成交额达标", value: formatNumber(amountQualified), detail: "成交额≥3亿（大成交前提）", icon: "coins" },
      { label: "模式触发", value: formatNumber(triggered), detail: "回调≤5日+均线附近反包", icon: "repeat", className: triggered ? "up" : "" },
      { label: "候选标的", value: formatNumber(decisionCount), detail: "反包触发且评分≥60", icon: "target", className: decisionCount ? "up" : "" },
      { label: "观察池", value: formatNumber(rows.length), detail: "含均线低吸/急跌观察", icon: "list" }
    ]);
    const stateCard = $("#dixi-market-state");
    const healthy = !shrink;
    stateCard.className = `oversold-style-card ${healthy ? "supportive" : "unsupported"}`;
    stateCard.innerHTML = `<div class="style-primary"><i data-lucide="${healthy ? "circle-check-big" : "triangle-alert"}"></i><span><strong>${shrink ? "两市明显缩量：按模式纪律控制仓位" : "市场量能正常：可按计划执行低吸"}</strong><small>${run.trade_date || "日期未知"} · ${run.source || "数据源未知"} · 只做上升趋势中期、成交额前 100 的人气强票</small></span></div><div><b>${formatNumber(triggered)}</b><small>模式触发</small></div><div><b>${formatNumber(decisionCount)}</b><small>候选标的</small></div><div><b>${formatNumber(rows.length)}</b><small>观察池</small></div>`;
    $("#dixi-method").innerHTML = asArray(run.methodology).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
    $("#dixi-risk-note").textContent = `${run.threshold_status || "经验阈值，待回测。"} ${run.disclaimer || ""}`;
    $("#dixi-result-meta").textContent = `${run.trade_date || "日期未知"} | ${formatNumber(rows.length)} 只 | 总分 = 量能20% + 热度20% + 股性15% + 趋势20% + 结构25%`;
    if (!rows.length) {
      element.innerHTML = stateMarkup("empty", "当前没有符合低吸模式的标的", "趋势破位、成交额不足或无回调结构的票不会进池。", null, true);
      initIcons();
      return;
    }
    element.innerHTML = `<table>
      <thead><tr><th>排名</th><th>股票</th><th>行业</th><th>买点</th><th class="numeric">评分</th><th class="numeric">当日</th><th class="numeric">量比</th><th class="numeric mobile-hide">成交额</th><th class="numeric mobile-hide">成交额排名</th><th class="numeric mobile-hide">60日涨停</th><th class="numeric mobile-hide">回调天数</th><th class="mobile-hide">买点参考</th><th class="mobile-hide">风险旗标</th><th class="mobile-hide">决策</th></tr></thead>
      <tbody>${rows.map((item) => {
        const snap = item.snapshot || {};
        const bp = String(item.buy_point || "");
        const bpClass = DIXI_BUYPOINT_CLASS[bp] || "neutral";
        const flags = asArray(item.risk_flags);
        const flagText = flags.map((flag) => `<span>${escapeHtml(flag)}</span>`).join("");
        const decisionClass = item.decision === "candidate" ? "bullish" : "neutral";
        const buyRef = firstDefined(item.buy_reference, snap.buy_reference, "--");
        return `<tr data-stock-code="${escapeHtml(item.code)}">
          <td>${formatNumber(item.rank)}</td>
          <td class="stock-cell"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.code)}</small></td>
          <td>${escapeHtml(item.industry || "其他")}</td>
          <td><span class="status-badge ${bpClass}">${escapeHtml(bp || "--")}</span></td>
          <td class="numeric"><span class="score-pill ${scoreClass(item.score)}">${formatNumber(item.score, 0)}</span></td>
          <td class="numeric ${toNumber(item.change_pct) > 0 ? "up" : "down"}">${formatPercent(item.change_pct)}</td>
          <td class="numeric">${formatNumber(item.volume_ratio, 2)}x</td>
          <td class="numeric mobile-hide">${formatNumber(toNumber(item.amount) / 100000000, 1)}亿</td>
          <td class="numeric mobile-hide">${item.amount_rank ? `#${formatNumber(item.amount_rank)}` : "--"}</td>
          <td class="numeric mobile-hide">${formatNumber(item.activity_60d_limit_ups, 0)}</td>
          <td class="numeric mobile-hide">${item.pullback_days == null ? "--" : formatNumber(item.pullback_days, 0) + "天"}</td>
          <td class="mobile-hide"><span class="evidence-list"><span>${escapeHtml(buyRef)}</span></span></td>
          <td class="mobile-hide"><span class="evidence-list">${flagText || "<span>无</span>"}</span></td>
          <td class="mobile-hide"><span class="status-badge ${decisionClass}">${item.decision === "candidate" ? "候选" : "观察"}</span></td>
        </tr>`;
      }).join("")}</tbody>
    </table>`;
    bindCandidateRows(element);
    initIcons();
  }

  async function loadMaifu(force = false) {
    if (state.maifu && state.loaded.has("maifu") && !force) {
      renderMaifu(state.maifu);
      return;
    }
    setState("#maifu-events", "loading", "正在加载埋伏日历", "固定事件 + 大热股映射 + 热议快讯", null);
    try {
      // The first full global-news aggregation can take longer than a normal
      // dashboard request; subsequent refreshes are served from the 2-minute
      // provider cache.
      const data = await api("/api/maifu/overview", { timeout: 60000 });
      state.maifu = data || {};
      state.loaded.add("maifu");
      renderMaifu(state.maifu);
    } catch (error) {
      setState("#maifu-events", "error", "埋伏日历加载失败", error.message, "maifu");
      showToast("error", "埋伏日历加载失败", error.message);
    }
  }

  function formatNewsTime(value) {
    const formatted = formatDateTime(value);
    if (formatted === "--") return "--";
    return formatted.split(" ").pop().slice(0, 5);
  }

  function maifuNewsSourceClass(source) {
    const value = String(source || "");
    if (value.includes("新浪")) return "sina";
    if (value.includes("华尔街")) return "wallstreet";
    if (value.includes("同花顺")) return "ths";
    if (value.includes("财联社")) return "cls";
    if (value.includes("东方")) return "eastmoney";
    if (value.includes("金十")) return "jin10";
    return "default";
  }

  function renderMaifuNews(data) {
    const element = $("#maifu-news");
    if (!element) return;
    const allEvents = asArray(data.news_events);
    const filter = state.maifuNews.filter || "all";
    const keyword = String(state.maifuNews.keyword || "").trim().toLowerCase();
    let events = allEvents;
    if (["realized", "unrealized", "pending"].includes(filter)) events = events.filter((item) => item.status === filter);
    if (keyword) {
      events = events.filter((item) => {
        const text = [item.event_title, item.title, item.summary, item.topic, item.status_label, item.status_detail, ...asArray(item.sources), ...asArray(item.evidence).map((row) => row.title)].join(" ").toLowerCase();
        return text.includes(keyword);
      });
    }
    const sourceStats = Object.values(data.news_sources || {})
      .filter((item) => item && item.ok)
      .sort((a, b) => Number(b.count || 0) - Number(a.count || 0))
      .slice(0, 6);
    $("#maifu-news-source-stats").innerHTML = sourceStats.map((item) => `<span class="maifu-source-stat">${escapeHtml(item.name || item.source || "来源")} <b>${formatNumber(item.count || 0)}</b></span>`).join("");
    if (!events.length) {
      element.innerHTML = stateMarkup("empty", "没有匹配的事件", keyword ? "请更换关键词或切换筛选条件。" : "全球新闻正在积累，暂时没有可跟踪事件。", null, true);
      return;
    }
    element.innerHTML = events.slice(0, 500).map((item) => {
      const statusClass = item.status === "realized" ? "realized" : item.status === "unrealized" ? "unrealized" : "pending";
      const evidence = asArray(item.evidence).slice(0, 3).map((row) => `<span class="maifu-event-evidence"><b>${escapeHtml(row.source || "来源")}</b> ${escapeHtml(row.title || "")}</span>`).join("");
      const sources = asArray(item.sources).slice(0, 5).join("、");
      const codes = asArray(item.related_codes).slice(0, 6).join("、");
      return `<article class="maifu-event-item ${statusClass}">
        <div class="maifu-news-line">
          <span class="maifu-event-status ${statusClass}">${escapeHtml(item.status_label || "待验证")}</span>
          <span class="maifu-event-topic">${escapeHtml(item.topic || "综合")}</span>
          <strong class="maifu-news-title">${escapeHtml(item.event_title || item.title || "--")}</strong>
          <span class="maifu-event-count">${formatNumber(item.news_count || 0)} 条跟进</span>
        </div>
        <p class="maifu-event-summary">${escapeHtml(item.summary || item.status_detail || "")}</p>
        <p class="maifu-event-detail">当前判定：${escapeHtml(item.status_detail || "")}${item.result_signal ? ` · 结果信号：${escapeHtml(item.result_signal)}` : ""}</p>
        <div class="maifu-event-meta">来源 ${escapeHtml(sources || "--")} · 首次 ${escapeHtml(formatNewsTime(item.first_seen))} · 最近 ${escapeHtml(formatNewsTime(item.last_seen))}${codes ? ` · 关联代码 ${escapeHtml(codes)}` : ""}</div>
        ${evidence ? `<div class="maifu-event-evidence-list">${evidence}</div>` : ""}
      </article>`;
    }).join("");
  }

  function renderMaifu(data) {
    const events = asArray(data.events);
    const hot = asArray(data.hot_stocks);
    const news = asArray(data.news);
    renderMetrics("#maifu-metrics", [
      { label: "窗口内事件", value: formatNumber(data.events_in_window), detail: "正处于提前埋伏窗口", icon: "calendar-clock", className: data.events_in_window ? "up" : "" },
      { label: "近期事件总数", value: formatNumber(events.length), detail: "未来 75 天内的固定事件", icon: "calendar" },
      { label: "连板大热股", value: formatNumber(data.hot_stocks_count), detail: "FFD 涨停池连板 ≥2 的映射源", icon: "flame" },
      { label: "连板梯队", value: formatNumber(data.board_ladder ? Object.keys(data.board_ladder).length : 0), detail: Object.entries(data.board_ladder || {}).map(([k, v]) => `${k}:${v}`).join(" ") || "暂无", icon: "bar-chart-3" },
      { label: "全球新闻事件", value: formatNumber(asArray(data.news_events).length), detail: `${formatNumber(news.length)} 条原始新闻已归并`, icon: "rss" }
    ]);
    $("#maifu-events-meta").textContent = `交易日 ${data.trade_date || "--"} | ${events.length} 个事件`;
    $("#maifu-hot-meta").textContent = `${formatNumber(hot.length)} 只连板股`;
    const newsSources = Object.values(data.news_sources || {});
    const healthyNewsSources = newsSources.filter((item) => item && item.ok).length;
    const cachedNews = news.some((item) => item && item.stale);
    const latestNews = news.map((item) => item.published || item.time).filter(Boolean).sort().at(-1);
    const trackedEvents = asArray(data.news_events);
    $("#maifu-news-meta").textContent = `${latestNews ? `最近 ${formatNewsTime(latestNews)} · ` : ""}${formatNumber(trackedEvents.length)} 个事件 · ${formatNumber(news.length)} 条新闻 · ${healthyNewsSources || "多"} 个来源${cachedNews ? " · 含缓存" : ""}`;
    const eventRows = events.map((item) => {
      const inWindow = Boolean(item.in_window);
      const matched = item.matched || {};
      const examples = asArray(matched.examples).map((s) => `${escapeHtml(s.name)}(${escapeHtml(s.code)})`).join("、");
      return `<tr>
        <td><span class="status-badge ${inWindow ? "bullish" : "neutral"}">${escapeHtml(item.status || "")}</span><small>${item.days_to_start} 天后启动</small></td>
        <td><strong>${escapeHtml(item.name)}</strong><small>窗口 ${escapeHtml(String(item.advance_days))} 天 · ${escapeHtml(item.start_date || "")}</small></td>
        <td>${escapeHtml(item.concepts || "--")}</td>
        <td><span class="evidence-list"><span>${escapeHtml(examples || item.note || "--")}</span></span></td>
        <td class="numeric">${formatNumber(matched.count || 0)} 只</td>
      </tr>`;
    }).join("");
    $("#maifu-events").innerHTML = `<table><thead><tr><th>状态</th><th>事件</th><th>受益方向</th><th>本地行业匹配示例</th><th class="numeric">匹配数</th></tr></thead><tbody>${eventRows || ""}</tbody></table>`;
    $("#maifu-hot").innerHTML = hot.length ? `<table><thead><tr><th>股票</th><th class="numeric">连板</th><th class="numeric">炸板</th><th>首板时间</th><th>题材</th></tr></thead><tbody>${hot.map((item) => `<tr data-stock-code="${escapeHtml(item.code)}"><td class="stock-cell"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.code)}</small></td><td class="numeric up">${formatNumber(item.lianban_count, 0)}板</td><td class="numeric">${formatNumber(item.open_count, 0)}</td><td>${escapeHtml(String(item.first_limit_time || "--").slice(0, 5))}</td><td>${escapeHtml(item.limit_reason || item.related_concepts || "--")}</td></tr>`).join("")}</tbody></table>` : stateMarkup("empty", "暂无连板大热股", "涨停池数据就绪后会自动展示连板梯队。", null, true);
    renderMaifuNews(data);
    $("#maifu-method").innerHTML = asArray(data.methodology).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
    $("#maifu-risk-note").textContent = `${data.threshold_status || ""} ${data.disclaimer || ""}`;
    initIcons();
  }

  function openStock(code) {
    const clean = String(code || "").match(/\d{6}/)?.[0];
    if (!clean) return;
    state.stock.code = clean;
    state.stock.analysis = null;
    state.stock.deep = null;
    $("#stock-code").value = clean;
    $("#global-stock-code").value = clean;
    navigate("stock");
  }

  async function loadStock(force = false) {
    if (state.stock.analysis && !force) {
      renderStockAnalysis(state.stock.analysis);
      return;
    }
    const code = state.stock.code || DEFAULT_CODE;
    const requestId = ++state.stock.requestId;
    setState("#stock-quote", "loading", "正在加载行情", `股票代码 ${code}`, null, true);
    setState("#stock-signal", "loading", "正在计算信号", "", null, true);
    setState("#stock-score-breakdown", "loading", "正在加载规则库综合", "", null, true);
    drawStockChart([]);
    try {
      const data = await api(`/api/stocks/${encodeURIComponent(code)}/analysis`, { timeout: 90000 });
      if (requestId !== state.stock.requestId) return;
      state.stock.analysis = data || {};
      state.stock.deep = null;
      state.loaded.add("stock");
      renderStockAnalysis(state.stock.analysis);
      setUpdatedAt(firstDefined(data.data_as_of, data.as_of, data.quote && data.quote.data_as_of), Boolean(data.data_delayed || data.stale));
    } catch (error) {
      if (requestId !== state.stock.requestId) return;
      setState("#stock-quote", "error", "个股行情加载失败", error.message, "stock", true);
      setState("#stock-signal", "error", "信号加载失败", error.message, "stock", true);
      setState("#stock-score-breakdown", "error", "规则库综合加载失败", error.message, "stock", true);
      drawStockChart([]);
      showToast("error", "个股分析失败", error.message);
    }
  }

  function quoteData(data) {
    return firstDefined(data.quote, data.snapshot, data.market_data, data.realtime, data);
  }

  function renderStockAnalysis(data) {
    const quote = quoteData(data);
    const name = firstDefined(quote.name, data.name, data.stock_name, "未知股票");
    const code = firstDefined(quote.code, data.code, state.stock.code);
    const price = firstDefined(quote.price, quote.latest, quote.close, data.price);
    const change = firstDefined(quote.change_pct, quote.pct_change, quote.change, 0);
    const previous = firstDefined(quote.pre_close, quote.previous_close, quote.prev_close);
    const stats = [
      ["今开", firstDefined(quote.open, quote.open_price)],
      ["最高", firstDefined(quote.high, quote.high_price)],
      ["最低", firstDefined(quote.low, quote.low_price)],
      ["量比", firstDefined(quote.volume_ratio, quote.vr)],
      ["换手", firstDefined(quote.turnover_rate, quote.turnover)],
      ["总市值", firstDefined(quote.market_cap, quote.total_market_cap)]
    ];
    $("#stock-quote").innerHTML = `<div class="quote-primary"><div class="stock-name"><span>${escapeHtml(name)} · ${escapeHtml(code)}</span><span class="status-badge neutral">${escapeHtml(firstDefined(quote.industry, data.industry, "A 股"))}</span></div><div class="quote-price-line"><strong class="${changeClass(change)}">${formatNumber(price, 2)}</strong><span class="${changeClass(change)}">${formatPercent(change)}</span></div><small class="muted">昨收 ${formatNumber(previous, 2)} · 行情 ${formatDateTime(firstDefined(quote.data_as_of, data.data_as_of, data.as_of))}</small></div>${stats.map(([label, value], index) => `<div class="quote-stat"><span>${label}</span><strong class="${index === 1 ? "up" : index === 2 ? "down" : ""}">${label === "总市值" ? formatCompact(value) : formatNumber(value, label === "换手" || label === "量比" ? 2 : 2)}${label === "换手" && value !== undefined ? "%" : ""}</strong></div>`).join("")}`;
    $("#stock-title").textContent = `${name} · 个股分析`;
    $("#stock-subtitle").textContent = `${code} · ${strategyText(firstDefined(data.summary, data.description, "行情、规则库综合与证据"))}`;
    renderStockSignal(data);
    renderScoreBreakdown(data);
    const candles = extractCandles(data);
    drawStockChart(candles);
    const range = candles.length ? `${candles[0].date} 至 ${candles[candles.length - 1].date} · ${candles.length} 个交易日` : "暂无 K 线";
    $("#chart-range").textContent = range;
    initIcons();
  }

  function renderStockSignal(data) {
    const signal = firstDefined(data.signal, data.recommendation, data.decision, data.technical, {});
    const score = toNumber(firstDefined(signal.score, signal.total, data.total_score, data.score, 0), 0);
    const meta = signalMeta(firstDefined(signal.label, signal.direction, signal.signal, data.signal_label), Number.NaN);
    const summary = strategyText(firstDefined(signal.summary, signal.reason, data.summary, "等待更多数据确认"));
    const facts = [
      ["MA5", firstDefined(signal.ma5, data.ma5_signal, "--")],
      ["KDJ", firstDefined(signal.kdj, data.kdj_signal, "--")],
      ["MACD", firstDefined(signal.macd, data.macd_signal, "--")],
      ["MA10", firstDefined(signal.ma10, data.ma10_signal, "--")]
    ];
    const ringClass = meta.key === "strong" || meta.key === "bullish" ? "high" : meta.key === "bearish" ? "low" : "mid";
    $("#stock-signal").innerHTML = `<div class="signal-summary"><div class="signal-score ${ringClass}"><strong>${score > 0 ? "+" : ""}${formatNumber(score)}</strong></div><span class="status-badge ${meta.key}"><i data-lucide="${meta.icon}"></i>&nbsp;${escapeHtml(meta.label)}</span><h3>${escapeHtml(firstDefined(signal.action, signal.title, meta.label))}</h3><p>${escapeHtml(summary)}</p><div class="signal-facts">${facts.map(([label, value]) => `<div class="signal-fact"><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`).join("")}</div></div>`;
  }

  function normalizeScoreBreakdown(data) {
    const raw = firstDefined(data.score_breakdown, data.scores, data.dimensions, data.technical && firstDefined(data.technical.breakdown, data.technical.components), data.analysis && data.analysis.scores, []);
    if (Array.isArray(raw)) {
      return raw.map((item, index) => ({
        key: firstDefined(item.key, item.dimension, item.name, `score_${index}`),
         label: strategyText(firstDefined(item.label, item.name, DIMENSION_LABELS[item.key], `维度 ${index + 1}`)),
        score: toNumber(firstDefined(item.score, item.value, item.points), 0),
         note: strategyText(firstDefined(item.note, item.reason, item.evidence, item.label, "暂无证据说明")),
        state: firstDefined(item.signal, item.state, item.judgement, "")
      }));
    }
    return Object.entries(raw || {}).map(([key, value]) => {
      const item = value && typeof value === "object" ? value : { score: value };
      return {
        key,
        label: strategyText(firstDefined(item.label, item.name, DIMENSION_LABELS[key], key)),
        score: toNumber(firstDefined(item.score, item.value, item.points), 0),
        note: strategyText(firstDefined(item.note, item.reason, item.evidence, "暂无证据说明")),
        state: firstDefined(item.signal, item.state, "")
      };
    });
  }

  function renderScoreBreakdown(data) {
    const rows = normalizeScoreBreakdown(data);
    const element = $("#stock-score-breakdown");
    if (!rows.length) {
      element.innerHTML = stateMarkup("empty", "暂无规则库综合拆解", "接口未返回各维度证据", null, true);
      return;
    }
    element.innerHTML = `<div class="score-grid">${rows.slice(0, 12).map((item) => {
      const compactTechnical = Math.abs(item.score) <= 3;
      const meta = compactTechnical
        ? item.score >= 2 ? { key: "strong", label: "强多" } : item.score > 0 ? { key: "bullish", label: "偏多" } : item.score <= -2 ? { key: "bearish", label: "强空" } : item.score < 0 ? { key: "bearish", label: "偏空" } : { key: "neutral", label: "中性" }
        : signalMeta(item.state, item.score);
      const width = compactTechnical ? ((item.score + 3) / 6) * 100 : Math.max(0, Math.min(100, item.score));
      return `<div class="score-dimension"><div class="score-dimension-head"><strong>${escapeHtml(item.label)}</strong><span class="status-badge ${meta.key}">${escapeHtml(meta.label)}</span></div><div class="score-number ${scoreClass(item.score) === "low" ? "down" : scoreClass(item.score) === "high" ? "up" : ""}">${formatNumber(item.score)}</div><div class="score-track"><span style="width:${width}%"></span></div><p title="${escapeHtml(item.note)}">${escapeHtml(item.note)}</p></div>`;
    }).join("")}</div>`;
    $("#score-version").textContent = `规则库版本 ${firstDefined(data.strategy_version, data.version, "当前")}`;
  }

  function extractCandles(data) {
    const chart = firstDefined(data.kline, data.klines, data.prices, data.history, data.daily, data.chart && firstDefined(data.chart.kline, data.chart.candles), []);
    if (chart && !Array.isArray(chart) && Array.isArray(chart.dates)) {
      return chart.dates.map((date, index) => ({
        date,
        open: toNumber(chart.open?.[index], chart.close?.[index]),
        close: toNumber(chart.close?.[index], 0),
        high: toNumber(chart.high?.[index], chart.close?.[index]),
        low: toNumber(chart.low?.[index], chart.close?.[index]),
        volume: toNumber(chart.volume?.[index], 0)
      })).filter((item) => item.close > 0);
    }
    return asArray(chart).map((row) => {
      if (Array.isArray(row)) {
        return {
          date: String(row[0] ?? ""),
          open: toNumber(row[1], 0),
          close: toNumber(row[2], 0),
          high: toNumber(row[3], 0),
          low: toNumber(row[4], 0),
          volume: toNumber(row[5], 0)
        };
      }
      const close = toNumber(firstDefined(row.close, row.price, row.latest), 0);
      return {
        date: String(firstDefined(row.date, row.trade_date, row.datetime, row.time, "")),
        open: toNumber(firstDefined(row.open, row.open_price), close),
        close,
        high: toNumber(firstDefined(row.high, row.high_price), close),
        low: toNumber(firstDefined(row.low, row.low_price), close),
        volume: toNumber(firstDefined(row.volume, row.vol, row.amount), 0)
      };
    }).filter((item) => item.close > 0 && item.high > 0 && item.low > 0).slice(-120);
  }

  function setupChartInteractions() {
    const canvas = $("#stock-chart");
    if (!canvas || state.chart.bound) return;
    state.chart.bound = true;
    canvas.addEventListener("pointermove", (event) => {
      const rect = canvas.getBoundingClientRect();
      const candles = state.chart.candles;
      if (!candles.length || rect.width <= 0) return;
      const plotLeft = 48;
      const plotWidth = Math.max(1, rect.width - 66);
      const index = Math.max(0, Math.min(candles.length - 1, Math.floor(((event.clientX - rect.left - plotLeft) / plotWidth) * candles.length)));
      state.chart.hoverIndex = index;
      drawStockChart(candles, index);
      const candle = candles[index];
      const tooltip = $("#chart-tooltip");
      tooltip.hidden = false;
      tooltip.innerHTML = `${escapeHtml(candle.date)}　开 ${formatNumber(candle.open, 2)}　高 ${formatNumber(candle.high, 2)}　低 ${formatNumber(candle.low, 2)}　收 <strong class="${changeClass(candle.close - candle.open)}">${formatNumber(candle.close, 2)}</strong>　量 ${formatCompact(candle.volume)}`;
    });
    canvas.addEventListener("pointerleave", () => {
      state.chart.hoverIndex = -1;
      $("#chart-tooltip").hidden = true;
      drawStockChart(state.chart.candles);
    });
    window.addEventListener("resize", debounce(() => drawStockChart(state.chart.candles, state.chart.hoverIndex), 120));
  }

  function drawStockChart(candles, hoverIndex = -1) {
    const canvas = $("#stock-chart");
    if (!canvas) return;
    state.chart.candles = candles || [];
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(300, rect.width || canvas.parentElement?.clientWidth || 600);
    const height = Math.max(260, rect.height || 360);
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    const context = canvas.getContext("2d");
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    context.clearRect(0, 0, width, height);
    context.fillStyle = "#ffffff";
    context.fillRect(0, 0, width, height);

    if (!candles || !candles.length) {
      context.fillStyle = "#667085";
      context.font = '12px "Microsoft YaHei", sans-serif';
      context.textAlign = "center";
      context.fillText("暂无可绘制的价格数据", width / 2, height / 2);
      return;
    }

    const margin = { top: 16, right: 18, bottom: 28, left: 48 };
    const volumeHeight = Math.max(48, height * 0.18);
    const gap = 18;
    const priceBottom = height - margin.bottom - volumeHeight - gap;
    const plotWidth = width - margin.left - margin.right;
    const priceHeight = priceBottom - margin.top;
    const lows = candles.map((item) => item.low);
    const highs = candles.map((item) => item.high);
    const minPrice = Math.min(...lows);
    const maxPrice = Math.max(...highs);
    const pricePadding = Math.max((maxPrice - minPrice) * 0.08, maxPrice * 0.002);
    const lowScale = minPrice - pricePadding;
    const highScale = maxPrice + pricePadding;
    const priceRange = Math.max(0.0001, highScale - lowScale);
    const maxVolume = Math.max(1, ...candles.map((item) => item.volume));
    const step = plotWidth / candles.length;
    const candleWidth = Math.max(1, Math.min(10, step * 0.62));
    const yPrice = (value) => margin.top + ((highScale - value) / priceRange) * priceHeight;

    context.strokeStyle = "#e4e7ec";
    context.fillStyle = "#667085";
    context.lineWidth = 1;
    context.font = '10px "Microsoft YaHei", sans-serif';
    context.textAlign = "right";
    context.textBaseline = "middle";
    for (let line = 0; line <= 4; line += 1) {
      const y = margin.top + (priceHeight / 4) * line;
      const value = highScale - (priceRange / 4) * line;
      context.beginPath();
      context.moveTo(margin.left, Math.round(y) + 0.5);
      context.lineTo(width - margin.right, Math.round(y) + 0.5);
      context.stroke();
      context.fillText(value.toFixed(2), margin.left - 6, y);
    }

    candles.forEach((item, index) => {
      const x = margin.left + step * (index + 0.5);
      const rising = item.close >= item.open;
      const color = rising ? "#d92d20" : "#039855";
      const openY = yPrice(item.open);
      const closeY = yPrice(item.close);
      const highY = yPrice(item.high);
      const lowY = yPrice(item.low);
      context.strokeStyle = color;
      context.fillStyle = color;
      context.lineWidth = 1;
      context.beginPath();
      context.moveTo(Math.round(x) + 0.5, highY);
      context.lineTo(Math.round(x) + 0.5, lowY);
      context.stroke();
      const bodyY = Math.min(openY, closeY);
      const bodyHeight = Math.max(1, Math.abs(closeY - openY));
      if (rising && bodyHeight > 1.5) {
        context.strokeRect(x - candleWidth / 2, bodyY, candleWidth, bodyHeight);
      } else {
        context.fillRect(x - candleWidth / 2, bodyY, candleWidth, bodyHeight);
      }
      const volumeY = height - margin.bottom - (item.volume / maxVolume) * volumeHeight;
      context.globalAlpha = 0.52;
      context.fillRect(x - candleWidth / 2, volumeY, candleWidth, height - margin.bottom - volumeY);
      context.globalAlpha = 1;
    });

    const labelIndexes = [0, Math.floor((candles.length - 1) / 2), candles.length - 1];
    context.fillStyle = "#667085";
    context.textAlign = "center";
    context.textBaseline = "top";
    labelIndexes.forEach((index) => {
      const x = margin.left + step * (index + 0.5);
      context.fillText(String(candles[index].date).slice(5, 10), x, height - margin.bottom + 7);
    });

    if (hoverIndex >= 0 && hoverIndex < candles.length) {
      const x = margin.left + step * (hoverIndex + 0.5);
      const y = yPrice(candles[hoverIndex].close);
      context.strokeStyle = "#98a2b3";
      context.setLineDash([3, 3]);
      context.beginPath();
      context.moveTo(x, margin.top);
      context.lineTo(x, height - margin.bottom);
      context.moveTo(margin.left, y);
      context.lineTo(width - margin.right, y);
      context.stroke();
      context.setLineDash([]);
    }
  }

  async function loadDeep(force = false) {
    if (state.stock.deep && !force) {
      renderDeep(state.stock.deep);
      return;
    }
    setState("#force-timeline", "loading", "正在拆解主力轨迹", "", null);
    setState("#deep-conclusion", "loading", "正在生成结论", "", null);
    try {
      const data = await api(`/api/stocks/${encodeURIComponent(state.stock.code)}/deep`);
      state.stock.deep = data || {};
      renderDeep(state.stock.deep);
    } catch (error) {
      setState("#force-timeline", "error", "深度分析加载失败", error.message, "stock");
      setState("#deep-conclusion", "error", "结论加载失败", error.message, "stock");
      showToast("error", "深度分析失败", error.message);
    }
  }

  function renderDeep(data) {
    const events = asArray(firstDefined(data.timeline, data.events, data.stages, data.phases, data.traces, []));
    const timeline = $("#force-timeline");
    if (!events.length) {
      timeline.innerHTML = stateMarkup("empty", "暂无主力轨迹", "最近 30 日没有可识别的阶段", null, true);
    } else {
      timeline.innerHTML = `<div class="timeline">${events.map((event) => {
        const type = firstDefined(event.type, event.stage, event.label, "阶段");
        const direction = signalMeta(firstDefined(event.signal, event.direction), event.score);
        const start = firstDefined(event.start_date, event.start, event.date, "--");
        const end = firstDefined(event.end_date, event.end, "");
        return `<div class="timeline-item"><span class="timeline-dot"></span><span class="timeline-date">${escapeHtml(end && end !== start ? `${start}–${end}` : start)}</span><span class="timeline-content"><strong>${escapeHtml(type)}</strong><small>${escapeHtml(firstDefined(event.description, event.reason, event.evidence, "暂无补充说明"))}</small></span><span class="status-badge ${direction.key}">${escapeHtml(firstDefined(event.change_pct !== undefined ? formatPercent(event.change_pct) : null, direction.label))}</span></div>`;
      }).join("")}</div>`;
    }
    const conclusion = firstDefined(data.conclusion, data.analysis, data.summary, {});
    const blocks = typeof conclusion === "string" ? [
      ["模型结论", conclusion, "基于最近 30 个交易日"]
    ] : [
      ["方向", firstDefined(conclusion.direction, conclusion.signal, data.direction, "待确认"), firstDefined(conclusion.direction_reason, conclusion.summary, "")],
      ["关键价位", firstDefined(conclusion.key_level, conclusion.trigger_price, data.key_level, "--"), firstDefined(conclusion.level_reason, conclusion.trigger_condition, "")],
      ["风险条件", firstDefined(conclusion.risk, conclusion.stop_condition, data.risk, "--"), firstDefined(conclusion.risk_reason, "")],
      ["策略建议", firstDefined(conclusion.advice, conclusion.action, data.advice, "保持观察"), firstDefined(conclusion.advice_reason, "")]
    ];
    $("#deep-conclusion").innerHTML = blocks.map(([label, value, note]) => `<div class="conclusion-block"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong>${note ? `<p>${escapeHtml(note)}</p>` : ""}</div>`).join("");
    $("#deep-period").textContent = firstDefined(data.period, data.date_range, "最近 30 个交易日");
    initIcons();
  }

  async function loadReview(force = false) {
    if (state.loaded.has("review") && !force) {
      renderReview(state.review || {});
      return;
    }
    renderMetrics("#review-metrics", []);
    setState("#review-table", "loading", "正在加载复盘记录", "", null);
    setState("#review-diagnosis", "loading", "正在加载策略诊断", "", null);
    try {
      const data = await api("/api/reviews/latest");
      state.review = data || {};
      state.loaded.add("review");
      renderReview(state.review);
      setUpdatedAt(firstDefined(data.finished_at, data.updated_at, new Date()));
    } catch (error) {
      setState("#review-table", "error", "复盘加载失败", error.message, "review");
      setState("#review-diagnosis", "error", "策略诊断加载失败", error.message, "review");
      showToast("error", "复盘加载失败", error.message);
    }
  }

  function reviewRecords(data) {
    return asArray(firstDefined(data.records, data.items, data.recommendations, data.results, data.picks, []));
  }

  function renderReview(data) {
    const summary = firstDefined(data.summary, data.stats, {});
    const records = reviewRecords(data);
    const pushed = firstDefined(summary.pushed, summary.push_count, records.filter((item) => firstDefined(item.pushed, item.type === "push")).length);
    const hits = firstDefined(summary.hits, summary.hit_count, records.filter((item) => Boolean(firstDefined(item.hit, item.success))).length);
    const hitRate = firstDefined(summary.hit_rate, pushed ? (hits / pushed) * 100 : 0);
    renderMetrics("#review-metrics", [
      { label: "沪深主板", value: formatNumber(firstDefined(summary.universe_count, summary.total, summary.first_boards, summary.first_board_count, 0)), detail: "复盘覆盖样本", icon: "database" },
      { label: "候选数", value: formatNumber(firstDefined(summary.candidates, summary.candidate_count, 0)), detail: "完成规则库综合", icon: "filter" },
      { label: "推送数", value: formatNumber(pushed), detail: pushed ? "进入结果追踪" : "今日无推送", icon: "send" },
      { label: "命中率", value: formatPercent(hitRate, 0), detail: `${formatNumber(hits)} / ${formatNumber(pushed)}`, icon: "target", className: hitRate >= 50 ? "up" : "" }
    ]);
    const element = $("#review-table");
    if (!records.length) {
      element.innerHTML = stateMarkup("empty", "暂无复盘记录", "当前交易日没有推送或候选记录", null, true);
    } else {
      element.innerHTML = `<table><thead><tr><th>类型</th><th>股票</th><th class="numeric">评分</th><th class="numeric">开盘</th><th class="numeric">收盘</th><th class="numeric">当日涨跌</th><th>命中</th><th class="mobile-hide">复盘说明</th></tr></thead><tbody>${records.map((row) => {
        const code = firstDefined(row.code, row.stock_code, "");
        const hit = Boolean(firstDefined(row.hit, row.success, row.is_hit, false));
        const change = firstDefined(row.change_pct, row.pnl_pct, row.return_pct, 0);
         return `<tr data-stock-code="${escapeHtml(code)}"><td>${escapeHtml(strategyText(firstDefined(row.type_label, row.type, row.pushed ? "推送" : "候选")))}</td><td class="stock-cell"><strong>${escapeHtml(firstDefined(row.name, row.stock_name, "未知股票"))}</strong><small>${escapeHtml(code)}</small></td><td class="numeric">${formatNumber(firstDefined(row.score, row.total_score), 0)}</td><td class="numeric">${formatNumber(firstDefined(row.open, row.open_price), 2)}</td><td class="numeric">${formatNumber(firstDefined(row.close, row.close_price), 2)}</td><td class="numeric ${changeClass(change)}">${formatPercent(change)}</td><td><span class="status-badge ${hit ? "bullish" : "neutral"}">${hit ? "命中" : "未命中"}</span></td><td class="reason-cell mobile-hide">${escapeHtml(strategyText(firstDefined(row.reason, row.review, row.comment, "--")))}</td></tr>`;
      }).join("")}</tbody></table>`;
      bindCandidateRows(element);
    }
    let diagnoses = asArray(firstDefined(data.diagnosis, data.diagnostics, data.insights, []));
    if (!diagnoses.length) diagnoses = [
      data.market_summary ? { title: "市场概况", detail: data.market_summary } : null,
      data.strategy_diagnosis ? { title: "策略诊断", detail: data.strategy_diagnosis } : null,
      data.advice ? { title: "交易建议", detail: data.advice } : null
    ].filter(Boolean);
    $("#review-diagnosis").innerHTML = diagnoses.length
      ? `<div class="diagnosis-list">${diagnoses.map((item) => {
          const value = typeof item === "string" ? { title: "复盘观察", detail: item } : item;
          return `<div class="diagnosis-item"><strong>${escapeHtml(strategyText(firstDefined(value.title, value.label, value.type, "复盘观察")))}</strong><span>${escapeHtml(strategyText(firstDefined(value.detail, value.description, value.reason, value.advice, "--")))}</span></div>`;
        }).join("")}</div>`
      : stateMarkup("empty", "暂无策略诊断", "当前接口未返回诊断信息", null, true);
    $("#review-date").textContent = firstDefined(data.date, data.trade_date, localDate());
    const status = $("#review-status");
    status.className = `status-badge ${String(firstDefined(data.status, "completed")).includes("fail") ? "bearish" : "bullish"}`;
    status.textContent = String(firstDefined(data.status, "completed")).includes("fail") ? "执行失败" : "复盘完成";
    initIcons();
  }

  async function loadBacktests(force = false) {
    if (state.loaded.has("backtest") && !force) {
      renderBacktests(state.backtests || {});
      return;
    }
    renderMetrics("#backtest-metrics", []);
    setState("#backtest-table", "loading", "正在读取回测结果", "", null);
    try {
      const data = await api("/api/backtests");
      state.backtests = data || {};
      state.loaded.add("backtest");
      renderBacktests(state.backtests);
      setUpdatedAt(firstDefined(data.updated_at, new Date()));
    } catch (error) {
      setState("#backtest-table", "error", "回测结果加载失败", error.message, "backtest");
      showToast("error", "回测加载失败", error.message);
    }
  }

  function backtestRows(data) {
    return asArray(firstDefined(data.records, data.items, data.results, data.trades, data.recommendations, []));
  }

  function renderBacktests(data) {
    const summary = firstDefined(data.summary, data.stats, {});
    const rows = backtestRows(data);
    const hits = firstDefined(summary.hits, summary.hit_count, rows.filter((row) => Boolean(firstDefined(row.hit, row.success))).length);
    const total = firstDefined(summary.total, summary.push_count, rows.length);
    const hitRate = firstDefined(summary.hit_rate, total ? (hits / total) * 100 : 0);
    renderMetrics("#backtest-metrics", [
      { label: "交易日", value: formatNumber(firstDefined(summary.trade_days, summary.trading_days, summary.days, 0)), detail: firstDefined(summary.period, data.period, "当前区间"), icon: "calendar-days" },
      { label: "推送记录", value: formatNumber(total), detail: `${formatNumber(hits)} 条命中`, icon: "send" },
      { label: "平均涨跌", value: formatPercent(firstDefined(summary.avg_return, summary.average_return, summary.avg_pnl_pct, 0)), detail: "按收盘价计算", icon: "chart-no-axes-combined", className: changeClass(firstDefined(summary.avg_return, summary.avg_pnl_pct, 0)) },
      { label: "命中率", value: formatPercent(hitRate, 0), detail: "达到策略命中条件", icon: "target", className: hitRate >= 50 ? "up" : "" }
    ]);
    const element = $("#backtest-table");
    if (!rows.length) {
      element.innerHTML = stateMarkup("empty", "暂无回测记录", "选择日期范围并运行一次回测", null, true);
    } else {
      element.innerHTML = `<table><thead><tr><th>日期</th><th>类别</th><th>股票</th><th>命中</th><th class="numeric">总涨跌</th><th class="numeric">7 日最佳</th><th class="numeric">评分</th><th class="mobile-hide">策略版本</th></tr></thead><tbody>${rows.map((row) => {
        const code = firstDefined(row.code, row.stock_code, "");
        const hit = Boolean(firstDefined(row.hit, row.success, row.is_hit, false));
        const totalReturn = firstDefined(row.total_return, row.return_pct, row.change_pct, row.pnl_pct, 0);
        const best = firstDefined(row.best_7d, row.best_7d_pct, row.best_7d_return, row.max_return_7d, 0);
        return `<tr data-stock-code="${escapeHtml(code)}"><td>${escapeHtml(firstDefined(row.date, row.trade_date, "--"))}</td><td>${escapeHtml(firstDefined(row.type_label, row.type, row.category, "推送"))}</td><td class="stock-cell"><strong>${escapeHtml(firstDefined(row.name, row.stock_name, "未知股票"))}</strong><small>${escapeHtml(code)}</small></td><td><span class="status-badge ${hit ? "bullish" : "neutral"}">${hit ? "命中" : "未命中"}</span></td><td class="numeric ${changeClass(totalReturn)}">${formatPercent(totalReturn)}</td><td class="numeric ${changeClass(best)}">${formatPercent(best)}</td><td class="numeric">${formatNumber(firstDefined(row.score, row.total_score), 0)}</td><td class="mobile-hide">${escapeHtml(firstDefined(row.strategy_version, row.version, "--"))}</td></tr>`;
      }).join("")}</tbody></table>`;
      bindCandidateRows(element);
    }
    $("#backtest-meta").textContent = firstDefined(data.period, summary.period, `${formatNumber(rows.length)} 条记录`);
    initIcons();
  }

  async function runBacktest() {
    const button = $("#backtest-run");
    const start = $("#backtest-from").value;
    const end = $("#backtest-to").value;
    setButtonBusy(button, true, "回测中");
    try {
      const data = await api("/api/backtests/run", { method: "POST", body: { start_date: start, end_date: end } });
      state.backtests = data || {};
      state.loaded.add("backtest");
      renderBacktests(state.backtests);
      showToast("success", "回测完成", `${start} 至 ${end}`);
    } catch (error) {
      showToast("error", "回测失败", error.message);
    } finally {
      setButtonBusy(button, false);
    }
  }

  async function loadJobs(force = false) {
    if (state.loaded.has("automation") && !force) {
      renderJobs(state.jobs);
      return;
    }
    setState("#jobs-list", "loading", "正在加载任务计划", "", null);
    try {
      const data = await api("/api/jobs");
      state.jobs = asArray(firstDefined(data.jobs, data.items, data, []));
      state.loaded.add("automation");
      renderJobs(state.jobs);
      setUpdatedAt(new Date());
    } catch (error) {
      setState("#jobs-list", "error", "任务计划加载失败", error.message, "automation");
      showToast("error", "任务加载失败", error.message);
    }
  }

  function renderJobs(jobs) {
    const element = $("#jobs-list");
    $("#jobs-meta").textContent = `${formatNumber(jobs.length)} 个任务`;
    if (!jobs.length) {
      element.innerHTML = stateMarkup("empty", "暂无定时任务", "后端尚未配置任务计划", null, true);
      return;
    }
    element.innerHTML = `<div class="job-list">${jobs.map((job) => {
      const id = firstDefined(job.id, job.job_id, job.name);
      const enabled = firstDefined(job.enabled, job.active, true) !== false;
      const status = String(firstDefined(job.status, job.last_status, "idle")).toLowerCase();
      const warning = status.includes("warning");
      const badgeClass = status.includes("fail") || status.includes("error") ? "bearish" : status.includes("run") || warning ? "warning" : "neutral";
      const statusLabel = status.includes("fail") || status.includes("error") ? "失败" : status.includes("run") ? "运行中" : warning ? "投递警告" : "正常";
      const channel = firstDefined(job.channel, "local") === "wecom" ? "企业微信" : "本地";
      return `<div class="job-row" data-job-id="${escapeHtml(id)}"><div class="job-title"><i data-lucide="${firstDefined(job.icon, "clock-3")}"></i><span><strong>${escapeHtml(firstDefined(job.name, job.title, id))}</strong><small>${escapeHtml(firstDefined(job.description, job.command, "自动策略任务"))}</small></span></div><div class="job-meta">${channel} · 下次 ${escapeHtml(formatDateTime(firstDefined(job.next_run_at, job.next_run)))}</div><div class="job-meta"><span class="status-badge ${badgeClass}">${statusLabel}</span>　${escapeHtml(firstDefined(job.last_duration_ms !== undefined ? `${job.last_duration_ms} ms` : null, formatDateTime(job.last_run_at), "尚未运行"))}</div><div class="job-actions"><button class="switch ${enabled ? "on" : ""}" type="button" data-job-toggle aria-label="${enabled ? "停用" : "启用"} ${escapeHtml(firstDefined(job.name, id))}" data-enabled="${enabled}"></button><button class="icon-button" type="button" data-job-run aria-label="立即运行" data-tooltip="立即运行"><i data-lucide="play"></i></button></div></div>`;
    }).join("")}</div>`;
    initIcons();
  }

  async function toggleJob(row, button) {
    const id = row.dataset.jobId;
    const enabled = button.dataset.enabled !== "true";
    button.disabled = true;
    try {
      await api(`/api/jobs/${encodeURIComponent(id)}`, { method: "PATCH", body: { enabled } });
      const job = state.jobs.find((item) => String(firstDefined(item.id, item.job_id, item.name)) === String(id));
      if (job) job.enabled = enabled;
      renderJobs(state.jobs);
      showToast("success", enabled ? "任务已启用" : "任务已停用", id);
    } catch (error) {
      button.disabled = false;
      showToast("error", "任务更新失败", error.message);
    }
  }

  async function runJob(row, button) {
    const id = row.dataset.jobId;
    setButtonBusy(button, true, "");
    try {
      await api(`/api/jobs/${encodeURIComponent(id)}/run`, { method: "POST", body: {} });
      showToast("success", "任务已触发", id);
      window.setTimeout(() => loadJobs(true), 800);
    } catch (error) {
      showToast("error", "任务运行失败", error.message);
    } finally {
      setButtonBusy(button, false);
    }
  }

  async function sendBotCommand(command) {
    const resultElement = $("#bot-command-result");
    setState(resultElement, "loading", "正在执行命令", "", null, true);
    try {
      const data = await api("/api/bot/command", { method: "POST", body: { command } });
      const text = firstDefined(data.response, data.message, data.content, data.text, JSON.stringify(data));
      const delivery = firstDefined(data.delivery, {});
      const sent = Boolean(delivery.sent);
      const deliveryReason = firstDefined(delivery.reason, sent ? "企业微信已接收消息" : "企业微信未发送");
      resultElement.innerHTML = `<div class="message-preview"><div class="message-bubble"><div class="message-title"><i data-lucide="bot"></i><span>机器人响应</span><span class="status-badge ${sent ? "bullish" : "bearish"}">${sent ? "已投递" : "未投递"}</span></div><div class="message-body">${escapeHtml(text)}</div><div class="message-delivery ${sent ? "success" : "error"}">${escapeHtml(deliveryReason)}</div></div></div>`;
      showToast(sent ? "success" : "warning", sent ? "命令已执行并投递" : "命令已执行，投递失败", deliveryReason);
      initIcons();
    } catch (error) {
      setState(resultElement, "error", "命令执行失败", error.message, null, true);
    }
  }

  async function testMessage(channel, content) {
    const resultElement = $("#message-test-result");
    setState(resultElement, "loading", "正在发送测试消息", "", null, true);
    try {
      const data = await api("/api/messages/test", { method: "POST", body: { channel, content } });
      const sent = firstDefined(data.sent, false);
      resultElement.innerHTML = `<div class="diagnosis-item"><strong>${sent ? "测试消息已发送" : "消息预览已生成"}</strong><span>${escapeHtml(firstDefined(data.reason, data.message, data.status, data.delivery_id, sent ? "渠道返回成功" : "尚未配置实际通道"))}</span></div>`;
      showToast(sent ? "success" : "warning", sent ? "消息测试成功" : "未实际发送", firstDefined(data.reason, channel));
    } catch (error) {
      setState(resultElement, "error", "测试消息发送失败", error.message, null, true);
    }
  }

  async function loadSettings(force = false) {
    if (state.loaded.has("settings") && !force) {
      renderSettings(state.settings || {}, state.sources);
      return;
    }
    setState("#data-sources", "loading", "正在检测数据源", "", null);
    try {
      const [settings, sourceData] = await Promise.all([
        api("/api/settings"),
        api("/api/data-sources").catch((error) => ({ error: error.message, sources: [] }))
      ]);
      state.settings = settings || {};
      state.sources = normalizeSources(sourceData);
      state.loaded.add("settings");
      renderSettings(state.settings, state.sources, sourceData.error);
      setUpdatedAt(new Date());
    } catch (error) {
      setState("#data-sources", "error", "设置加载失败", error.message, "settings");
      showToast("error", "设置加载失败", error.message);
    }
  }

  function renderSettings(settings, sources, sourceError) {
    const form = $("#settings-form");
    const values = firstDefined(settings.settings, settings.config, settings, {});
    const rulebookThreshold = firstDefined(values.rulebook_threshold, values.technical_threshold, 62);
    ["rulebook_threshold", "rulebook_push_threshold", "auction_threshold", "scan_limit", "strategy_version"].forEach((name) => {
      const input = form.elements[name];
      if (input) input.value = firstDefined(
        name === "rulebook_threshold" ? rulebookThreshold : values[name],
        name === "rulebook_push_threshold" ? 72 : name === "auction_threshold" ? 60 : name === "scan_limit" ? 80 : DEFAULT_RULEBOOK_VERSION
      );
    });
    if (form.elements.technical_threshold) form.elements.technical_threshold.value = rulebookThreshold;
    if (form.elements.auto_scheduler) form.elements.auto_scheduler.checked = Boolean(firstDefined(values.auto_scheduler, true));
    if (form.elements.wecom_webhook) form.elements.wecom_webhook.value = "";
    if (form.elements.clear_wecom_webhook) {
      form.elements.clear_wecom_webhook.checked = false;
      form.elements.clear_wecom_webhook.disabled = !Boolean(values.wecom_webhook);
    }
    renderSources(sources, sourceError);
  }

  function renderSources(sources, error) {
    const element = $("#data-sources");
    if (error && !sources.length) {
      setState(element, "error", "数据源检测失败", error, "settings", true);
      return;
    }
    if (!sources.length) {
      element.innerHTML = stateMarkup("empty", "暂无数据源状态", "后端未返回健康检查结果", null, true);
      return;
    }
    element.innerHTML = `<div class="source-list">${sources.map((source) => {
      const healthy = firstDefined(source.healthy, source.ok, String(source.status).toLowerCase() === "ok", true);
      const status = String(firstDefined(source.status, healthy ? "ok" : "error")).toLowerCase();
      const degraded = Boolean(source.degraded) || status === "degraded" || status === "warning";
      const tone = degraded ? "warn" : healthy ? "good" : "bad";
      const statusLabel = degraded ? "降级" : healthy ? "正常" : "异常";
      return `<div class="source-row"><div class="source-main"><span class="status-dot ${tone}"></span><span><strong>${escapeHtml(firstDefined(source.name, source.source, source.id, "数据源"))}</strong><small>${escapeHtml(firstDefined(source.description, source.endpoint, source.message, "行情与基础数据"))}</small></span></div><span><strong>${escapeHtml(firstDefined(source.latency_ms !== undefined ? `${source.latency_ms} ms` : null, statusLabel))}</strong><small>${escapeHtml(formatDateTime(firstDefined(source.updated_at, source.checked_at)))}</small></span></div>`;
    }).join("")}</div>`;
  }

  function normalizeSources(payload) {
    const raw = firstDefined(payload && payload.sources, payload && payload.items, []);
    if (Array.isArray(raw)) return raw;
    if (raw && typeof raw === "object") {
      return Object.entries(raw).map(([name, value]) => ({
        name,
        ...(value && typeof value === "object" ? value : { status: value })
      }));
    }
    return [];
  }

  async function saveSettings() {
    const button = $("#settings-save");
    const form = $("#settings-form");
    const formData = new FormData(form);
    const payload = Object.fromEntries(formData.entries());
    payload.rulebook_threshold = toNumber(payload.rulebook_threshold, 62);
    payload.rulebook_push_threshold = toNumber(payload.rulebook_push_threshold, 72);
    if (payload.rulebook_push_threshold < payload.rulebook_threshold) {
      showToast("warning", "设置未保存", "规则库推送门槛不能低于规则库综合门槛");
      form.elements.rulebook_push_threshold.focus();
      return;
    }
    // The legacy field is constrained to -12..12; only mirror it when valid.
    // New servers consume rulebook_threshold, while old servers safely retain
    // their existing technical threshold instead of rejecting a 0..100 value.
    const legacyThreshold = toNumber(payload.technical_threshold, NaN);
    if (Number.isFinite(legacyThreshold) && legacyThreshold >= -12 && legacyThreshold <= 12 && payload.rulebook_threshold <= 12) {
      payload.technical_threshold = payload.rulebook_threshold;
    } else {
      delete payload.technical_threshold;
    }
    payload.auction_threshold = toNumber(payload.auction_threshold, 60);
    payload.scan_limit = toNumber(payload.scan_limit, 80);
    payload.auto_scheduler = form.elements.auto_scheduler.checked;
    payload.clear_wecom_webhook = form.elements.clear_wecom_webhook.checked;
    if (!payload.wecom_webhook) delete payload.wecom_webhook;
    setButtonBusy(button, true, "保存中");
    try {
      let data;
      try {
        data = await api("/api/settings", { method: "PUT", body: { values: payload } });
      } catch (error) {
        // A strict legacy API may reject the new field; retry with the compatible payload.
        if (/rulebook_threshold|422|validation|extra/i.test(error.message || "")) {
          const legacyPayload = { ...payload };
          delete legacyPayload.rulebook_threshold;
          data = await api("/api/settings", { method: "PUT", body: { values: legacyPayload } });
          showToast("warning", "已保存兼容配置", "当前服务端未识别 rulebook_threshold，已同步旧门槛字段");
        } else {
          throw error;
        }
      }
      state.settings = data || payload;
      renderSettings(state.settings, state.sources);
      showToast("success", "设置已保存", "新配置已提交到服务端");
    } catch (error) {
      showToast("error", "设置保存失败", error.message);
    } finally {
      setButtonBusy(button, false);
    }
  }

  function renderRotation(selector, metaSelector, payload) {
    const rows = asArray(firstDefined(payload.top, payload.boards, []));
    const meta = $(metaSelector);
    meta.textContent = payload.history_available ? "10 \u65e5\u6570\u636e\u5df2\u8986\u76d6" : "\u5f53\u65e5\u5f3a\u5ea6\uff08\u65e0\u5386\u53f2\uff09";
    if (!rows.length) {
      setState(selector, "empty", "\u6682\u65e0\u677f\u5757\u6570\u636e", "\u8bf7\u5237\u65b0\u91cd\u8bd5", "intel", true);
      return;
    }
    $(selector).innerHTML = `<div class="rotation-list">${rows.slice(0, 10).map((row) => {
      const change = toNumber(row.change_pct, 0);
      return `<div class="rotation-row"><span class="rotation-rank">${formatNumber(row.rank)}</span><strong>${escapeHtml(row.name)}</strong><span class="rotation-state">${escapeHtml(row.stage || row.state || "\u89c2\u5bdf")}</span><span class="${changeClass(change)}">${formatPercent(change)}</span><span class="rotation-score">${formatNumber(row.strength, 1)}</span></div>`;
    }).join("")}</div>`;
  }

  function renderMarketNews(rows) {
    const element = $("#market-news");
    if (!rows.length) {
      setState("#market-news", "empty", "\u6682\u65e0\u65b0\u95fb\u6570\u636e", "\u8bf7\u7a0d\u540e\u5237\u65b0", "intel", true);
      return;
    }
    element.innerHTML = rows.map((item) => {
      const title = escapeHtml(item.title || "--");
      const href = String(item.url || "").startsWith("http") ? escapeHtml(item.url) : "";
      const eventTags = asArray(item.event_tags).map((tag) => `<span class="tag event">${escapeHtml(tag)}</span>`).join("") || `<span class="tag empty">\u672a\u8bc6\u522b\u4e8b\u4ef6\u9898\u6750</span>`;
      const boards = asArray(item.board_tags).map((board) => `<span class="tag board">${escapeHtml(board.name)} <small>${escapeHtml(board.stage || "")}</small></span>`).join("") || `<span class="tag empty">\u5f85\u677f\u5757\u6838\u9a8c</span>`;
      const stocks = asArray(item.stock_tags).map((stock) => `<span class="tag stock">${escapeHtml(stock.name)} <small>${escapeHtml(stock.code || "")}</small></span>`).join("") || `<span class="tag empty">\u672a\u8bc6\u522b\u5b9e\u4f53\u4e2a\u80a1</span>`;
      const sourceName = String(item.source || "").includes("tencent") ? "\u817e\u8baf\u65b0\u95fb" : String(item.source || "").includes("eastmoney") ? "\u4e1c\u65b9\u8d22\u5bcc" : "FFD \u65b0\u95fb";
      const titleMarkup = href ? `<a href="${href}" target="_blank" rel="noopener noreferrer">${title}<i data-lucide="arrow-up-right"></i></a>` : `<div class="news-title">${title}</div>`;
      return `<article class="news-item"><div class="news-item-meta"><span>${escapeHtml(item.publisher || sourceName)}</span><time>${escapeHtml(item.time || "--")}</time></div>${titleMarkup}<p>${escapeHtml(item.content || "")}</p><div class="news-mapping"><div><b>\u4e8b\u4ef6\u9898\u6750</b><span>${eventTags}</span></div><div><b>\u5173\u8054\u677f\u5757</b><span>${boards}</span></div><div><b>\u5173\u8054\u4e2a\u80a1</b><span>${stocks}</span></div></div><small class="mapping-notice">${escapeHtml(item.mapping_notice || "")}</small></article>`;
    }).join("");
  }

  function renderRotationMatrix(matrix) {
    const columns = asArray(matrix.columns);
    if (!columns.length) {
      setState("#rotation-matrix", "empty", "\u901a\u8fbe\u4fe1\u672c\u5730\u677f\u5757\u5386\u53f2\u6570\u636e\u4e0d\u8db3", "\u9700\u81f3\u5c11\u4e24\u4e2a\u4ea4\u6613\u65e5", "intel", true);
      return;
    }
    $("#rotation-matrix-meta").textContent = `${columns.length} \u4e2a\u4ea4\u6613\u65e5 | \u6bcf\u65e5\u524d\u540e ${formatNumber(matrix.top_n)} \u540d`;
    $("#rotation-matrix").innerHTML = `<div class="matrix-scroll"><div class="matrix-grid">${columns.map((column) => `<section class="matrix-day"><h3>${escapeHtml(String(column.date).slice(4, 6))}/${escapeHtml(String(column.date).slice(6, 8))}</h3><div class="matrix-side top"><b>\u6da8\u5e45\u524d\u5341</b>${asArray(column.top).map((item, index) => `<div><span>${index + 1}</span><strong>${escapeHtml(item.name)}</strong><em class="up">${formatPercent(item.change_pct)}</em></div>`).join("")}</div><div class="matrix-side bottom"><b>\u8dcc\u5e45\u524d\u5341</b>${asArray(column.bottom).map((item, index) => `<div><span>${index + 1}</span><strong>${escapeHtml(item.name)}</strong><em class="down">${formatPercent(item.change_pct)}</em></div>`).join("")}</div></section>`).join("")}</div></div>`;
    const list = (rows) => asArray(rows).slice(0, 5).map((item) => `<li><strong>${escapeHtml(item.name)}</strong><span>${formatPercent(item.return_10d)} | ${formatPercent(item.max_daily_pct)} / ${formatPercent(item.min_daily_pct)}</span></li>`).join("") || `<li>--</li>`;
    $("#rotation-summary").innerHTML = `<section><h3>\u5341\u65e5\u6da8\u5e45\u6700\u5927</h3><ol>${list(matrix.leaders)}</ol></section><section><h3>\u5341\u65e5\u8dcc\u5e45\u6700\u5927</h3><ol>${list(matrix.laggards)}</ol></section><section class="offensive"><h3>\u8fdb\u653b\u677f\u5757</h3><ol>${list(matrix.offensive)}</ol></section><section class="defensive"><h3>\u9632\u5fa1\u677f\u5757</h3><ol>${list(matrix.defensive)}</ol></section>`;
  }

  async function loadIntel(force = false) {
    if (state.loaded.has("intel") && !force) return;
    setState("#market-news", "loading", "\u6b63\u5728\u52a0\u8f7d\u65b0\u95fb", "", null, true);
    setState("#industry-rotation", "loading", "\u6b63\u5728\u52a0\u8f7d\u884c\u4e1a\u8f6e\u52a8", "", null, true);
    setState("#concept-rotation", "loading", "\u6b63\u5728\u52a0\u8f7d\u6982\u5ff5\u8f6e\u52a8", "", null, true);
    try {
      const [newsResult, industryResult, conceptResult, matrixResult] = await Promise.allSettled([
        api("/api/news/market?limit=30"),
        api("/api/boards/industry/rotation?top_n=10"),
        api("/api/boards/concept/rotation?top_n=10"),
        api("/api/boards/all/matrix?days=10&top_n=10")
      ]);
      const news = newsResult.status === "fulfilled" ? newsResult.value : { rows: [], count: 0 };
      const industry = industryResult.status === "fulfilled" ? industryResult.value : {};
      const concept = conceptResult.status === "fulfilled" ? conceptResult.value : {};
      const matrix = matrixResult.status === "fulfilled" ? matrixResult.value : {};
      state.intel = { news, industry, concept, matrix };
      state.loaded.add("intel");
      const rows = asArray(news.rows);
      renderMarketNews(rows);
      const sourceValue = String(news.source || "");
      const newsSource = sourceValue.includes("ffd") ? "FFD \u4e3b\u6e90" : sourceValue.includes("tencent") ? "\u817e\u8baf\u5907\u7528" : "\u4e1c\u8d22\u5907\u7528";
      $("#intel-news-meta").textContent = `${newsSource} | ${formatNumber(news.count || rows.length)} \u6761 | ${formatDateTime(news.as_of)}`;
      renderRotation("#industry-rotation", "#industry-rotation-meta", industry || {});
      renderRotation("#concept-rotation", "#concept-rotation-meta", concept || {});
      renderRotationMatrix(matrix || {});
      setUpdatedAt(firstDefined(news.as_of, new Date()));
      initIcons();
    } catch (error) {
      setState("#market-news", "error", "\u65b0\u95fb\u52a0\u8f7d\u5931\u8d25", error.message, "intel", true);
      setState("#industry-rotation", "error", "\u884c\u4e1a\u8f6e\u52a8\u52a0\u8f7d\u5931\u8d25", error.message, "intel", true);
      setState("#concept-rotation", "error", "\u6982\u5ff5\u8f6e\u52a8\u52a0\u8f7d\u5931\u8d25", error.message, "intel", true);
      setState("#rotation-matrix", "error", "\u5341\u65e5\u8f6e\u52a8\u77e9\u9635\u52a0\u8f7d\u5931\u8d25", error.message, "intel", true);
      showToast("error", "\u65b0\u95fb\u4e0e\u677f\u5757\u52a0\u8f7d\u5931\u8d25", error.message);
    }
  }

  function loaders() {
    return {
      overview: loadOverview,
      intel: loadIntel,
      overnight: loadOvernight,
      screener: loadScreener,
      oversold: loadOversold,
      yijiner: loadYijiner,
      dixi: loadDixi,
      maifu: loadMaifu,
      stock: loadStock,
      review: loadReview,
      backtest: loadBacktests,
      automation: loadJobs,
      settings: loadSettings
    };
  }

  function navigate(view, updateHash = true, loadView = true) {
    if (!VIEWS.has(view)) view = "overview";
    state.view = view;
    $$('[data-view-page]').forEach((page) => page.classList.toggle("active", page.dataset.viewPage === view));
    $$('.nav-item[data-view], .mobile-nav [data-view]').forEach((item) => item.classList.toggle("active", item.dataset.view === view));
    closeSidebar();
    if (updateHash && window.location.hash !== `#${view}`) history.pushState(null, "", `#${view}`);
    const loader = loaders()[view];
    if (loader && loadView) loader(false);
    window.scrollTo({ top: 0, behavior: "instant" });
    initIcons();
  }

  function openSidebar() {
    $("#sidebar").classList.add("open");
    const backdrop = $("#sidebar-backdrop");
    backdrop.hidden = false;
    requestAnimationFrame(() => backdrop.removeAttribute("hidden"));
  }

  function closeSidebar() {
    $("#sidebar").classList.remove("open");
    $("#sidebar-backdrop").hidden = true;
  }

  async function refreshCurrent() {
    const button = $("#refresh-button");
    if (button.disabled) return;
    const refresh = loaders()[state.view];
    button.disabled = true;
    button.classList.add("busy");
    try {
      if (state.view === "stock" && state.stock.tab === "deep") await loadDeep(true);
      else if (refresh) await refresh(true);
      await loadHealth();
    } finally {
      button.disabled = false;
      button.classList.remove("busy");
    }
  }

  function debounce(fn, delay) {
    let timer;
    return (...args) => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => fn(...args), delay);
    };
  }

  function bindEvents() {
    $$('.nav-item[data-view], .mobile-nav [data-view]').forEach((button) => {
      button.addEventListener("click", () => navigate(button.dataset.view));
    });
    $("#menu-button").addEventListener("click", openSidebar);
    $("#mobile-more").addEventListener("click", openSidebar);
    $("#sidebar-close").addEventListener("click", closeSidebar);
    $("#sidebar-backdrop").addEventListener("click", closeSidebar);
    $("#refresh-button").addEventListener("click", refreshCurrent);
    $("#intel-refresh").addEventListener("click", () => loadIntel(true));
    $("#overview-run").addEventListener("click", runOverviewScreener);
    $("#screener-run").addEventListener("click", runScreener);
    $("#yijiner-run").addEventListener("click", runYijinerScan);
    $("#dixi-run").addEventListener("click", runDixiScan);
    $("#dixi-auction").addEventListener("click", loadDixiAuction);
    $("#maifu-run").addEventListener("click", () => loadMaifu(true));
    $("#maifu-news-refresh").addEventListener("click", () => loadMaifu(true));
    $$("#maifu-news-tabs button").forEach((button) => {
      button.addEventListener("click", () => {
        state.maifuNews.filter = button.dataset.maifuNewsFilter || "all";
        $$("#maifu-news-tabs button").forEach((item) => item.classList.toggle("active", item === button));
        renderMaifuNews(state.maifu || {});
      });
    });
    $("#maifu-news-search").addEventListener("input", debounce((event) => {
      state.maifuNews.keyword = event.target.value;
      renderMaifuNews(state.maifu || {});
    }, 120));
    $("#oversold-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $("#oversold-run");
      setButtonBusy(button, true, "筛选中");
      try {
        await loadOversold(true);
      } finally {
        setButtonBusy(button, false);
      }
    });
    $("#research-add-form").addEventListener("submit", addResearchStock);
    $("#research-batch-form").addEventListener("submit", importResearchBatch);
    $("#research-batch-text").addEventListener("input", previewResearchBatch);
    $("#research-refresh").addEventListener("click", () => loadOvernight(true));
    $("#research-analyze-all").addEventListener("click", analyzeAllResearch);
    $("#backtest-run").addEventListener("click", runBacktest);
    $("#settings-save").addEventListener("click", saveSettings);
    $("#sources-refresh").addEventListener("click", () => loadSettings(true));

    $("#global-search").addEventListener("submit", (event) => {
      event.preventDefault();
      const value = $("#global-stock-code").value.trim();
      const code = value.match(/\d{6}/)?.[0];
      if (!code) {
        showToast("warning", "请输入股票代码", "当前版本使用 6 位 A 股代码查询");
        return;
      }
      openStock(code);
    });

    $("#stock-loader-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const code = $("#stock-code").value.trim().match(/\d{6}/)?.[0];
      if (!code) {
        showToast("warning", "代码格式不正确", "请输入 6 位 A 股代码");
        return;
      }
      openStock(code);
    });

    $$("#screener-mode button").forEach((button) => {
      button.addEventListener("click", () => {
        state.screener.mode = button.dataset.mode;
        $$("#screener-mode button").forEach((item) => item.classList.toggle("active", item === button));
      });
    });

    $("#candidate-filter").addEventListener("input", debounce((event) => {
      state.screener.filter = event.target.value;
      renderScreenerRows(extractCandidates(state.screener.data || {}).map((item, index) => normalizeCandidate(item, index, state.screener.context || {})));
    }, 120));
    $("#candidate-signal-filter").addEventListener("change", (event) => {
      state.screener.signal = event.target.value;
      renderScreenerRows(extractCandidates(state.screener.data || {}).map((item, index) => normalizeCandidate(item, index, state.screener.context || {})));
    });

    $$("#stock-tabs button").forEach((button) => {
      button.addEventListener("click", () => {
        const tab = button.dataset.stockTab;
        state.stock.tab = tab;
        $$("#stock-tabs button").forEach((item) => item.classList.toggle("active", item === button));
        $("#stock-analysis-tab").hidden = tab !== "analysis";
        $("#stock-deep-tab").hidden = tab !== "deep";
        if (tab === "deep") loadDeep(false);
        else drawStockChart(state.chart.candles);
      });
    });

    $("#bot-command-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const command = $("#bot-command").value.trim();
      if (command) sendBotCommand(command);
    });
    $("#message-test-form").addEventListener("submit", (event) => {
      event.preventDefault();
      testMessage("wecom", $("#message-content").value.trim());
    });

    document.addEventListener("click", (event) => {
      const navTarget = event.target.closest("[data-nav-target]");
      if (navTarget) navigate(navTarget.dataset.navTarget);
      const retry = event.target.closest("[data-retry-view]");
      if (retry) {
        const loader = loaders()[retry.dataset.retryView];
        if (loader) loader(true);
      }
      const jobToggle = event.target.closest("[data-job-toggle]");
      if (jobToggle) toggleJob(jobToggle.closest("[data-job-id]"), jobToggle);
      const jobRun = event.target.closest("[data-job-run]");
      if (jobRun) runJob(jobRun.closest("[data-job-id]"), jobRun);
    });

    window.addEventListener("popstate", () => navigate(window.location.hash.slice(1).replace(/^\/+/, ""), false));
  }

  function initializeDates() {
    const today = new Date();
    $("#overview-date").value = localDate(today);
    $("#backtest-to").value = localDate(today);
    const start = new Date(today);
    start.setDate(start.getDate() - 30);
    $("#backtest-from").value = localDate(start);
  }

  function initializeTradingState() {
    const now = new Date();
    const minutes = now.getHours() * 60 + now.getMinutes();
    const weekday = now.getDay();
    const trading = weekday >= 1 && weekday <= 5 && ((minutes >= 570 && minutes <= 690) || (minutes >= 780 && minutes <= 900));
    const chip = $("#trading-chip");
    chip.innerHTML = `<span class="status-dot ${trading ? "good" : ""}"></span><span>${trading ? "交易中" : "非交易时段"}</span>`;
  }

  async function autoRefreshMarketData() {
    if (document.hidden || state.autoRefreshBusy || !["overview", "stock"].includes(state.view)) return;
    const now = new Date();
    const weekday = now.getDay();
    const minutes = now.getHours() * 60 + now.getMinutes();
    // Include auction and a short post-close settlement window. The server is
    // the authority for the exact session and data timestamp.
    const syncWindow = weekday >= 1 && weekday <= 5 && minutes >= 9 * 60 + 15 && minutes <= 15 * 60 + 10;
    if (!syncWindow) return;
    state.autoRefreshBusy = true;
    try {
      if (state.view === "overview") await loadOverview(true);
      else if (state.stock.tab === "analysis") await loadStock(true);
    } finally {
      state.autoRefreshBusy = false;
    }
  }

  function init() {
    initializeDates();
    initializeTradingState();
    bindEvents();
    setupChartInteractions();
    initIcons();
    loadHealth();
    const initialView = window.location.hash.slice(1).replace(/^\/+/, "");
    navigate(VIEWS.has(initialView) ? initialView : "overview", false);
    window.setInterval(autoRefreshMarketData, 30000);
    window.setInterval(refreshScreenerAuction, 3000);
    // Recover the sidebar status after a transient startup/network timeout,
    // including outside trading hours when market auto-refresh is disabled.
    window.setInterval(loadHealth, 30000);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
