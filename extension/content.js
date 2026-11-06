// Job Agent autofill: fills form fields you can review. It never clicks Submit/Apply.
// When YOU click Submit/Apply on a filled form, the application is logged to your tracker.
(() => {
  if (window.__jobAgentLoaded) return;
  window.__jobAgentLoaded = true;

  const SKIP = new Set(["password", "file", "hidden", "submit", "button", "image", "reset", "search"]);
  const APPLY_TEXT = /^(submit|apply|send)\b|submit application|apply now|send application|complete application/i;
  const NOT_APPLY = /\b(save|next|continue|back|cancel|sign in|log in|upload|add another)\b/i;
  const COLORS = {profile: "#16a34a", ai: "#d97706"};
  let host = null, toastTimer = null, filled = false;

  const clean = (t) => (t || "").replace(/\s+/g, " ").replace(/[*✱]/g, "").trim();
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);

  function labelFor(el) {
    if (el.labels && el.labels.length) return clean(el.labels[0].innerText);
    const by = el.getAttribute("aria-labelledby");
    if (by) {
      const text = by.split(/\s+/).map((id) => document.getElementById(id)?.innerText || "").join(" ");
      if (clean(text)) return clean(text);
    }
    if (el.getAttribute("aria-label")) return clean(el.getAttribute("aria-label"));
    const wrap = el.closest("label");
    if (wrap) return clean(wrap.innerText);
    return clean(el.placeholder || el.name || el.id || "");
  }

  function groupLabel(el) {
    const set = el.closest("fieldset");
    const legend = set?.querySelector("legend");
    if (legend) return clean(legend.innerText);
    const group = el.closest("[role=radiogroup], [role=group]");
    if (group?.getAttribute("aria-label")) return clean(group.getAttribute("aria-label"));
    if (group?.getAttribute("aria-labelledby")) {
      return clean(document.getElementById(group.getAttribute("aria-labelledby"))?.innerText);
    }
    // Nearest preceding text block (common on custom ATS layouts).
    let node = (group || el.parentElement)?.parentElement;
    for (let i = 0; node && i < 4; i++, node = node.parentElement) {
      const text = clean(node.querySelector("label, legend, h3, h4, p, span")?.innerText);
      if (text && text.length < 300) return text;
    }
    return clean(el.name);
  }

  function collect() {
    const fields = [], elements = {}, radios = {};
    let n = 0;
    for (const el of document.querySelectorAll("input, select, textarea")) {
      const type = el.tagName === "SELECT" ? "select" : el.tagName === "TEXTAREA" ? "textarea" : (el.type || "text").toLowerCase();
      if (SKIP.has(type) || el.disabled || el.readOnly) continue;
      if (type === "radio") {
        const key = el.name || "radio" + n++;
        if (!radios[key]) {
          radios[key] = {id: "jr" + Object.keys(radios).length, label: groupLabel(el), type: "radio", options: [], required: el.required, els: []};
        }
        radios[key].options.push(labelFor(el) || el.value);
        radios[key].els.push(el);
        continue;
      }
      if (!visible(el)) continue;
      const id = "jf" + n++;
      const field = {id, label: labelFor(el), type, required: el.required || el.getAttribute("aria-required") === "true", options: []};
      if (type === "select") field.options = [...el.options].filter((o) => o.value !== "").map((o) => clean(o.text)).filter(Boolean);
      if (type === "checkbox") field.label = field.label || groupLabel(el);
      fields.push(field);
      elements[id] = el;
    }
    for (const group of Object.values(radios)) {
      if (!group.els.some((el) => visible(el) || visible(el.parentElement))) continue;
      elements[group.id] = group.els;
      const {els, ...field} = group;
      fields.push(field);
    }
    return {fields, elements};
  }

  function setValue(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value); // works with React-controlled inputs
    for (const type of ["input", "change", "blur"]) el.dispatchEvent(new Event(type, {bubbles: true}));
  }

  function fill(target, value, source) {
    const mark = (el) => { el.style.outline = `2px solid ${COLORS[source]}`; el.style.outlineOffset = "2px"; };
    if (Array.isArray(target)) { // radio group
      const want = clean(value).toLowerCase();
      const el = target.find((r) => clean(labelFor(r) || r.value).toLowerCase() === want);
      if (el && !el.checked) el.click();
      if (el) mark(el.closest("label") || el.parentElement || el);
      return !!el;
    }
    if (target.tagName === "SELECT") {
      const option = [...target.options].find((o) => clean(o.text) === value);
      if (!option) return false;
      target.value = option.value;
      target.dispatchEvent(new Event("change", {bubbles: true}));
    } else if (target.type === "checkbox") {
      if (/^(yes|true|checked|1)$/i.test(value) || value === clean(labelFor(target))) {
        if (!target.checked) target.click();
      } else return false;
    } else {
      if (target.value && target.value.trim()) return false; // never overwrite what you typed
      setValue(target, value);
    }
    mark(target);
    return true;
  }

  function send(message) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(message, (reply) => resolve(reply || {error: chrome.runtime.lastError?.message || "No reply from Job Agent."}));
      } catch (e) {
        resolve({error: "Job Agent extension was reloaded; refresh this page."});
      }
    });
  }

  function toast(text) {
    const box = host.shadowRoot.getElementById("toast");
    box.textContent = text;
    box.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { box.hidden = true; }, 8000);
  }

  async function autofill() {
    const button = host.shadowRoot.getElementById("go");
    button.disabled = true;
    button.textContent = "Filling…";
    const {fields, elements} = collect();
    const reply = await send({type: "autofill", fields: JSON.stringify(fields)});
    button.disabled = false;
    button.textContent = "Autofill";
    if (reply.error) return toast(reply.error);
    let count = 0;
    for (const {id, value, source} of reply.values || []) {
      if (elements[id] && fill(elements[id], value, source)) count++;
    }
    filled = true;
    toast(`Filled ${count} field(s): green = profile, amber = AI. Review everything, then submit yourself. ${reply.message.includes("AI fill is off") ? "AI fill is off." : ""}`);
  }

  function jobInfo() {
    for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
      try {
        const items = [].concat(JSON.parse(script.textContent)).flatMap((x) => x["@graph"] || [x]);
        const job = items.find((x) => x && x["@type"] === "JobPosting");
        if (job) return {title: clean(job.title), company: clean(job.hiringOrganization?.name)};
      } catch (e) { /* ignore bad JSON-LD */ }
    }
    const site = document.querySelector('meta[property="og:site_name"]')?.content;
    const h1 = document.querySelector("h1")?.innerText;
    const parts = document.title.split(/\s+[-|–@]\s+|\s+at\s+/);
    return {title: clean(h1 || parts[0]), company: clean(site || parts[1] || location.hostname)};
  }

  async function applied() {
    const key = "jobAgentApplied:" + location.href;
    if (!filled || sessionStorage.getItem(key)) return;
    sessionStorage.setItem(key, "1");
    const reply = await send({type: "applied", url: location.href, ...jobInfo()});
    toast(reply.error ? "Could not log application: " + reply.error : reply.message);
  }

  function watchApply() {
    document.addEventListener("submit", () => applied(), true);
    document.addEventListener("click", (event) => {
      const el = event.target.closest?.("button, input[type=submit], [role=button], a");
      if (!el || !event.isTrusted) return; // only your real clicks count
      const text = clean(el.innerText || el.value || el.getAttribute("aria-label"));
      if (text.length < 40 && APPLY_TEXT.test(text) && !NOT_APPLY.test(text)) applied();
    }, true);
  }

  function looksLikeApplication() {
    const {fields} = collect();
    const labels = fields.map((f) => f.label.toLowerCase()).join(" | ");
    return fields.length >= 3 && /name|email|e-mail|resume|phone/.test(labels);
  }

  function mount() {
    host = document.createElement("div");
    host.style.cssText = "all:initial;position:fixed;z-index:2147483647;right:16px;bottom:16px;";
    host.attachShadow({mode: "open"}).innerHTML = `
      <style>
        button{font:600 14px system-ui,sans-serif;background:#1d4ed8;color:#fff;border:0;border-radius:999px;padding:10px 16px;cursor:pointer;box-shadow:0 2px 8px #0004}
        button:disabled{opacity:.6;cursor:wait}
        #toast{font:13px system-ui,sans-serif;background:#111827;color:#fff;border-radius:8px;padding:10px 12px;margin-bottom:8px;max-width:320px;box-shadow:0 2px 8px #0004}
      </style>
      <div id="toast" hidden></div>
      <button id="go" type="button" title="Fill this form from your Job Agent profile (never submits)">Autofill</button>`;
    host.shadowRoot.getElementById("go").addEventListener("click", autofill);
    document.documentElement.append(host);
    watchApply();
  }

  // Single-page apps render forms late: keep checking briefly until one appears.
  let tries = 0;
  const timer = setInterval(() => {
    if (looksLikeApplication()) { clearInterval(timer); mount(); }
    else if (++tries > 30) clearInterval(timer);
  }, 2000);
  if (looksLikeApplication()) { clearInterval(timer); mount(); }
})();
