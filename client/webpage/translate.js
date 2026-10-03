// 一键汉化：网页侧提取与注入。
// 与 client/translate/extractor.py 使用同一套跳过规则（由 Python 侧通过 setRules 注入 skip_rules.json）。
(function () {
  "use strict";
  const STATE_KEY = "__bananaIndexTranslate";
  if (window[STATE_KEY]) { window[STATE_KEY].refreshRules(); return; }

  const state = {
    enabled: false,
    rules: null,
    units: [],          // {id, text, nodes:[Node], originalNodes:[string]}
    cache: new Map(),   // 原文 -> 译文
    wrappers: new WeakMap(), // 原文首节点 -> 注入的兄弟包裹节点（还原依据）
    observer: null,
    pending: false,
    lastError: "",
  };

  const SEPARATOR = " ";
  const wrapperTag = "font";

  function htmlToText(node) {
    return (node.textContent || "").replace(/\s+/g, " ").trim();
  }

  function isSkippedElement(el) {
    const rules = state.rules;
    if (!rules) { return false; }
    if (el.classList && el.classList.contains("banana-index-translation")) { return true; }
    if (el.tagName.toLowerCase() === wrapperTag && el.getAttribute("lang") === "zh-CN") { return true; }
    if (rules.skip_tags.indexOf(el.tagName.toLowerCase()) >= 0) { return true; }
    for (const attr of rules.skip_attributes) {
      const value = (el.getAttribute(attr) || "").trim().toLowerCase();
      if (value && rules.skip_attribute_values.indexOf(value) >= 0) { return true; }
      if (attr === "translate" && value === "no") { return true; }
    }
    const classes = ((el.className && el.className.toString()) || "") + " " + (el.id || "");
    const lowered = classes.toLowerCase();
    for (const token of rules.skip_class_substrings) {
      if (lowered.indexOf(token.toLowerCase()) >= 0) { return true; }
    }
    const role = (el.getAttribute("role") || "").toLowerCase();
    if (role && rules.skip_role.indexOf(role) >= 0) { return true; }
    if (el.hasAttribute("contenteditable")) { return true; }
    return false;
  }

  function isKeptVerbatim(text) {
    const rules = state.rules;
    if (!rules) { return false; }
    const value = text.trim();
    if (value.length < (rules.min_length || 2)) { return true; }
    const test = (patterns) => (patterns || []).some((pattern) => {
      try { return new RegExp(pattern).test(value); } catch (err) { return false; }
    });
    return test(rules.keep_patterns) || test(rules.code_like_heuristics);
  }

  function collectText(root, blocks, inline) {
    const parts = [];
    const nodes = [];
    const walk = (node) => {
      for (const child of node.childNodes) {
        if (child.nodeType === Node.TEXT_NODE) {
          parts.push(child.nodeValue);
          nodes.push(child);
        } else if (child.nodeType === Node.ELEMENT_NODE) {
          if (isSkippedElement(child)) { continue; }
          const tag = child.tagName.toLowerCase();
          if (blocks.has(tag)) { continue; }
          parts.push(" ");
          walk(child);
        }
      }
    };
    walk(root);
    return { text: parts.join("").replace(/\s+/g, " ").trim(), nodes };
  }

  function buildUnits() {
    const rules = state.rules;
    if (!rules) { return []; }
    const blocks = new Set(rules.block_tags.map((t) => t.toLowerCase()));
    const inline = new Set(rules.inline_tags.map((t) => t.toLowerCase()));
    const units = [];

    const visit = (el) => {
      if (!el || el.nodeType !== Node.ELEMENT_NODE || isSkippedElement(el)) { return; }
      if (blocks.has(el.tagName.toLowerCase())) {
        const collected = collectText(el, blocks, inline);
        if (collected.text && !isKeptVerbatim(collected.text)) {
          units.push({ id: "u" + units.length, text: collected.text, nodes: collected.nodes });
        }
      }
      for (const child of el.children) { visit(child); }
    };
    if (document.body) { visit(document.body); }
    if (document.title && !isKeptVerbatim(document.title)) {
      units.push({ id: "title", text: document.title, nodes: null, isTitle: true });
    }
    return units;
  }

  // 注入策略（照搬 kiss-translator 的安全做法）：不改写原文节点，
  // 而是把译文插入为原文首节点的兄弟节点，并把原文节点整体隐藏；
  // 还原时只需移除包裹节点并恢复原文显示，不存在破坏原始 DOM 的风险。
  function applyTranslation(unit, translated) {
    if (unit.isTitle) {
      if (unit.originalTitle === undefined) { unit.originalTitle = document.title; }
      document.title = translated;
      return;
    }
    if (!unit.nodes || !unit.nodes.length) { return; }
    const host = unit.nodes[0];
    if (!host || !host.parentNode || (host.isConnected === false)) { return; }
    if (!unit.originalNodes) {
      unit.originalNodes = unit.nodes.map((node) => node.nodeValue);
    }
    removeWrapperFor(unit);
    const wrapper = document.createElement(wrapperTag);
    wrapper.setAttribute("lang", "zh-CN");
    wrapper.className = "banana-index-translation";
    wrapper.setAttribute("translate", "no");
    wrapper.textContent = translated;
    host.parentNode.insertBefore(wrapper, host.nextSibling);
    unit.nodes.forEach((node) => { node.nodeValue = ""; });
    unit.hiddenNodes = unit.nodes.slice();
    state.wrappers.set(host, wrapper);
  }

  function removeWrapperFor(unit) {
    if (!unit.nodes || !unit.nodes.length) { return; }
    const host = unit.nodes[0];
    const wrapper = host ? state.wrappers.get(host) : null;
    if (wrapper && wrapper.parentNode) { wrapper.parentNode.removeChild(wrapper); }
    if (host) { state.wrappers.delete(host); }
  }

  function restoreUnit(unit) {
    if (unit.isTitle) {
      if (unit.originalTitle !== undefined) { document.title = unit.originalTitle; }
      return;
    }
    removeWrapperFor(unit);
    if (unit.originalNodes && unit.nodes) {
      unit.nodes.forEach((node, index) => {
        if (index < unit.originalNodes.length) { node.nodeValue = unit.originalNodes[index]; }
      });
    }
    unit.hiddenNodes = null;
  }

  // Python 侧完成翻译后调用该全局函数回调（QWebChannel 只支持返回值，不支持推送事件）
  const pendingRequests = new Map();
  let requestSeq = 0;

  window.__bananaIndexOnTranslations = function (requestId, payloadJson) {
    const entry = pendingRequests.get(requestId);
    if (!entry) { return; }
    pendingRequests.delete(requestId);
    clearTimeout(entry.timer);
    let payload = null;
    try { payload = JSON.parse(payloadJson); } catch (err) {
      entry.reject(new Error("翻译结果解析失败")); return;
    }
    if (payload && payload.error) { entry.reject(new Error(payload.error)); return; }
    entry.resolve((payload && payload.translations) || []);
  };

  function bridgeTranslate(items) {
    return new Promise((resolve, reject) => {
      if (!window.__bananaBridge) { reject(new Error("翻译通道未就绪")); return; }
      const requestId = "r" + (requestSeq += 1);
      const timer = setTimeout(() => {
        pendingRequests.delete(requestId);
        reject(new Error("翻译超时，请检查网络或密钥设置"));
      }, 180000);
      pendingRequests.set(requestId, { resolve, reject, timer });
      try {
        window.__bananaBridge.requestTranslations(requestId, JSON.stringify(items));
      } catch (err) {
        clearTimeout(timer);
        pendingRequests.delete(requestId);
        reject(err);
      }
    });
  }

  async function translatePage() {
    if (state.pending) { return { status: "busy" }; }
    state.pending = true;
    try {
      const units = buildUnits();
      state.units = units;
      const targets = [];
      const inFlight = new Map(); // 原文 -> [units]，合并同段落重复请求
      for (const unit of units) {
        if (state.cache.has(unit.text)) {
          applyTranslation(unit, state.cache.get(unit.text));
          continue;
        }
        if (inFlight.has(unit.text)) {
          inFlight.get(unit.text).push(unit);
        } else {
          inFlight.set(unit.text, [unit]);
          targets.push(unit);
        }
      }
      if (targets.length) {
        const payload = targets.map((unit) => ({ id: unit.id, text: unit.text }));
        const translated = await bridgeTranslate(payload);
        const byId = new Map();
        (translated || []).forEach((item) => {
          if (item && typeof item.id === "string" && typeof item.text === "string") {
            byId.set(item.id, item.text);
          }
        });
        targets.forEach((unit) => {
          const value = byId.get(unit.id);
          if (typeof value !== "string" || !value.trim()) { return; }
          state.cache.set(unit.text, value);
          (inFlight.get(unit.text) || [unit]).forEach((target) => {
            if (target.nodes && target.nodes[0] && target.nodes[0].isConnected !== false) {
              applyTranslation(target, value);
            }
          });
        });
      }
      state.enabled = true;
      state.lastError = "";
      return { status: "ok", units: units.length, translated: targets.length,
               cached: units.length - targets.length };
    } catch (err) {
      state.lastError = String(err && err.message ? err.message : err);
      return { status: "error", message: state.lastError };
    } finally {
      state.pending = false;
    }
  }

  function restorePage() {
    (state.units || []).forEach(restoreUnit);
    state.enabled = false;
    return { status: "restored", units: (state.units || []).length };
  }

  function refreshRules() { /* 规则由 Python 侧推送，见 setRules */ }

  window[STATE_KEY] = {
    setRules(json) { state.rules = JSON.parse(json); },
    refreshRules,
    translate: translatePage,
    restore: restorePage,
    status() {
      return { enabled: state.enabled, units: (state.units || []).length,
               cached: state.cache.size, error: state.lastError };
    },
    hasRules() { return !!state.rules; },
  };
})();
