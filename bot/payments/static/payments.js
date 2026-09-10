"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const page = document.body.dataset.page;
  const state = { user: null, products: [], orders: [], terms: null, kind: "register", selected: null, editing: null, review: null, code: null, poll: null, refreshing: false };
  const labels = { pending: "待支付", paid: "已支付", expired: "已过期", issued: "已发码", claimed: "兑换处理中", redeemed: "已兑换", held: "暂停使用", review: "待核查", fulfilled: "已发码", failed: "待处理" };
  const errors = {
    unauthorized: "请先通过 Telegram 登录。", login_required: "请先通过 Telegram 登录。",
    forbidden: "当前账号没有此操作权限。", csrf_failed: "页面已失效，请刷新后重试。",
    sales_disabled: "套餐销售暂未开放，已有订单仍可查询。", payments_disabled: "套餐销售暂未开放，已有订单仍可查询。",
    invalid_config: "支付服务暂不可用，请稍后重试。", invalid_configuration: "支付服务暂不可用，请稍后重试。",
    product_changed: "套餐已更新，请关闭窗口并重新选择套餐。", terms_changed: "购买须知已更新，请重新确认。",
    terms_required: "请阅读并同意购买须知。", consent_required: "请阅读并同意购买须知。",
    not_found: "未找到对应记录。", sold_out: "该套餐已售罄，请选择其他套餐。", capacity_full: "注册名额已满。",
    no_capacity: "注册名额已满。", code_held: "该兑换码暂停使用，请联系管理员。", code_unavailable: "兑换码暂不可用，请稍后刷新。",
    invalid_product: "请检查套餐名称、时长、价格和销售上限。", checkout_failed: "暂时无法创建支付，请稍后重试。",
    gateway_unavailable: "支付服务暂不可用，请稍后重试。", challenge_expired: "登录请求已过期，请重新登录。",
    test_buyer_not_allowed: "当前为测试支付，仅允许配置的测试账号下单。",
    code_mode_mismatch: "兑换码不属于当前支付环境。",
    order_mode_mismatch: "订单不属于当前支付环境。",
    stripe_amount_too_small: "套餐金额低于 Stripe 允许的最低金额，请联系管理员调整价格。",
    stripe_credentials_invalid: "Stripe 密钥验证失败，请联系管理员检查支付配置。",
    stripe_permission_denied: "Stripe 拒绝了当前收款权限，请联系管理员核查。",
    service_unavailable: "暂时无法创建或查询付款，请稍后重试或联系管理员。",
  };
  const money = (fen) => new Intl.NumberFormat("zh-CN", { style: "currency", currency: "CNY" }).format(Number(fen || 0) / 100);
  const date = (value) => {
    if (!value) return "-";
    const input = typeof value === "string" && !/(Z|[+-]\d{2}:\d{2})$/.test(value) ? `${value}Z` : value;
    const parsed = new Date(input);
    return Number.isNaN(parsed.getTime()) ? "-" : new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(parsed);
  };
  const productOf = (order) => order.product || order.product_snapshot || {};
  const label = (value) => labels[value] || "处理中";
  const iconRefresh = () => window.lucide?.createIcons();
  const show = (element, visible = true) => { if (element) element.hidden = !visible; };
  const text = (element, value) => { if (element) element.textContent = value; };
  const node = (tag, value, className) => {
    const item = document.createElement(tag);
    if (value !== undefined) item.textContent = value;
    if (className) item.className = className;
    return item;
  };
  const icon = (name) => { const item = node("i"); item.dataset.lucide = name; return item; };
  function toast(message) { text($("toast"), message); show($("toast")); clearTimeout(state.toast); state.toast = setTimeout(() => show($("toast"), false), 4000); }
  function errorMessage(error) { return error.message || "请求失败，请稍后重试。"; }
  function displayError(id, error) { text($(id), errorMessage(error)); show($(id)); }
  async function api(path, options = {}) {
    const headers = { Accept: "application/json", ...options.headers };
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    if (options.method && options.method !== "GET" && state.user?.csrf_token) headers["X-CSRF-Token"] = state.user.csrf_token;
    const response = await fetch(`/payments${path}`, { ...options, headers, credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(25000) });
    let body;
    try { body = await response.json(); } catch (_) { body = {}; }
    if (!response.ok) {
      const code = typeof body.detail === "string" ? body.detail : body.detail?.code || body.code;
      const fallback = response.status === 401 ? "请先通过 Telegram 登录。" : response.status === 403 ? "访问被拒绝，请刷新页面后重试。" : response.status === 429 ? "操作过于频繁，请稍后重试。" : "请求失败，请稍后重试。";
      const error = new Error(errors[code] || (typeof code === "string" && /[\u4e00-\u9fff]/.test(code) ? code : fallback));
      error.status = response.status; error.code = code; throw error;
    }
    return body;
  }
  function safeExternal(value, provider) {
    try {
      const url = new URL(value);
      if (url.protocol !== "https:") return null;
      if (provider === "stripe" && url.hostname !== "checkout.stripe.com") return null;
      if (provider === "telegram" && url.hostname !== "t.me") return null;
      return url.href;
    } catch (_) { return null; }
  }
  function statusBadge(value, override) {
    const style = value === "paid" || value === "issued" || value === "redeemed" ? "success" : ["review", "held", "claimed", "pending"].includes(value) ? "warning" : "";
    return node("span", override || label(value), `badge ${style}`);
  }
  function actionButton(name, title, action) {
    const button = node("button", undefined, "icon-button"); button.type = "button"; button.title = title; button.setAttribute("aria-label", title); button.append(icon(name));
    button.addEventListener("click", async () => { button.disabled = true; try { await action(); } catch (error) { toast(errorMessage(error)); } finally { button.disabled = false; } });
    return button;
  }
  async function loadIdentity() {
    try { state.user = await api("/me"); if (!state.user.telegram_id) state.user = null; }
    catch (error) { if (error.status !== 401) throw error; state.user = null; }
    show($("login-button"), !state.user); show($("logout-button"), !!state.user); show($("identity"), !!state.user);
    text($("identity"), state.user ? `TG ${state.user.telegram_id}` : "");
    show($("admin-nav"), ["admin", "owner"].includes(state.user?.role));
  }
  async function startLogin() {
    clearTimeout(state.poll); show($("telegram-link"), false); show($("retry-login"), false); show($("login-code-wrap"), false);
    text($("login-status"), "正在创建登录请求");
    if (!$("login-dialog").open) $("login-dialog").showModal();
    try {
      const result = await api("/auth/start", { method: "POST", headers: { "X-Payment-Request": "1" } });
      const url = safeExternal(result.login_url, "telegram"); if (!url) throw new Error("登录链接不可用，请稍后重试。");
      $("telegram-link").href = url; show($("telegram-link")); text($("login-code"), result.display_code); show($("login-code-wrap"), !!result.display_code);
      text($("login-status"), "请在 Telegram 中核对确认码并确认登录。");
      const deadline = Date.now() + 5 * 60 * 1000;
      async function poll() {
        if (!$("login-dialog").open) return;
        try {
          const response = await api("/auth/poll", { headers: { "X-Payment-Request": "1" } });
          if (response.authenticated) { window.location.reload(); return; }
          if (Date.now() >= deadline || response.expired) throw new Error("登录请求已过期，请重新登录。");
          state.poll = setTimeout(poll, 2000);
        } catch (error) { text($("login-status"), errorMessage(error)); show($("retry-login")); show($("telegram-link"), false); }
      }
      state.poll = setTimeout(poll, 2000);
    } catch (error) { text($("login-status"), errorMessage(error)); show($("retry-login")); }
  }
  function requireLogin() {
    if (state.user) return true;
    $("page-status").replaceChildren(icon("lock-keyhole"), node("h2", "请先登录"), node("p", "通过 Telegram 确认身份后查看。"));
    const button = node("button", "Telegram 登录", "button primary"); button.type = "button"; button.addEventListener("click", startLogin); $("page-status").append(button); show($("page-status")); iconRefresh(); return false;
  }
  async function loadShop() {
    const result = await api("/products"); state.products = result.products || []; state.terms = result.terms;
    text($("terms-text"), result.terms?.text || "购买须知暂不可用，请稍后再试。");
    show($("shop-view")); renderProducts();
  }
  function renderProducts() {
    const tier = $("tier-filter").value;
    const products = state.products.filter((item) => item.active !== false && item.kind === state.kind && (tier === "all" || item.tier === tier));
    text($("product-section-title"), state.kind === "register" ? "注册套餐" : "续期套餐");
    $("products").replaceChildren(); show($("products-empty"), products.length === 0);
    products.forEach((product) => {
      const card = $("product-template").content.firstElementChild.cloneNode(true); card.dataset.tier = product.tier;
      text(card.querySelector(".product-title"), product.title); text(card.querySelector(".product-kind"), product.kind === "register" ? "新账号 · 注册码" : "已有账号 · 续期码");
      const tierBadge = card.querySelector(".product-tier"); text(tierBadge, product.tier === "vip" ? "VIP" : "普通"); tierBadge.classList.add(product.tier === "vip" ? "vip" : "normal");
      text(card.querySelector(".price strong"), money(product.price_fen)); text(card.querySelector(".product-months"), product.months);
      text(card.querySelector(".product-access"), product.tier === "vip" ? "包含 VIP 线路权益" : "普通线路权益");
      text(card.querySelector(".product-period"), product.kind === "register" ? "注册成功后开始计时" : "按顺序追加套餐周期");
      const buy = card.querySelector(".product-buy"); buy.disabled = !state.terms?.version || Number(product.price_fen) <= 0;
      buy.addEventListener("click", () => selectProduct(product)); $("products").append(card);
    }); iconRefresh();
  }
  function selectProduct(product) {
    if (!state.user) { startLogin(); return; }
    state.selected = product; $("accept-terms").checked = false; $("pay-button").disabled = true; show($("checkout-error"), false);
    text($("checkout-product"), product.title); text($("checkout-price"), money(product.price_fen));
    text($("checkout-description"), `${product.kind === "register" ? "注册码" : "续期码"} · ${product.tier === "vip" ? "VIP" : "普通"} · ${product.months} 个月 · 1 份`);
    text($("checkout-terms"), state.terms.text); $("checkout-dialog").showModal();
  }
  async function checkout() {
    if (!$("accept-terms").checked || !state.selected || !state.terms) return;
    const button = $("pay-button"); button.disabled = true; show($("checkout-error"), false);
    try {
      const result = await api("/checkout", { method: "POST", body: JSON.stringify({ product_id: state.selected.id, product_version: state.selected.version, terms_version: state.terms.version, accepted: true }) });
      const url = safeExternal(result.checkout_url, "stripe"); if (!url) throw new Error("支付链接暂不可用，请在我的订单中查看结果。");
      window.location.assign(url);
    } catch (error) {
      displayError("checkout-error", error);
      if (["product_changed", "terms_changed"].includes(error.code)) { $("accept-terms").checked = false; state.selected = null; await loadShop().catch(() => {}); }
      button.disabled = !$("accept-terms").checked || !state.selected;
    }
  }
  function filteredOrders(admin) {
    const prefix = admin ? "admin-order" : "order";
    const query = $(`${prefix}-search`).value.trim().toLowerCase(); const filter = $(`${prefix}-filter`).value;
    return state.orders.filter((order) => (!query || `${order.id} ${productOf(order).title} ${admin ? order.buyer_tg : ""}`.toLowerCase().includes(query)) && (filter === "all" || (filter === "review" ? order.review_required : order.payment_state === filter)));
  }
  function renderOrders(admin = false) {
    const orders = filteredOrders(admin); const tbody = $(admin ? "admin-orders-body" : "orders-body"); tbody.replaceChildren();
    for (const order of orders) {
      const product = productOf(order); const row = node("tr"); const identity = node("td");
      if (admin) { identity.append(node("span", order.id, "mono"), node("span", `TG ${order.buyer_tg}`, "secondary-line")); }
      else { const link = node("a", product.title || "会员套餐", "order-link"); link.href = `/payments/order/${encodeURIComponent(order.id)}`; identity.append(link, node("span", order.id, "secondary-line mono")); }
      row.append(identity);
      if (admin) row.append(node("td", product.title || "会员套餐"));
      row.append(node("td", money(order.amount_fen)));
      const payment = node("td"); payment.append(statusBadge(order.review_required ? "review" : order.payment_state)); row.append(payment);
      if (!admin) { const fulfillment = node("td"); fulfillment.append(statusBadge(order.fulfillment_state, order.fulfillment_state === "issued" ? "已发码" : "待发码")); row.append(fulfillment); }
      row.append(node("td", date(order.created_at))); const action = node("td");
      if (admin) {
        const actions = node("div", undefined, "row-actions");
        actions.append(actionButton("refresh-cw", "重新对账", async () => { await api(`/admin/orders/${encodeURIComponent(order.id)}/reconcile`, { method: "POST" }); toast("已提交对账任务"); }));
        if (order.payment_state === "paid") actions.append(actionButton("send", "补发原码", async () => { await api(`/admin/orders/${encodeURIComponent(order.id)}/resend`, { method: "POST" }); toast("已提交原码补发任务"); }));
        if (state.user?.role === "owner") actions.append(actionButton("clipboard-check", "记录核查结论", () => { state.review = order; $("review-form").reset(); text($("review-order-id"), order.id); show($("review-error"), false); $("review-dialog").showModal(); }));
        action.append(actions);
      } else { const link = node("a", undefined, "icon-button"); link.href = `/payments/order/${encodeURIComponent(order.id)}`; link.title = "查看订单"; link.setAttribute("aria-label", "查看订单"); link.append(icon("arrow-right")); action.append(link); }
      row.append(action); tbody.append(row);
    }
    show($(admin ? "admin-orders-empty" : "orders-empty"), orders.length === 0);
    if (!admin) text($("orders-count"), `共 ${orders.length} 笔订单`);
    iconRefresh();
  }
  async function loadOrders() { if (!requireLogin()) return; state.orders = (await api("/orders")).orders || []; show($("orders-view")); renderOrders(); }
  async function loadOrder() {
    if (!requireLogin()) return;
    const order = await api(`/orders/${encodeURIComponent(document.body.dataset.orderId)}`); const product = productOf(order);
    state.order = order; text($("detail-title"), product.title || "会员套餐"); text($("detail-id"), order.id); text($("detail-amount"), money(order.amount_fen));
    const badge = statusBadge(order.review_required ? "review" : order.payment_state); badge.id = "detail-status"; $("detail-status").replaceWith(badge);
    const facts = [["套餐用途", product.kind === "register" ? "注册新账号" : "续期已有账号"], ["线路等级", product.tier === "vip" ? "VIP" : "普通"], ["套餐时长", `${product.months} 个月`], ["创建时间", date(order.created_at)], ["发码状态", order.fulfillment_state === "issued" ? "已发码" : "待发码"], ["兑换状态", order.code_state ? label(order.code_state) : "尚未发码"]];
    if (order.redeemed_at) facts.push(["兑换时间", date(order.redeemed_at)]);
    $("order-facts").replaceChildren(...facts.map(([key, value]) => { const group = node("div"); group.append(node("dt", key), node("dd", value)); return group; }));
    const restricted = order.refunded || ["held", "review"].includes(order.code_state);
    let message = "";
    if (restricted) message = "该订单需要核查，兑换码暂不可用。请联系管理员。";
    else if (order.review_required) message = "该订单需要人工核查，请联系管理员确认处理进度。";
    else if (order.code_state === "redeemed") message = "此兑换码已使用，不能再次兑换。";
    else if (order.payment_state === "paid" && order.fulfillment_state !== "issued") message = "付款已确认，兑换码正在发放。请稍后刷新查看。";
    else if (order.payment_state === "pending") message = "付款结果以实际到账为准。已付款的订单请稍后刷新，无需重复付款。";
    else if (order.payment_state === "expired") message = "该支付订单已过期。已完成付款但状态未更新时，请联系管理员核查。";
    text($("order-message"), message); show($("order-message"), !!message);
    show($("code-section"), order.fulfillment_state === "issued" && !restricted);
    if (restricted) { state.code = null; text($("code-value"), "•••• •••• •••• ••••"); $("copy-code").disabled = true; }
    const checkoutUrl = safeExternal(order.checkout_url, "stripe");
    const canPay = order.payment_state === "pending" && checkoutUrl && (!order.expires_timestamp || order.expires_timestamp * 1000 > Date.now());
    show($("continue-payment"), !!canPay); if (canPay) $("continue-payment").href = checkoutUrl;
    show($("order-view")); iconRefresh();
  }
  async function revealCode() {
    $("reveal-code").disabled = true;
    try { const result = await api(`/orders/${encodeURIComponent(document.body.dataset.orderId)}/code`); if (!result.code) throw new Error("兑换码正在发放，请稍后刷新。"); state.code = result.code; text($("code-value"), result.code); $("copy-code").disabled = false; }
    catch (error) { toast(errorMessage(error)); } finally { $("reveal-code").disabled = false; }
  }
  async function loadAdmin() {
    if (!requireLogin()) return;
    if (!["admin", "owner"].includes(state.user.role)) throw new Error("当前账号没有销售管理权限。");
    const [orders, products] = await Promise.all([api("/admin/orders"), api("/admin/products")]);
    state.orders = orders.orders || []; state.products = products.products || [];
    text($("metric-orders"), state.orders.length); text($("metric-paid"), money(state.orders.filter((order) => order.payment_state === "paid").reduce((sum, order) => sum + Number(order.amount_fen), 0)));
    text($("metric-review"), state.orders.filter((order) => order.review_required).length); show($("new-product"), state.user.role === "owner");
    show($("admin-view")); renderOrders(true); renderAdminProducts();
  }
  function renderAdminProducts() {
    $("admin-products-body").replaceChildren();
    for (const product of state.products) {
      const row = node("tr"); row.append(node("td", product.title), node("td", `${product.kind === "register" ? "注册" : "续期"} / ${product.tier === "vip" ? "VIP" : "普通"}`), node("td", `${product.months} 个月`), node("td", money(product.price_fen)), node("td", product.sales_limit ?? "不限"));
      const active = node("td"); active.append(node("span", product.active ? "已上架" : "草稿 / 已下架", `badge ${product.active ? "success" : ""}`)); row.append(active);
      const actions = node("td"); if (state.user.role === "owner") actions.append(actionButton("pencil", "编辑套餐", () => editProduct(product))); row.append(actions); $("admin-products-body").append(row);
    }
    show($("admin-products-empty"), state.products.length === 0); iconRefresh();
  }
  function editProduct(product) {
    state.editing = product || null; const form = $("product-form"); form.reset();
    text($("product-dialog-title"), product ? "编辑套餐" : "新增套餐"); show($("product-error"), false);
    for (const field of ["title", "kind", "tier", "months"]) if (product?.[field] !== undefined) form.elements[field].value = product[field];
    form.elements.price.value = product ? (product.price_fen / 100).toFixed(2) : ""; form.elements.sales_limit.value = product?.sales_limit ?? ""; form.elements.active.checked = product?.active || false;
    $("product-dialog").showModal();
  }
  async function saveProduct(event) {
    event.preventDefault(); const form = event.currentTarget; if (!form.reportValidity()) return;
    const button = form.querySelector("button[type=submit]"); button.disabled = true; show($("product-error"), false);
    try {
      const fields = form.elements; const data = { title: fields.title.value.trim(), kind: fields.kind.value, tier: fields.tier.value, months: Number(fields.months.value), price_fen: Math.round(Number(fields.price.value) * 100), sales_limit: fields.sales_limit.value ? Number(fields.sales_limit.value) : null, active: fields.active.checked };
      if (state.editing) Object.assign(data, { id: state.editing.id, version: state.editing.version });
      await api("/admin/products", { method: "POST", body: JSON.stringify(data) }); $("product-dialog").close(); await loadAdmin(); toast("套餐已保存");
    } catch (error) { displayError("product-error", error); } finally { button.disabled = false; }
  }
  async function saveReview(event) {
    event.preventDefault(); const form = event.currentTarget; if (!form.reportValidity() || !state.review) return;
    const button = form.querySelector("button[type=submit]"); button.disabled = true;
    try { await api(`/admin/orders/${encodeURIComponent(state.review.id)}/review`, { method: "POST", body: JSON.stringify({ note: form.elements.note.value.trim() }) }); $("review-dialog").close(); await loadAdmin(); toast("核查结论已记录"); }
    catch (error) { displayError("review-error", error); } finally { button.disabled = false; }
  }
  async function load() {
    if (state.refreshing) return;
    state.refreshing = true; $("refresh-button").disabled = true; show($("page-error"), false);
    for (const id of ["orders-view", "order-view", "admin-view"]) show($(id), false);
    try {
      await loadIdentity(); show($("page-status"), false);
      if (page === "shop") await loadShop(); else if (page === "orders") await loadOrders(); else if (page === "order") await loadOrder(); else if (page === "admin") await loadAdmin();
    } catch (error) { show($("page-status"), false); displayError("page-error", error); }
    finally { state.refreshing = false; $("refresh-button").disabled = false; iconRefresh(); }
  }
  $("theme-toggle").addEventListener("click", () => { const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = theme; try { localStorage.setItem("dusheng-payment-theme", theme); } catch (_) {} });
  $("login-button").addEventListener("click", startLogin); $("retry-login").addEventListener("click", startLogin); $("refresh-button").addEventListener("click", load);
  $("logout-button").addEventListener("click", async () => { try { await api("/auth/logout", { method: "POST" }); window.location.assign("/payments/shop"); } catch (error) { toast(errorMessage(error)); } });
  document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
  $("login-dialog").addEventListener("close", () => clearTimeout(state.poll));
  $("accept-terms").addEventListener("change", () => { $("pay-button").disabled = !$("accept-terms").checked || !state.selected; });
  $("pay-button").addEventListener("click", checkout);
  document.querySelectorAll("[data-kind]").forEach((button) => button.addEventListener("click", () => { state.kind = button.dataset.kind; document.querySelectorAll("[data-kind]").forEach((tab) => tab.setAttribute("aria-selected", String(tab === button))); renderProducts(); }));
  $("tier-filter")?.addEventListener("change", renderProducts);
  for (const id of ["order-search", "order-filter", "admin-order-search", "admin-order-filter"]) $(id)?.addEventListener(id.endsWith("search") ? "input" : "change", () => renderOrders(page === "admin"));
  $("reveal-code")?.addEventListener("click", revealCode);
  $("copy-code")?.addEventListener("click", async () => { if (!state.code) return; try { await navigator.clipboard.writeText(state.code); toast("兑换码已复制"); } catch (_) { toast("复制失败，请选中兑换码后手动复制。"); } });
  document.querySelectorAll("[data-admin-tab]").forEach((button) => button.addEventListener("click", () => { document.querySelectorAll("[data-admin-tab]").forEach((tab) => tab.setAttribute("aria-selected", String(tab === button))); show($("admin-orders-panel"), button.dataset.adminTab === "orders"); show($("admin-products-panel"), button.dataset.adminTab === "products"); }));
  $("new-product")?.addEventListener("click", () => editProduct(null)); $("product-form").addEventListener("submit", saveProduct); $("review-form").addEventListener("submit", saveReview);
  window.addEventListener("pageshow", (event) => { if (event.persisted) window.location.reload(); });
  iconRefresh(); load();
})();
