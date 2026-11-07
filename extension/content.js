// Nova's content script (SPEC §9): runs in every frame, in the extension's isolated world. The worker calls
// globalThis.__nova.<fn>(...) through chrome.scripting.executeScript. No listeners, no timers while idle.
//   map({words, sections, limit})  visible actionable elements (+ headings / sections for scroll_to), ≤ limit
//   click(n, {label, highlightMs})  scrollIntoView -> highlight -> pointer events + click() -> what changed
//   fill(n, text, {submit})         set a field's text (never a password / card field)
//   read(what)                      the page's main text, or the selection
//   highlight(n, {on, ms})          outline an element (while Nova asks "Click “Submit”?")
//   wait({selector, text, timeout}) until something shows up
//   scroll(dir, amount) / scrollTo(n)
//   canvasItems()                   Canvas DOM fallback: To Do / planner links with their row text
//   caret(limit)                    dictation: the focused field and the text just before its caret
//   docsReady({focus, timeout})     Google Docs: wait until the editor is up (and focus it)
(() => {
  if (globalThis.__nova && globalThis.__nova.version >= 2) return;

  const ACTIONABLE = [
    "a[href]", "button", "input:not([type=hidden])", "select", "textarea", "summary", "label[for]",
    "[role=button]", "[role=link]", "[role=tab]", "[role=menuitem]", "[role=menuitemcheckbox]", "[role=menuitemradio]",
    "[role=checkbox]", "[role=radio]", "[role=switch]", "[role=option]", "[role=combobox]", "[role=searchbox]",
    "[role=textbox]", "[role=treeitem]", "[contenteditable='']", "[contenteditable=true]", "[onclick]",
    "[tabindex]:not([tabindex='-1'])",
  ].join(",");
  const HEADINGS = "h1,h2,h3,h4,h5,h6,[role=heading]";
  const SECTIONS = "section[aria-label],section[aria-labelledby],[role=region][aria-label],nav[aria-label],aside[aria-label],[role=complementary][aria-label],form[aria-label]";
  const LANDMARKS = "nav,aside,header,footer,form,dialog,[role=navigation],[role=complementary],[role=banner],[role=contentinfo],[role=search],[role=dialog],[role=menu],[role=tablist]";
  const ROWS = "tr,li,[role=row],[role=listitem],article,[role=article]";
  const SECRET = /pass(word|code|wd)|\bpin\b|cvv|cvc|security.?code|card.?number|ccnum|credit.?card/i;
  const MAX_IDS = 4000;

  const refs = new Map(); // n -> WeakRef(element): ids from the last few maps stay valid for a click
  let seq = 0;

  const clean = (s, max = 100) => {
    s = String(s || "").replace(/\s+/g, " ").trim();
    return s.length > max ? s.slice(0, max - 1) + "…" : s;
  };

  function remember(el) {
    seq += 1;
    refs.set(seq, new WeakRef(el));
    if (refs.size > MAX_IDS) refs.delete(refs.keys().next().value);
    return seq;
  }

  function element(n) {
    const ref = refs.get(Number(n));
    const el = ref && ref.deref();
    return el && el.isConnected ? el : null;
  }

  function byId(el, id) {
    const root = el.getRootNode();
    return (root.getElementById && root.getElementById(id)) || document.getElementById(id);
  }

  function nameOf(el) {
    let s = el.getAttribute("aria-label") || "";
    if (!s) {
      const ids = el.getAttribute("aria-labelledby");
      if (ids) s = ids.split(/\s+/).map((id) => (byId(el, id) || {}).textContent || "").join(" ");
    }
    const tag = el.tagName;
    if (!s && (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT")) {
      const type = (el.type || "").toLowerCase();
      if (type === "submit" || type === "button" || type === "reset") s = el.value || "";
      if (!s && el.labels && el.labels.length) s = [...el.labels].map((l) => l.textContent).join(" ");
      if (!s) s = el.getAttribute("placeholder") || el.getAttribute("title") || el.alt || "";
      if (!s && type === "search") s = "search";
      if (!s) s = el.getAttribute("name") || el.id || "";
    }
    if (!s && tag !== "INPUT" && tag !== "TEXTAREA" && tag !== "SELECT") s = el.textContent || "";
    if (!clean(s)) {
      const img = el.querySelector && el.querySelector("img[alt],svg title,[aria-label]");
      s = el.getAttribute("title") || (img && (img.getAttribute("alt") || img.getAttribute("aria-label") || img.textContent)) || el.getAttribute("alt") || "";
    }
    return clean(s);
  }

  const INPUT_ROLES = { checkbox: "checkbox", radio: "radio", submit: "button", button: "button", reset: "button", image: "button", search: "searchbox", range: "slider", file: "button", color: "button" };

  function roleOf(el) {
    const r = el.getAttribute("role");
    if (r) return r.split(/\s+/)[0];
    switch (el.tagName) {
      case "A": return "link";
      case "BUTTON": case "SUMMARY": return "button";
      case "SELECT": return "combobox";
      case "TEXTAREA": return "textbox";
      case "LABEL": return "label";
      case "INPUT": return INPUT_ROLES[(el.type || "text").toLowerCase()] || "textbox";
      case "H1": case "H2": case "H3": case "H4": case "H5": case "H6": return "heading";
      case "SECTION": return "region";
      case "NAV": return "navigation";
      case "ASIDE": return "complementary";
      case "MAIN": return "main";
      case "ARTICLE": return "article";
      case "FORM": return "form";
      default: return el.isContentEditable ? "textbox" : "clickable";
    }
  }

  function secret(el) {
    const type = (el.type || "").toLowerCase();
    const auto = (el.getAttribute("autocomplete") || "").toLowerCase();
    if (type === "password" || auto.startsWith("cc-") || auto.includes("password")) return true;
    return SECRET.test(`${el.name || ""} ${el.id || ""} ${el.getAttribute("aria-label") || ""} ${el.getAttribute("placeholder") || ""}`);
  }

  function shown(el) {
    if (el.checkVisibility && !el.checkVisibility({ opacityProperty: true, visibilityProperty: true, checkOpacity: true, checkVisibilityCSS: true })) return null;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return null;
    // parked off to the side (skip links, screen-reader-only text): no scrolling brings it into view
    const page = document.scrollingElement || document.documentElement;
    if (r.right + scrollX <= 0 || r.left + scrollX >= Math.max(page.scrollWidth, innerWidth) || r.bottom + scrollY <= 0) return null;
    return r;
  }

  // Every open shadow root under `root` (nested ones too).
  function shadowRoots(root, out) {
    for (const el of root.querySelectorAll("*")) {
      if (el.shadowRoot) {
        out.push(el.shadowRoot);
        shadowRoots(el.shadowRoot, out);
      }
    }
    return out;
  }

  // The element that stands for `el` in document order (its outermost shadow host, if inside one).
  function anchor(el) {
    let node = el;
    while (node.getRootNode() instanceof ShadowRoot) node = node.getRootNode().host;
    return node;
  }

  function before(a, b) {
    return !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
  }

  // The last heading before `el` in document order (binary search over headings in document order).
  function headingBefore(headings, el) {
    let lo = 0;
    let hi = headings.length - 1;
    let found = null;
    const at = anchor(el);
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      const h = headings[mid];
      if (h === at || h.contains(at) || before(h, at)) {
        found = h;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    return found;
  }

  function sectionOf(el, headings) {
    const parts = [];
    const mark = el.closest(LANDMARKS);
    if (mark) {
      const label = mark.getAttribute("aria-label") || (mark.getAttribute("aria-labelledby") && (byId(mark, mark.getAttribute("aria-labelledby")) || {}).textContent);
      parts.push(clean(label, 40) || mark.getAttribute("role") || mark.tagName.toLowerCase());
    }
    const h = headingBefore(headings, el);
    if (h && h !== el) parts.push(clean(h.textContent, 60));
    return parts.join(" · ");
  }

  function rowOf(el, name) {
    const row = el.parentElement && el.parentElement.closest(ROWS);
    if (!row) return "";
    let text = clean(row.textContent, 300);
    if (!text || text.length >= 300) return "";
    if (name && text.includes(name)) text = clean(text.replace(name, " "), 300);
    return clean(text, 140);
  }

  // Clickables the selectors miss (divs with click handlers): sample the viewport and keep the outermost
  // element under each point whose cursor is a pointer.
  function pointerClickables(have) {
    const out = [];
    const cols = 12;
    const rows = 16;
    const seen = new Set();
    for (let i = 0; i < cols; i++) {
      for (let j = 0; j < rows; j++) {
        let el = document.elementFromPoint(((i + 0.5) * innerWidth) / cols, ((j + 0.5) * innerHeight) / rows);
        let best = null;
        for (let depth = 0; el && depth < 6 && el !== document.body; depth++, el = el.parentElement) {
          if (seen.has(el)) break;
          seen.add(el);
          if (have.has(el) || el.closest(ACTIONABLE)) {
            best = null;
            break;
          }
          if (getComputedStyle(el).cursor === "pointer") best = el;
        }
        if (best && !have.has(best)) {
          have.add(best);
          out.push(best);
        }
      }
    }
    return out;
  }

  function map(opts = {}) {
    const started = performance.now();
    const limit = Math.max(1, Math.min(opts.limit || 300, 1000));
    const words = (opts.words || []).map((w) => String(w).toLowerCase()).filter((w) => w.length > 1);
    const roots = shadowRoots(document, [document]);
    const found = new Set();
    for (const root of roots) for (const el of root.querySelectorAll(ACTIONABLE)) found.add(el);
    const headings = [];
    for (const root of roots) for (const el of root.querySelectorAll(HEADINGS)) headings.push(el);
    headings.sort((a, b) => (a === b ? 0 : before(anchor(a), anchor(b)) ? -1 : 1));
    if (opts.sections) {
      for (const h of headings) found.add(h);
      for (const root of roots) for (const el of root.querySelectorAll(SECTIONS)) found.add(el);
      if (words.length) {
        for (const root of roots) {
          for (const el of root.querySelectorAll("[id]")) {
            const id = el.id.toLowerCase();
            if (id.length <= 40 && words.some((w) => id.includes(w))) found.add(el);
          }
        }
      }
    }
    for (const el of pointerClickables(found)) found.add(el);

    const vw = innerWidth;
    const vh = innerHeight;
    const candidates = [];
    let order = 0;
    for (const el of found) {
      order += 1;
      if (el.closest("[data-nova]")) continue;
      const r = shown(el);
      if (!r) continue;
      const name = nameOf(el);
      const role = roleOf(el);
      if (!name && role !== "textbox" && role !== "searchbox" && role !== "combobox") continue;
      const inView = r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw;
      const low = name.toLowerCase();
      const hit = words.length && words.some((w) => low.includes(w));
      candidates.push({ el, r, name, role, inView, priority: (hit ? 4 : 0) + (inView ? 2 : 0) + (role === "link" || role === "button" ? 1 : 0), order });
    }
    const total = candidates.length;
    candidates.sort((a, b) => b.priority - a.priority || a.order - b.order);
    const kept = candidates.slice(0, limit).sort((a, b) => a.order - b.order);
    const elements = kept.map(({ el, r, name, role, inView }) => {
      const out = {
        id: remember(el), role, name, tag: el.tagName.toLowerCase(),
        section: sectionOf(el, headings), row: rowOf(el, name),
        box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)], inView,
      };
      if (el.href && typeof el.href === "string") out.href = el.href.slice(0, 300);
      if (el.disabled || el.getAttribute("aria-disabled") === "true") out.disabled = true;
      if (el.checked !== undefined && (role === "checkbox" || role === "radio" || role === "switch")) out.checked = !!el.checked;
      else if (el.getAttribute("aria-checked")) out.checked = el.getAttribute("aria-checked") === "true";
      if (el.getAttribute("aria-current") && el.getAttribute("aria-current") !== "false") out.current = true;
      if (role === "textbox" || role === "searchbox" || role === "combobox") {
        out.editable = true;
        if (secret(el)) out.secret = true;
        else if (typeof el.value === "string" && el.value) out.value = clean(el.value, 40);
        if ((el.type || "").toLowerCase() === "search" || role === "searchbox" || /search|query|\bq\b/i.test(`${el.name || ""} ${el.id || ""} ${el.getAttribute("placeholder") || ""}`)) out.search = true;
      }
      if (el.tagName === "BUTTON" || (el.tagName === "INPUT" && /submit|image/i.test(el.type || ""))) {
        if ((el.type || "submit").toLowerCase() === "submit" && el.form) out.submits = true;
      }
      return out;
    });
    const se = document.scrollingElement || document.documentElement;
    return {
      url: location.href, title: document.title, viewport: [vw, vh],
      scroll: [Math.round(se.scrollTop), Math.round(se.scrollHeight - se.clientHeight)],
      elements, total, ms: Math.round(performance.now() - started),
    };
  }

  // --- highlight -------------------------------------------------------------------------------------------------

  let lit = null;

  function unlight() {
    if (lit) {
      clearTimeout(lit.timer);
      lit.box.remove();
      lit = null;
    }
  }

  function light(el, ms) {
    unlight();
    const r = el.getBoundingClientRect();
    const box = document.createElement("div");
    box.setAttribute("data-nova", "highlight");
    Object.assign(box.style, {
      position: "fixed", left: `${r.left - 4}px`, top: `${r.top - 4}px`, width: `${r.width + 8}px`, height: `${r.height + 8}px`,
      border: "2px solid #0A84FF", borderRadius: "8px", boxShadow: "0 0 0 4px rgba(10,132,255,0.28)",
      zIndex: "2147483647", pointerEvents: "none", boxSizing: "border-box", margin: "0", padding: "0",
    });
    document.documentElement.appendChild(box);
    lit = { box, timer: setTimeout(unlight, ms) };
  }

  function highlight(n, opts = {}) {
    if (opts.on === false) {
      unlight();
      return { ok: true };
    }
    const el = element(n);
    if (!el) return { ok: false, error: "stale" };
    el.scrollIntoView({ block: "center", inline: "nearest", behavior: "instant" });
    light(el, opts.ms || 15000);
    return { ok: true };
  }

  // --- click ---------------------------------------------------------------------------------------------------

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function pointer(el, type) {
    const r = el.getBoundingClientRect();
    const init = { bubbles: true, cancelable: true, composed: true, view: window, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, button: 0, buttons: type.endsWith("down") ? 1 : 0 };
    const event = type.startsWith("pointer") ? new PointerEvent(type, { ...init, pointerId: 1, pointerType: "mouse", isPrimary: true }) : new MouseEvent(type, init);
    el.dispatchEvent(event);
  }

  // What happened after a click: "navigated" (address changed), "changed" (the page changed), or "none".
  function watch(ms) {
    return new Promise((resolve) => {
      const href = location.href;
      let timer = null;
      const finish = (effect) => {
        observer.disconnect();
        clearTimeout(timer);
        removeEventListener("pagehide", onHide);
        resolve(effect);
      };
      const onHide = () => finish("navigated");
      const observer = new MutationObserver((records) => {
        if (records.some((m) => !(m.target instanceof Element && m.target.closest("[data-nova]")) && ![...m.addedNodes, ...m.removedNodes].every((x) => x instanceof Element && x.hasAttribute("data-nova")))) {
          finish(location.href !== href ? "navigated" : "changed");
        }
      });
      observer.observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
      addEventListener("pagehide", onHide);
      timer = setTimeout(() => finish(location.href !== href ? "navigated" : "none"), ms);
    });
  }

  async function click(n, opts = {}) {
    const el = element(n);
    if (!el) return { ok: false, error: "stale" };
    const name = nameOf(el);
    if (opts.label && clean(opts.label) !== name) return { ok: false, error: "stale", name };
    if (el.disabled || el.getAttribute("aria-disabled") === "true") return { ok: false, error: "disabled", name };
    el.scrollIntoView({ block: "center", inline: "nearest", behavior: "instant" });
    const wait = opts.highlightMs ?? 300;
    if (wait > 0) {
      light(el, wait + 200);
      await sleep(wait);
    }
    unlight();
    const watching = watch(opts.watchMs || 400);
    pointer(el, "pointerdown");
    pointer(el, "mousedown");
    if (el.focus) el.focus({ preventScroll: true });
    pointer(el, "pointerup");
    pointer(el, "mouseup");
    if (typeof el.click === "function") el.click();
    else el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, composed: true, view: window }));
    const effect = await watching;
    return { ok: true, effect, name };
  }

  // --- fill ------------------------------------------------------------------------------------------------------

  function fill(n, text, opts = {}) {
    const el = element(n);
    if (!el) return { ok: false, error: "stale" };
    if (secret(el)) return { ok: false, error: "secret" };
    el.scrollIntoView({ block: "center", inline: "nearest", behavior: "instant" });
    el.focus({ preventScroll: true });
    if (el.isContentEditable) {
      const range = document.createRange();
      range.selectNodeContents(el);
      const sel = getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      document.execCommand("insertText", false, text);
    } else if ("value" in el) {
      const proto = Object.getPrototypeOf(el);
      const setter = Object.getOwnPropertyDescriptor(proto, "value");
      if (setter && setter.set) setter.set.call(el, text);
      else el.value = text;
      el.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
      el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
    } else {
      return { ok: false, error: "not a field" };
    }
    const value = el.isContentEditable ? el.textContent : el.value;
    if (opts.submit) {
      const key = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true, composed: true };
      const go = el.dispatchEvent(new KeyboardEvent("keydown", key));
      el.dispatchEvent(new KeyboardEvent("keyup", key));
      if (go && el.form) {
        if (el.form.requestSubmit) el.form.requestSubmit();
        else el.form.submit();
      }
    }
    return { ok: true, value: clean(value, 200), name: nameOf(el) };
  }

  // --- read / wait / scroll ----------------------------------------------------------------------------------------

  function read(what) {
    if (what === "selection") return { ok: true, text: String(getSelection() || "") };
    const main = document.querySelector("main,[role=main],article,#content") || document.body;
    return { ok: true, text: (main.innerText || "").slice(0, 20000), title: document.title, url: location.href };
  }

  function wait(opts = {}) {
    const started = performance.now();
    const timeout = Math.min(opts.timeout || 5000, 30000);
    const there = () => (opts.selector && document.querySelector(opts.selector)) || (opts.text && (document.body.textContent || "").includes(opts.text));
    return new Promise((resolve) => {
      if (there()) {
        resolve({ ok: true, ms: 0 });
        return;
      }
      const tick = setInterval(() => {
        if (there()) finish(true);
        else if (performance.now() - started > timeout) finish(false);
      }, 100);
      function finish(ok) {
        clearInterval(tick);
        resolve({ ok, ms: Math.round(performance.now() - started) });
      }
    });
  }

  function scrollable(el) {
    if (!el || el.scrollHeight <= el.clientHeight + 40) return false;
    if (el === document.scrollingElement) return true;
    return /(auto|scroll|overlay)/.test(getComputedStyle(el).overflowY);
  }

  // The thing that scrolls: the page, else the scrollable under the middle of the window, else the biggest one.
  function scroller() {
    const page = document.scrollingElement || document.documentElement;
    if (scrollable(page)) return page;
    for (let el = document.elementFromPoint(innerWidth / 2, innerHeight / 2); el && el !== document.documentElement; el = el.parentElement) {
      if (scrollable(el)) return el;
    }
    let best = null;
    let area = 0;
    for (const el of document.querySelectorAll("div,main,section,article,ul")) {
      if (!scrollable(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.width * r.height > area) {
        area = r.width * r.height;
        best = el;
      }
    }
    return best || page;
  }

  function scroll(dir, amount) {
    const el = scroller();
    const page = el === document.scrollingElement || el === document.documentElement;
    const h = page ? innerHeight : el.clientHeight;
    const w = page ? innerWidth : el.clientWidth;
    const share = { small: 0.25, page: 0.85, large: 2 }[amount || "page"] || 0.85;
    const maxY = el.scrollHeight - el.clientHeight;
    const maxX = el.scrollWidth - el.clientWidth;
    const y = el.scrollTop;
    const x = el.scrollLeft;
    let top = y;
    let left = x;
    if (dir === "down") top = Math.min(maxY, y + share * h);
    else if (dir === "up") top = Math.max(0, y - share * h);
    else if (dir === "top") top = 0;
    else if (dir === "bottom") top = maxY;
    else if (dir === "right") left = Math.min(maxX, x + share * w);
    else if (dir === "left") left = Math.max(0, x - share * w);
    const moved = Math.abs(top - y) >= 1 || Math.abs(left - x) >= 1;
    if (moved) el.scrollTo({ top, left, behavior: "smooth" });
    return { ok: true, moved, from: Math.round(y), to: Math.round(top), max: Math.round(maxY), page };
  }

  function scrollTo(n) {
    const el = element(n);
    if (!el) return { ok: false, error: "stale" };
    el.scrollIntoView({ block: "start", inline: "nearest", behavior: "smooth" });
    setTimeout(() => {
      if (el.isConnected) light(el, 900);
    }, 450);
    return { ok: true, name: nameOf(el) };
  }

  function describe(n) {
    const el = element(n);
    return el ? { ok: true, name: nameOf(el), role: roleOf(el) } : { ok: false, error: "stale" };
  }

  // Canvas DOM fallback (SPEC §10): links to course items on the page (dashboard To Do, planner, calendar)
  // with the text of the row they sit in (which carries the due date).
  function canvasItems() {
    const out = [];
    const seen = new Set();
    const links = document.querySelectorAll("a[href*='/assignments/'],a[href*='/quizzes/'],a[href*='/discussion_topics/'],a[href*='/pages/']");
    for (const a of links) {
      if (seen.has(a.href) || !/\/courses\/\d+\//.test(a.href)) continue;
      seen.add(a.href);
      const row = a.closest("li,tr,[role=row],[role=listitem],article,.ToDoSidebarItem,.planner-item,[data-testid]") || a.parentElement;
      out.push({ title: clean(a.textContent, 120), url: a.href, row: clean(row ? row.textContent : "", 240) });
      if (out.length >= 200) break;
    }
    return { items: out, url: location.href };
  }

  // --- the caret (dictation, SPEC §11) ------------------------------------------------------------------------------

  const TEXT_INPUTS = new Set(["", "text", "search", "email", "url", "tel"]);
  const BLOCKS = "p,div,li,h1,h2,h3,h4,h5,h6,blockquote,pre,td,th,dd,dt,section,article";

  function deepActive() {
    let el = document.activeElement;
    while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
    return el;
  }

  function docsEditor() {
    return location.hostname === "docs.google.com" && /^\/document\/d\//.test(location.pathname);
  }

  // Docs draws its text on a canvas; its cursor is still an element: where it is says whether a paste landed.
  function docsCursor() {
    const c = document.querySelector(".kix-cursor-caret") || document.querySelector(".kix-cursor");
    if (!c) return null;
    const r = c.getBoundingClientRect();
    return r.width || r.height ? [Math.round(r.left), Math.round(r.top), Math.round(r.height)] : null;
  }

  // Text of the caret's own line / paragraph before it ("" = it starts the line).
  function editableBefore(root, limit, focused) {
    const sel = getSelection();
    if (!sel || !sel.rangeCount) return { focused, editable: true, before: null };
    const r = sel.getRangeAt(0);
    let block = r.startContainer.nodeType === 1 ? r.startContainer : r.startContainer.parentElement;
    block = (block && block.closest(BLOCKS)) || root;
    if (!root.contains(block)) block = root;
    const pre = document.createRange();
    pre.selectNodeContents(block);
    pre.setEnd(r.startContainer, r.startOffset);
    return { focused, editable: true, before: pre.toString().slice(-limit), secret: secret(root) };
  }

  function caret(limit = 300) {
    const focused = document.hasFocus();
    if (docsEditor()) {
      return { focused, docs: true, editable: true, before: null, cursor: docsCursor(), ready: !!document.querySelector(".kix-appview-editor") };
    }
    const el = deepActive();
    if (!el || el === document.body || el === document.documentElement) {
      if (document.designMode === "on" || (document.body && document.body.isContentEditable)) return editableBefore(document.body, limit, focused);
      return { focused, editable: false };
    }
    if (el.tagName === "IFRAME" || el.tagName === "FRAME") return { focused, frame: true };
    const tag = el.tagName;
    if (tag === "INPUT" && secret(el)) return { focused, editable: true, secret: true };
    if (tag === "TEXTAREA" || (tag === "INPUT" && TEXT_INPUTS.has((el.getAttribute("type") || "").toLowerCase()))) {
      if (secret(el)) return { focused, editable: true, secret: true };
      const value = el.value || "";
      let at = null;
      try {
        at = el.selectionStart;
      } catch (e) {
        at = null;
      }
      if (at === null || at === undefined) at = value.length;
      return { focused, editable: true, before: value.slice(Math.max(0, at - limit), at), single: tag === "INPUT" };
    }
    if (el.isContentEditable) return editableBefore(el, limit, focused);
    return { focused, editable: false };
  }

  // Google Docs: until the editor is up a paste goes nowhere. Known parts: the editor (.kix-appview-editor) and the
  // hidden frame that takes his typing (.docs-texteventtarget-iframe). If Docs changes them, carry on once the page
  // has loaded and settled (known: false).
  async function docsReady(opts = {}) {
    const started = performance.now();
    const timeout = Math.min(opts.timeout || 10000, 30000);
    while (performance.now() - started < timeout) {
      const editor = document.querySelector(".kix-appview-editor");
      const target = document.querySelector("iframe.docs-texteventtarget-iframe");
      const settled = document.readyState === "complete" && performance.now() - started > 4000;
      if ((editor && document.readyState === "complete") || settled) {
        if (opts.focus && target && document.activeElement !== target) {
          try {
            target.focus();
            const inner = target.contentDocument && target.contentDocument.querySelector("[contenteditable=true]");
            if (inner) inner.focus();
          } catch (e) {
            /* best effort */
          }
        }
        return { ok: true, known: !!editor, focused: !target || document.activeElement === target, cursor: docsCursor(), ms: Math.round(performance.now() - started) };
      }
      await sleep(100);
    }
    return { ok: false, ms: Math.round(performance.now() - started) };
  }

  globalThis.__nova = { version: 2, map, click, fill, read, highlight, wait, scroll, scrollTo, describe, canvasItems, caret, docsReady };
})();
