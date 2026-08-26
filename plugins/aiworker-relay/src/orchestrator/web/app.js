(function () {
  "use strict";
  const page = document.body.dataset.page || "overview";
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (v) =>
    String(v ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const json = (v) => {
    try {
      return JSON.stringify(v);
    } catch (_) {
      return "";
    }
  };
  const fmtDate = (v) => {
    if (!v) return "暂无";
    const d = new Date(v);
    return Number.isNaN(d.getTime())
      ? esc(v)
      : d.toLocaleString("zh-CN", {
          month: "numeric",
          day: "numeric",
          hour: "2-digit",
          minute: "2-digit",
        });
  };
  const fmtCredit = (v) => {
    const n = Number(v);
    return Number.isFinite(n)
      ? n.toLocaleString("en-US", { maximumFractionDigits: 4 })
      : "暂无";
  };
  const stateText = {
    enabled: "已启用",
    frozen: "已冻结",
    unverified: "未验证",
    verified: "已验证",
    running: "运行中",
    starting: "启动中",
    stopping: "结束中",
    succeeded: "已完成",
    failed: "失败",
    stopped: "已停止",
    stopped_forced: "已强制停止",
    unavailable: "不可用",
    created: "已创建",
    waiting_connection: "等待连接",
    waiting_verification: "等待首次验证",
    ready: "已准备好",
  };
  const status = (v, kind = "") =>
    `<span class="status status-${esc(kind || v || "unknown")}">${esc(stateText[v] || v || "暂无")}</span>`;
  const nav = [
    ["index.html", "概览", "overview"],
    ["workers.html", "Workers", "workers"],
    ["runs.html", "运行记录", "runs"],
    ["usage.html", "用量", "usage"],
    ["settings.html", "设置", "settings"],
  ];
  function shell(title, sub) {
    const links = nav
      .map(
        ([href, label, key]) =>
          `<li><a class="nav-link" href="${href}" ${page === key ? 'aria-current="page"' : ""}><span>${label}</span></a></li>`,
      )
      .join("");
    return `<div class="shell"><aside class="sidebar"><a class="brand" href="index.html"><span class="brand-mark">AR</span><span>AIworker Relay</span></a><p class="nav-label">工作台</p><ul class="nav-list">${links}</ul><div class="sidebar-foot">本地控制面<strong>由 Codex 管理</strong></div></aside><div class="main"><header class="mobile-bar"><a class="mobile-brand" href="index.html"><span class="brand-mark">AR</span><span class="mobile-brand-label">AIworker Relay</span></a><nav class="mobile-nav">${nav
      .map(
        ([h, l, k]) =>
          `<a href="${h}" ${page === k ? 'aria-current="page"' : ""}>${l}</a>`,
      )
      .join(
        "",
      )}</nav></header><main class="page"><div class="topline"><div><p class="crumb">AIworker Relay</p><h1>${title}</h1><p class="page-subtitle">${sub}</p></div><div class="top-actions" data-top-actions></div></div><div id="content"></div></main></div></div>`;
  }
  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok)
      throw Object.assign(new Error(body.message || "请求失败"), {
        body,
        status: r.status,
      });
    return body;
  }
  function card(title, body, cls = "card card-pad") {
    return `<section class="${cls}"><h2>${title}</h2>${body}</section>`;
  }
  async function readOpenRouterKeyStatus() {
    try {
      return await api("/api/openrouter-key");
    } catch (_) {
      return { configured: null };
    }
  }
  function profileReadiness(p, keyStatus) {
    if (keyStatus.configured === false)
      return status("waiting_connection", "waiting");
    if (p.state === "frozen") return status("frozen");
    if (p.verification !== "verified")
      return status("waiting_verification", "waiting");
    return status("ready", "enabled");
  }
  function profileStatusChips(p, keyStatus) {
    const readiness = profileReadiness(p, keyStatus);
    const availability =
      p.state === "frozen" && keyStatus.configured !== false
        ? ""
        : status(p.state);
    return `${readiness}${availability}${status(p.verification)}`;
  }
  function profileCard(p, keyStatus) {
    return `<article class="worker-card"><div class="worker-top"><span class="worker-mark">${esc((p.display_name || p.model).slice(0, 2).toUpperCase())}</span><div class="worker-statuses">${profileStatusChips(p, keyStatus)}</div></div><h2 class="worker-name">${esc(p.display_name || p.model)}</h2><p class="worker-kind">OpenRouter · 外部 worker</p><div class="worker-detail"><span>推理偏好</span><strong>${esc(p.default_reasoning || "auto")}</strong></div><div class="worker-foot"><a class="text-link" href="worker.html?id=${encodeURIComponent(p.id)}">查看详情</a><button class="button button-secondary button-small" data-toggle-profile="${esc(p.id)}" data-state="${esc(p.state)}">${p.state === "frozen" ? "激活" : "冻结"}</button></div></article>`;
  }
  function nativeCards(workers) {
    return (workers || [])
      .map(
        (worker) =>
          `<article class="worker-card"><div class="worker-top"><span class="worker-mark native">${esc(worker.badge || (worker.display_name || worker.id).slice(0, 2).toUpperCase())}</span><span class="section-note">由 Codex 管理</span></div><h2 class="worker-name">${esc(worker.display_name || worker.id)}</h2><p class="worker-kind">原生子代理</p><div class="worker-detail"><span>状态来源</span><strong>Codex</strong></div></article>`,
      )
      .join("");
  }

  function renderUnavailable() {
    $("#content").innerHTML =
      `<section class="section card empty"><strong>本地控制面暂不可连接</strong><span>暂时无法读取 Worker 或运行记录。</span><div style="margin-top:16px"><button class="button" data-retry>重试连接</button></div></section>`;
  }
  function runRow(r) {
    return `<div class="list-row"><div><h3 class="row-title"><a class="text-link" href="run.html?id=${encodeURIComponent(r.run_id)}">${esc(r.model)}</a></h3><div class="row-meta"><span>${esc(r.reasoning_effort || "auto")}</span><span>${esc(r.run_id.slice(0, 10))}</span>${status(r.status)}</div></div><span class="row-time">${fmtDate(r.updated_at)}</span></div>`;
  }
  function overviewNextStep(profiles, keyStatus) {
    if (!profiles.length) {
      return `<aside class="quiet-card"><h2>添加第一个 Worker</h2><p>先粘贴模型名并确认目录信息。连接 OpenRouter 可以随后完成。</p><a class="button" href="add-worker.html">添加 Worker</a></aside>`;
    }
    const profile = profiles[0];
    if (keyStatus.configured === false) {
      return `<aside class="quiet-card"><h2>Worker 已保存，等待连接</h2><p>本机还没有连接 OpenRouter，暂时不能派发新任务。</p><a class="button" href="settings.html?profile=${encodeURIComponent(profile.id)}">配置 Key</a></aside>`;
    }
    if (keyStatus.configured === true) {
      const selected =
        profiles.find((item) => item.state === "enabled" && item.verification === "verified") ||
        profiles.find((item) => item.state === "enabled") ||
        profile;
      const verified = selected.verification === "verified" && selected.state === "enabled";
      return `<aside class="quiet-card"><h2>${verified ? "已准备好在 Codex 中使用" : "连接已就绪，等待首次验证"}</h2><p>现在回到 Codex，描述任务并明确指定 ${esc(selected.display_name || selected.model)}。${verified ? "" : "该档案需要由你明确选择一次实验性任务。"}</p><a class="button" href="worker.html?id=${encodeURIComponent(selected.id)}">查看 Worker</a></aside>`;
    }
    return `<aside class="quiet-card"><h2>查看 Worker 连接状态</h2><p>暂时无法确认本机 OpenRouter 连接，请在设置页检查。</p><a class="button" href="settings.html?profile=${encodeURIComponent(profile.id)}">打开设置</a></aside>`;
  }
  async function renderOverview(o) {
    const keyStatus = await readOpenRouterKeyStatus();
    const active = (o.runs || []).filter((r) =>
      ["starting", "running", "stopping"].includes(r.status),
    );
    const profileCards = o.profiles.length
      ? o.profiles
          .slice(0, 2)
          .map((profile) => profileCard(profile, keyStatus))
          .join("")
      : `<div class="empty card" style="grid-column:1/-1">还没有外部 Worker</div>`;
    $("#content").innerHTML =
      `<section class="section"><div class="metric-grid"><div class="metric"><span class="metric-label">已配置 Worker</span><strong class="metric-value">${o.profiles.length}</strong><span class="metric-foot">外部模型档案</span></div><div class="metric"><span class="metric-label">当前运行</span><strong class="metric-value">${active.length}</strong><span class="metric-foot">仅统计本地可观测任务</span></div><div class="metric"><span class="metric-label">实际费用</span><strong class="metric-value">待归因</strong><span class="metric-foot">暂未能可靠关联</span></div></div></section><section class="section grid-two">${card("正在运行", active.length ? `<div class="list-card">${active.map(runRow).join("")}</div>` : `<div class="empty"><strong>当前没有运行中的外部任务</strong><span>任务由 Codex 派发后会出现在这里。</span></div>`)}${overviewNextStep(o.profiles, keyStatus)}</section><section class="section"><div class="section-head"><h2 class="section-title">Worker</h2><a class="text-link" href="workers.html">查看全部</a></div><div class="worker-grid">${profileCards}${nativeCards(o.native_workers)}</div></section>`;
  }
  async function renderWorkers(o) {
    const keyStatus = await readOpenRouterKeyStatus();
    $("#content").innerHTML =
      `<section class="section"><div class="section-head"><h2 class="section-title">外部 Worker</h2><a class="button button-small" href="add-worker.html">添加 Worker</a></div><div class="worker-grid">${o.profiles.map((profile) => profileCard(profile, keyStatus)).join("") || `<div class="empty card" style="grid-column:1/-1"><strong>还没有外部 Worker</strong><span>粘贴 OpenRouter 模型名即可开始配置。</span></div>`}</div></section><section class="section"><div class="section-head"><h2 class="section-title">原生子代理</h2><p class="section-note">状态由 Codex 管理</p></div><div class="worker-grid">${nativeCards(o.native_workers)}</div></section>`;
  }
  function runTable(runs) {
    return runs.length
      ? `<div class="card list-card">${runs.map(runRow).join("")}</div>`
      : `<div class="card empty"><strong>还没有外部运行记录</strong><span>回到 Codex，描述任务并明确指定一个已连接的 Worker；任务被派发后会显示在这里。</span></div>`;
  }
  function renderRuns(o) {
    if (!o.runs.length) {
      $("#content").innerHTML = `<section class="section">${runTable([])}</section>`;
      return;
    }
    $("#content").innerHTML =
      `<section class="section"><div class="filter-row"><div class="filter-group"><button class="filter" aria-pressed="true" data-run-filter="all">全部</button><button class="filter" aria-pressed="false" data-run-filter="active">运行中</button><button class="filter" aria-pressed="false" data-run-filter="done">已结束</button></div><span class="section-note">${o.runs.length} 条记录</span></div><div class="section" id="run-list">${runTable(o.runs)}</div></section>`;
    document.addEventListener("click", (e) => {
      const b = e.target.closest("[data-run-filter]");
      if (!b) return;
      document
        .querySelectorAll("[data-run-filter]")
        .forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      const mode = b.dataset.runFilter;
      const rs = o.runs.filter(
        (r) =>
          mode === "all" ||
          (mode === "active" &&
            ["starting", "running", "stopping"].includes(r.status)) ||
          (mode === "done" &&
            !["starting", "running", "stopping"].includes(r.status)),
      );
      $("#run-list").innerHTML = runTable(rs);
    });
  }
  function accountSummaryMarkup(value) {
    const refresh =
      '<div style="margin-top:16px"><button class="button button-secondary button-small" data-load-account>刷新账户信息</button></div>';
    if (value.status === "account_balance") {
      return `<div class="data-pair"><span>可用余额</span><strong>${fmtCredit(value.remaining_credits)} credits</strong></div><div class="data-pair"><span>累计购买</span><strong>${fmtCredit(value.total_credits)} credits</strong></div><div class="data-pair"><span>累计使用</span><strong>${fmtCredit(value.total_usage)} credits</strong></div><div class="reference-foot">账户总额 · 更新于 ${fmtDate(value.refreshed_at)}</div>${refresh}`;
    }
    if (value.status === "key_limit") {
      return `<div class="data-pair"><span>当前 Key 可用额度</span><strong>${fmtCredit(value.limit_remaining)} credits</strong></div><div class="data-pair"><span>当前 Key 总限额</span><strong>${fmtCredit(value.limit)} credits</strong></div><div class="data-pair"><span>当前 Key 已用</span><strong>${fmtCredit(value.usage)} credits</strong></div><div class="reference-foot">这是当前 Key 的限额${value.limit_reset ? ` · 重置周期：${esc(value.limit_reset)}` : ""}，不是账户总余额。</div>${refresh}`;
    }
    if (value.status === "management_key_required") {
      return `<div class="empty"><strong>账户总余额需要管理 Key</strong><span>当前 Key 仍可用于 Worker 调用；OpenRouter 没有提供可展示的 Key 限额。</span></div>${refresh}`;
    }
    if (value.status === "missing_key") {
      return `<div class="empty"><strong>尚未配置 OpenRouter Key</strong><span>请先到设置页保存 Key。</span></div>${refresh}`;
    }
    return `<div class="empty"><strong>暂时无法读取账户信息</strong><span>${esc(value.message || "请稍后再试。")}</span></div>${refresh}`;
  }

  function renderUsage(o) {
    const active = o.runs.filter((r) => r.status === "running").length;
    $("#content").innerHTML =
      `<section class="section"><div class="metric-grid"><div class="metric"><span class="metric-label">本月实际费用</span><strong class="metric-value">待归因</strong><span class="metric-foot">尚未建立可靠费用关联</span></div><div class="metric"><span class="metric-label">已记录运行</span><strong class="metric-value">${o.runs.length}</strong><span class="metric-foot">本地证据记录</span></div><div class="metric"><span class="metric-label">实时活跃</span><strong class="metric-value">${active}</strong><span class="metric-foot">外部进程</span></div></div></section><section class="section grid-two">${card("OpenRouter 账户", '<div id="account-result" class="empty"><strong>账户信息尚未读取</strong><span>只在你点击时读取。当前保存的 Key 具有管理权限时显示账户总额；否则只显示 OpenRouter 返回的当前 Key 限额。</span><div style="margin-top:16px"><button class="button button-secondary button-small" data-load-account>刷新账户信息</button></div></div>')}${card("费用口径", `<p>目录价格只用于比较模型，不代表本地实际花费。当前运行的实际费用状态为“待归因”或“暂不可用”，不会用估算值替代。</p>`)}</section><section class="section">${card("按 Worker 查看", o.profiles.length ? `<div class="list-card">${o.profiles.map((p) => `<div class="list-row"><div><strong>${esc(p.display_name || p.model)}</strong><div class="row-meta">${esc(p.model)}</div></div><span class="row-time">待归因</span></div>`).join("")}</div>` : `<div class="empty">添加 Worker 后，这里会按档案汇总。</div>`)}</section>`;
  }
  async function renderSettings(o) {
    let k;
    try {
      k = await api("/api/openrouter-key");
    } catch (err) {
      $("#content").innerHTML =
        `<section class="section card empty"><strong>本地控制面暂不可连接</strong><span>${esc(err.message)}</span><div style="margin-top:16px"><button class="button" data-retry>重试连接</button></div></section>`;
      return;
    }
    const requestedProfile = new URLSearchParams(location.search).get("profile");
    const profile =
      o.profiles.find((item) => item.id === requestedProfile) || o.profiles[0];
    const nextHref = profile
      ? `worker.html?id=${encodeURIComponent(profile.id)}`
      : "add-worker.html";
    const nextLabel = profile ? "查看已保存 Worker" : "添加 Worker";
    $("#content").innerHTML =
      `<section class="section profile-layout"><div class="card card-pad"><h2>OpenRouter Key</h2><p>密钥只保存在本机钥匙串中，不会回显到页面。</p><form class="form-grid" id="key-form"><div class="field"><label for="key">API Key</label><input id="key" name="key" type="password" autocomplete="off" placeholder="${k.configured ? "已配置，输入新 Key 可替换" : "sk-or-v1-…"}"><small>保存时会向 OpenRouter 验证有效性。具有管理权限的 Key 可在用量页读取账户总额；普通 Key 仍可用于 Worker 调用。</small></div><div class="field-actions"><button class="button" type="submit">保存 Key</button></div><div id="key-note" aria-live="polite"></div></form></div>${card("本地运行状态", `<div class="data-pair"><span>控制面</span><strong>由 Codex 启动</strong></div><div class="data-pair"><span>数据位置</span><strong>本机应用数据目录</strong></div>`)}</section>`;
    $("#key-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const note = $("#key-note");
      note.className = "notice";
      note.textContent = "正在验证…";
      try {
        await api("/api/openrouter-key", {
          method: "PUT",
          body: json({ key: e.target.key.value }),
        });
        note.className = "notice success";
        note.innerHTML = `Key 已保存。<a class="text-link" href="${nextHref}">${nextLabel}</a>`;
        e.target.reset();
      } catch (err) {
        note.className = "notice error";
        note.textContent = err.message;
      }
    });
  }
  function normalizeModel(value) {
    try {
      const u = new URL(value);
      if (u.hostname !== "openrouter.ai" && u.hostname !== "www.openrouter.ai")
        return value.trim();
      const p = u.pathname.split("/").filter(Boolean);
      return p.length >= 2 && !["docs", "api"].includes(p[0])
        ? p.slice(0, 2).join("/")
        : value.trim();
    } catch (_) {
      return value.trim();
    }
  }
  function reasoningOptions(model) {
    const r = model && model.reasoning;
    if (!r || !Object.prototype.hasOwnProperty.call(r, "supported_efforts")) {
      return ["auto"];
    }
    if (Array.isArray(r.supported_efforts)) {
      const efforts = r.supported_efforts.filter(
        (x) => x && (!r.mandatory || x !== "none"),
      );
      return ["auto"].concat(efforts);
    }
    if (r.supported_efforts === null) {
      const gateway = ["max", "xhigh", "high", "medium", "low", "minimal"];
      if (!r.mandatory) gateway.push("none");
      return ["auto"].concat(gateway);
    }
    return ["auto"];
  }
  function reasoningOptionLabel(value) {
    if (value === "auto") return "auto · 由 Codex 为本次任务选择";
    return `${value} · 固定默认档位`;
  }
  async function renderAdd() {
    $("#content").innerHTML =
      `<section class="section profile-layout"><div class="card card-pad"><h2>添加外部 Worker</h2><p>粘贴 OpenRouter 模型名或模型页面链接。保存前会检查它是否存在。</p><form class="form-grid" id="add-form"><div class="field"><label for="model">模型</label><input id="model" required placeholder="例如 provider/model-name"><small>不会在这里配置 Key；Key 请到设置页保存。</small></div><button class="button button-secondary" type="button" id="lookup">查找模型</button><div id="matches"></div><div class="field"><label for="display">显示名称（可选）</label><input id="display" placeholder="默认使用模型名称"></div><div class="field"><label for="effort">默认推理偏好</label><select id="effort"><option value="auto">auto · 由 Codex 为本次任务选择</option></select><small>固定档位会按 Profile 设置派发；只有 auto 允许 Codex 在模型支持范围内选择。</small></div><div class="field"><label for="initial-state">初始档案状态</label><select id="initial-state"><option value="enabled">启用</option><option value="frozen">冻结</option></select><small>冻结的 Worker 不会被派发。</small></div><div class="field-actions"><button class="button" type="submit">保存 Worker</button></div><div id="add-note" aria-live="polite"></div></form></div>${card("添加后", `<div class="data-pair"><span>初始验证</span><strong>未验证</strong></div><div class="data-pair"><span>使用方式</span><strong>由 Codex 明确派发</strong></div>`)}</section>`;
    let selected = null;
    $("#lookup").addEventListener("click", async () => {
      const note = $("#matches"),
        q = normalizeModel($("#model").value);
      if (!q) {
        note.innerHTML = '<div class="notice error">请输入模型名或链接。</div>';
        return;
      }
      note.innerHTML = '<div class="notice">正在查找…</div>';
      try {
        const data = await api("/api/models?query=" + encodeURIComponent(q));
        const ms = (data.models || []).filter(
          (m) =>
            m.id === q ||
            !q.includes("/") ||
            m.id.toLowerCase().includes(q.toLowerCase()),
        );
        if (!ms.length) {
          note.innerHTML =
            '<div class="notice error">没有找到这个 OpenRouter 模型。</div>';
          return;
        }
        note.innerHTML = `<div class="field"><label for="match">选择精确模型</label><select id="match">${ms.map((m) => `<option value="${esc(m.id)}">${esc(m.name || m.id)} · ${esc(m.id)}</option>`).join("")}</select></div>`;
        const choose = () => {
          selected = ms.find((m) => m.id === $("#match").value) || ms[0];
          $("#model").value = selected.id;
          $("#effort").innerHTML = reasoningOptions(selected)
            .map(
              (x) =>
                `<option value="${esc(x)}">${esc(reasoningOptionLabel(x))}</option>`,
            )
            .join("");
        };
        $("#match").addEventListener("change", choose);
        choose();
      } catch (err) {
        note.innerHTML = `<div class="notice error">${esc(err.message)}</div>`;
      }
    });
    $("#add-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const note = $("#add-note"),
        model = normalizeModel($("#model").value);
      note.className = "notice";
      note.textContent = "正在保存…";
      try {
        const p = await api("/api/profiles", {
          method: "POST",
          body: json({
            model,
            display_name: $("#display").value || undefined,
            state: $("#initial-state").value,
            default_reasoning: $("#effort").value,
          }),
        });
        location.href = "worker.html?id=" + encodeURIComponent(p.id);
      } catch (err) {
        note.className = "notice error";
        note.textContent = err.message;
      }
    });
  }
  function parameterText(c) {
    const architecture =
      c.architecture && typeof c.architecture === "object" ? c.architecture : {};
    const value =
      c.parameter_count ??
      c.parameters ??
      architecture.parameter_count ??
      architecture.parameters;
    if (value === null || value === undefined || value === "") return "目录未提供";
    const n = Number(value);
    if (!Number.isFinite(n)) return String(value);
    if (n >= 1_000_000_000) {
      return `${(n / 1_000_000_000).toLocaleString("en-US", {
        maximumFractionDigits: 1,
      })}B`;
    }
    if (n >= 1_000_000) {
      return `${(n / 1_000_000).toLocaleString("en-US", {
        maximumFractionDigits: 1,
      })}M`;
    }
    return n.toLocaleString("en-US");
  }

  function benchmarkSource(source) {
    return (
      {
        "artificial-analysis": "Artificial Analysis",
        "design-arena": "Design Arena",
        openrouter: "OpenRouter 评测",
      }[source] || source || "公开基准"
    );
  }

  function benchmarkType(value) {
    return (
      {
        gpqa_diamond: "GPQA Diamond",
        tau_bench_verified_airline: "TAU-Bench 航空",
      }[value] || String(value || "公开参考").replaceAll("_", " ")
    );
  }

  function numericMetric(value) {
    const n = Number(value);
    return value !== null && value !== "" && Number.isFinite(n) ? n : null;
  }

  function benchmarkMetrics(entry, asOf) {
    const source = benchmarkSource(entry.source);
    const at = entry.last_run_timestamp || asOf;
    const note = `${source}${at ? ` · ${fmtDate(at)}` : ""}`;
    const metrics = [];
    [
      ["coding_index", "编码指数"],
      ["agentic_index", "Agentic 指数"],
      ["intelligence_index", "智能指数"],
    ].forEach(([field, label]) => {
      const value = numericMetric(entry[field]);
      if (value !== null) metrics.push({ label, value: String(value), note });
    });
    const accuracy = numericMetric(entry.accuracy);
    if (accuracy !== null) {
      const percentage = accuracy <= 1 ? accuracy * 100 : accuracy;
      metrics.push({
        label: benchmarkType(entry.benchmark_type),
        value: `${percentage.toLocaleString("en-US", {
          maximumFractionDigits: 1,
        })}%`,
        note: `${note}${entry.total_tasks ? ` · ${entry.total_tasks} 项` : ""}`,
      });
    }
    if (!metrics.length) {
      [
        ["rating", "评分"],
        ["elo", "Elo"],
        ["score", "分数"],
      ].some(([field, label]) => {
        const value = numericMetric(entry[field]);
        if (value === null) return false;
        metrics.push({ label, value: String(value), note });
        return true;
      });
    }
    return metrics;
  }

  function benchmarkMarkup(data) {
    const entries = Array.isArray(data.entries) ? data.entries : [];
    const asOf = data.meta && data.meta.as_of;
    const metrics = entries.flatMap((entry) => benchmarkMetrics(entry, asOf));
    const refresh = `<div style="margin-top:16px"><button class="button button-secondary button-small" data-load-benchmarks="${esc(data.profile_id)}">刷新公开跑分</button></div>`;
    if (!entries.length) {
      return `<div class="empty"><strong>暂无公开参考分数</strong><span>OpenRouter 目前没有与这个精确模型标识匹配的 benchmark 记录。</span></div><div class="reference-foot">查询于 ${fmtDate(data.refreshed_at)}</div>${refresh}`;
    }
    if (!metrics.length) {
      return `<div class="empty"><strong>已收录公开记录</strong><span>当前来源没有返回可展示的性能指标。</span></div><div class="reference-foot">查询于 ${fmtDate(data.refreshed_at)}</div>${refresh}`;
    }
    return `<div class="reference-grid">${metrics.map((metric) => `<div class="reference-metric"><span class="reference-label">${esc(metric.label)}</span><strong class="reference-value">${esc(metric.value)}</strong><span class="reference-note">${esc(metric.note)}</span></div>`).join("")}</div><div class="reference-foot">精确模型：${esc(data.model)} · 查询于 ${fmtDate(data.refreshed_at)} · <a class="text-link" href="https://openrouter.ai/docs/api/api-reference/benchmarks/list-benchmarks" target="_blank" rel="noreferrer">查看来源</a></div>${refresh}`;
  }

  function workerNextStep(p, keyStatus) {
    const name = esc(p.display_name || p.model);
    if (keyStatus.configured === false) {
      return card(
        "下一步",
        `<p>Worker 已保存，等待连接 OpenRouter。没有已验证的 Key 时，Codex 不能派发新任务。</p><a class="button" href="settings.html?profile=${encodeURIComponent(p.id)}">配置 Key</a>`,
      );
    }
    if (p.state === "frozen") {
      return card(
        "下一步",
        `<p>这个 Worker 已冻结，不会接收新任务。需要使用时，先激活它，再回到 Codex 明确指定 ${name}。</p>`,
      );
    }
    if (p.verification !== "verified") {
      return card(
        "下一步",
        `<p>连接已就绪，等待首次验证。回到 Codex，描述任务并明确指定 ${name}；未验证档案只能由你明确选择进行实验性任务。</p>`,
      );
    }
    return card(
      "下一步",
      `<p>这个 Worker 已准备好在 Codex 中使用。回到 Codex，描述任务并明确指定 ${name}。</p>`,
    );
  }
  async function renderWorkerDetail(o) {
    const keyStatus = await readOpenRouterKeyStatus();
    const id = new URLSearchParams(location.search).get("id");
    const p =
      o.profiles.find((x) => x.id === id) ||
      o.profiles.find((x) => x.model === id);
    if (!p) {
      $("#content").innerHTML =
        '<section class="section card empty"><strong>找不到这个 Worker</strong><a class="text-link" href="workers.html">返回列表</a></section>';
      return;
    }
    const metadata = p.metadata || {},
      c = metadata.catalog || {},
      price = c.pricing || {},
      reasoning = c.reasoning || {};
    const pricing =
      price.prompt || price.completion
        ? `输入 ${esc(price.prompt || "暂无")} / 输出 ${esc(price.completion || "暂无")}`
        : "暂无目录价格";
    const policy =
      c.data_policy && typeof c.data_policy === "object"
        ? Object.entries(c.data_policy)
            .map(
              ([key, value]) =>
                `${key}: ${typeof value === "object" ? json(value) : value}`,
            )
            .join(" · ")
        : c.data_policy;
    $("#content").innerHTML =
      `<section class="section detail-layout"><div><div class="card profile-intro"><span class="profile-mark">${esc((p.display_name || p.model).slice(0, 2).toUpperCase())}</span><div><h2 class="profile-title">${esc(p.display_name || p.model)}</h2><p class="profile-kind">${esc(p.model)} · OpenRouter</p><div class="profile-statuses">${profileStatusChips(p, keyStatus)}</div></div></div><div class="section">${workerNextStep(p, keyStatus)}</div><div class="card section"><div class="reference-grid"><div class="reference-metric"><span class="reference-label">模型参数</span><strong class="reference-value">${esc(parameterText(c))}</strong><span class="reference-note">来自 OpenRouter 目录</span></div><div class="reference-metric"><span class="reference-label">上下文窗口</span><strong class="reference-value">${c.context_length ? esc(Number(c.context_length).toLocaleString()) : "暂无"}</strong><span class="reference-note">来自 OpenRouter 目录</span></div><div class="reference-metric"><span class="reference-label">目录价格</span><strong class="reference-value" style="font-size:16px">${pricing}</strong><span class="reference-note">不是实际花费</span></div><div class="reference-metric"><span class="reference-label">推理信息</span><strong class="reference-value" style="font-size:16px">${Array.isArray(reasoning.supported_efforts) ? esc(reasoning.supported_efforts.join(" · ")) : reasoning.supported_efforts === null ? "网关标准档位" : reasoning.mandatory ? "必须推理" : "暂无"}</strong><span class="reference-note">目录字段</span></div></div><div class="reference-foot">目录快照：${metadata.catalog_fetched_at ? fmtDate(metadata.catalog_fetched_at) : "旧档案未记录时间"}</div></div><div class="section">${card("隐私与数据保留", `<p>${esc(policy || "目录未提供可验证的数据保留说明")}</p>`)}</div></div><aside><div class="card profile-card"><h2>档案设置</h2><div class="data-pair"><span>默认推理</span><strong>${esc(p.default_reasoning || "auto")}</strong></div><div class="data-pair"><span>本机状态</span><strong>${status(p.state)}</strong></div><div class="data-pair"><span>兼容性</span><strong>${p.verification === "verified" ? "已验证" : "未验证"}</strong></div><button class="button button-secondary" style="width:100%;margin-top:13px" data-toggle-profile="${esc(p.id)}" data-state="${esc(p.state)}">${p.state === "frozen" ? "激活 Worker" : "冻结 Worker"}</button></div>${card("公开跑分参考", `<div id="benchmark-result" class="empty"><strong>公开数据尚未读取</strong><span>只在你点击时查询与此模型精确匹配的 OpenRouter benchmark 记录。</span><div style="margin-top:16px"><button class="button button-secondary button-small" data-load-benchmarks="${esc(p.id)}">加载公开跑分</button></div></div>`)}</aside></section>`;
  }
  function renderRunDetail(o) {
    const id = new URLSearchParams(location.search).get("id"),
      r = o.runs.find((x) => x.run_id === id);
    if (!r) {
      $("#content").innerHTML =
        '<section class="section card empty"><strong>找不到这条运行记录</strong><a class="text-link" href="runs.html">返回运行记录</a></section>';
      return;
    }
    const active = ["starting", "running", "stopping"].includes(r.status);
    const awaitingForce = r.stop_outcome === "awaiting_force";
    $("#content").innerHTML =
      `<section class="section detail-layout"><div class="card run-detail"><div>${status(r.status)}</div><h2 class="run-title">${esc(r.model)}</h2><p class="run-subtitle">${esc(r.run_id)}</p><div class="run-actions">${awaitingForce ? `<button class="button button-danger" data-force-run="${esc(r.run_id)}">确认强制终止</button>` : active ? `<button class="button button-danger" data-stop-run="${esc(r.run_id)}">温和停止</button>` : ""}<a class="button button-secondary" href="runs.html">返回记录</a></div></div><aside class="card profile-card"><h2>运行信息</h2>${[
        ["推理偏好", r.reasoning_effort],
        ["进程", r.pid],
        [
          "RSS 采样",
          r.rss_samples && r.rss_samples.length
            ? r.rss_samples.length + " 次"
            : "暂无",
        ],
        ["退出码", r.exit_code],
        ["工作区变更", r.dirty_workspace_excluded ? "已排除" : "暂无"],
        ["费用状态", r.cost_state],
        ["停止结果", r.stop_outcome],
        ["更新时间", fmtDate(r.updated_at)],
      ]
        .map(
          ([a, b]) =>
            `<div class="data-pair"><span>${a}</span><strong>${b === null || b === undefined || b === "" ? "暂无" : esc(b)}</strong></div>`,
        )
        .join("")}</aside></section><section class="section">${card(
        "产物",
        Object.keys(r.artifacts || {}).length
          ? Object.entries(r.artifacts)
              .map(
                ([k, v]) =>
                  `<div class="data-pair"><span>${esc(k)}</span><strong class="model-slug">${esc(v)}</strong></div>`,
              )
              .join("")
          : `<div class="empty">暂无产物记录。</div>`,
      )}</section><dialog class="dialog" id="stop-dialog"><div class="dialog-body"><h2>结束这个任务？</h2><p>先请求温和停止；如果进程没有退出，再由你确认强制终止。</p><div class="dialog-actions"><button class="button button-secondary" data-close-dialog>取消</button><button class="button button-danger" data-confirm-stop="${esc(r.run_id)}">温和停止</button></div></div></dialog>`;
  }
  async function boot() {
    document.body.innerHTML = shell(
      ...({
        overview: ["概览", "查看 Worker、运行状态与本地费用归因。"],
        workers: ["Workers", "管理外部模型档案与 Codex 原生子代理。"],
        runs: ["运行记录", "查看由 Codex 派发的外部任务及其证据。"],
        usage: ["用量", "区分目录价格与当前仍待归因的实际花费。"],
        settings: ["设置", "管理本地 OpenRouter 连接。"],
        add: ["添加 Worker", "从 OpenRouter 模型目录中选择一个外部模型。"],
        worker: ["Worker 详情", "查看模型目录信息、推理能力与本机档案状态。"],
        run: ["运行详情", "查看这次外部任务的状态、资源采样和产物。"],
      }[page] || ["概览", ""]),
    );
    let o;
    try {
      o = await api("/api/overview");
    } catch (err) {
      renderUnavailable();
      wire();
      return;
    }
    if (page === "overview") await renderOverview(o);
    if (page === "workers") await renderWorkers(o);
    if (page === "runs") renderRuns(o);
    if (page === "usage") renderUsage(o);
    if (page === "settings") await renderSettings(o);
    if (page === "add") renderAdd();
    if (page === "worker") await renderWorkerDetail(o);
    if (page === "run") renderRunDetail(o);
    wire();
    if (location.protocol !== "file:") connectEvents();
  }
  function wire() {
    document.addEventListener("click", async (e) => {
      const account = e.target.closest("[data-load-account]");
      if (account) {
        const result = $("#account-result");
        if (!result) return;
        account.disabled = true;
        result.innerHTML = '<div class="empty"><strong>正在读取账户信息</strong></div>';
        try {
          result.innerHTML = accountSummaryMarkup(
            await api("/api/openrouter-account"),
          );
        } catch (err) {
          result.innerHTML = `<div class="empty"><strong>暂时无法读取账户信息</strong><span>${esc(err.message)}</span></div><div style="margin-top:16px"><button class="button button-secondary button-small" data-load-account>重试</button></div>`;
        }
        return;
      }
      const benchmarks = e.target.closest("[data-load-benchmarks]");
      if (benchmarks) {
        const result = $("#benchmark-result");
        if (!result) return;
        const profileId = benchmarks.dataset.loadBenchmarks;
        benchmarks.disabled = true;
        result.innerHTML = '<div class="empty"><strong>正在读取公开跑分</strong></div>';
        try {
          result.innerHTML = benchmarkMarkup(
            await api(
              "/api/profiles/" +
                encodeURIComponent(profileId) +
                "/benchmarks",
            ),
          );
        } catch (err) {
          result.innerHTML = `<div class="empty"><strong>暂时无法读取公开跑分</strong><span>${esc(err.message)}</span></div><div style="margin-top:16px"><button class="button button-secondary button-small" data-load-benchmarks="${esc(profileId)}">重试</button></div>`;
        }
        return;
      }
      const toggle = e.target.closest("[data-toggle-profile]");
      if (toggle) {
        try {
          await api(
            "/api/profiles/" +
              encodeURIComponent(toggle.dataset.toggleProfile) +
              "/state",
            {
              method: "POST",
              body: json({
                state: toggle.dataset.state === "frozen" ? "enabled" : "frozen",
              }),
            },
          );
          location.reload();
        } catch (err) {
          alert(err.message);
        }
        return;
      }
      const close = e.target.closest("[data-close-dialog]");
      if (close) {
        close.closest("dialog")?.close();
        return;
      }
      const force = e.target.closest("[data-force-run]");
      if (force) {
        try {
          await api(
            "/api/runs/" + encodeURIComponent(force.dataset.forceRun) + "/stop",
            { method: "POST", body: json({ force: true }) },
          );
          location.reload();
        } catch (err) {
          alert(err.message);
        }
        return;
      }
      const stop = e.target.closest("[data-stop-run]");
      if (stop) {
        $("#stop-dialog")?.showModal();
        const confirm = $("[data-confirm-stop]");
        confirm.onclick = async () => {
          try {
            const r = await api(
              "/api/runs/" + encodeURIComponent(stop.dataset.stopRun) + "/stop",
              { method: "POST", body: json({ force: false }) },
            );
            if (r.stop_outcome === "awaiting_force") {
              confirm.textContent = "强制终止";
              confirm.dataset.force = "1";
              confirm.onclick = async () => {
                await api(
                  "/api/runs/" +
                    encodeURIComponent(stop.dataset.stopRun) +
                    "/stop",
                  { method: "POST", body: json({ force: true }) },
                );
                location.reload();
              };
            } else location.reload();
          } catch (err) {
            alert(err.message);
          }
        };
      }
      const retry = e.target.closest("[data-retry]");
      if (retry) location.reload();
    });
  }
  function connectEvents() {
    const s = new EventSource("/api/events");
    [
      "profile.updated",
      "run.created",
      "run.updated",
      "run.rss",
    ].forEach((n) =>
      s.addEventListener(n, () => {
        if (n !== "run.rss") location.reload();
      }),
    );
    s.onerror = () => s.close();
  }
  boot();
})();
