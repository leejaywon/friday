// Friday's Mini App: the part every component shares (src/friday/miniapp.py is the server side).
// The spec arrives in the URL fragment: {v: 1, kind, rid, key, title, description, site, submit,
// ...what the component needs}. The page shows the header, lets the component render its body,
// and sends {rid, values} back encrypted to the owner's server through Telegram.WebApp.sendData.
//
// A component is one file in components/ that calls
//   FridayApp.register(kind, function (spec, app) { ...; return { collect, submitLabel } })
// where collect() returns {values, skipped} to send (skipped: names the owner marked not needed),
// or null when something needs fixing (the component shows what). app gives it: body (its container), el, icon, haptic, changed (call on every edit,
// for the close confirmation), submit and inTelegram.
"use strict";

// ---- Encryption: pure functions (no DOM), so they can be checked outside the page. ----
function b64urlEncode(bytes) {
  let s = "";
  for (const b of new Uint8Array(bytes)) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function b64urlDecode(text) {
  const s = atob(text.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((text.length + 3) % 4));
  return Uint8Array.from(s, (c) => c.charCodeAt(0));
}
// The fragment is '#<spec>', possibly followed by Telegram's own '?tgWebAppData=...' or '&...'.
function decodeSpec(hash) {
  for (const part of (hash || "").replace(/^#/, "").split(/[?&]/)) {
    if (!/^[A-Za-z0-9_-]{16,}$/.test(part)) continue;
    try {
      const spec = JSON.parse(new TextDecoder().decode(b64urlDecode(part)));
      if (spec && spec.v === 1 && Number.isInteger(spec.rid) && spec.key) return spec;
    } catch (e) { /* not ours */ }
  }
  return null;
}
// {rid, values} -> sendData's JSON {v, k, iv, ct}: AES-256-GCM with a fresh key, the key wrapped
// with the server's RSA-OAEP-SHA256 public key (SPKI, base64url).
async function encryptForServer(publicKeyB64, payload) {
  const subtle = crypto.subtle;
  const pub = await subtle.importKey("spki", b64urlDecode(publicKeyB64), { name: "RSA-OAEP", hash: "SHA-256" }, false, ["encrypt"]);
  const aes = await subtle.generateKey({ name: "AES-GCM", length: 256 }, true, ["encrypt"]);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = await subtle.encrypt({ name: "AES-GCM", iv }, aes, new TextEncoder().encode(JSON.stringify(payload)));
  const k = await subtle.encrypt({ name: "RSA-OAEP" }, pub, await subtle.exportKey("raw", aes));
  return JSON.stringify({ v: 1, k: b64urlEncode(k), iv: b64urlEncode(iv), ct: b64urlEncode(ct) });
}

window.FridayApp = (function () {
  const components = {};
  const tg = window.Telegram && window.Telegram.WebApp;
  const inTelegram = !!(tg && tg.platform && tg.platform !== "unknown");
  const $ = (id) => document.getElementById(id);
  const root = document.documentElement;
  const LIMIT = 4096; // sendData's maximum
  // Static icons (this file's own markup, never spec content).
  const ICON = {
    globe: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9.5"/><path d="M2.5 12h19M12 2.5c2.5 2.6 3.8 5.8 3.8 9.5s-1.3 6.9-3.8 9.5c-2.5-2.6-3.8-5.8-3.8-9.5s1.3-6.9 3.8-9.5z"/></svg>',
    eye: '<svg viewBox="0 0 24 24"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>',
    eyeOff: '<svg viewBox="0 0 24 24"><path d="M10.6 5.1A10.4 10.4 0 0 1 12 5c6.4 0 10 7 10 7a17.6 17.6 0 0 1-2.6 3.5M6.6 6.6C3.7 8.5 2 12 2 12s3.6 7 10 7c1.9 0 3.6-.6 5-1.5M9.9 9.9a3 3 0 0 0 4.2 4.2M3 3l18 18"/></svg>',
    chevron: '<svg viewBox="0 0 24 24"><path d="M7 10l5 5 5-5"/></svg>',
    info: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9.5"/><path d="M12 11v5.5M12 7.5v.01"/></svg>',
    alert: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9.5"/><path d="M12 7.5V13M12 16.5v.01"/></svg>',
    lock: '<svg viewBox="0 0 24 24"><rect x="5" y="10.5" width="14" height="10" rx="2.5"/><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3"/></svg>',
  };

  const can = (v) => inTelegram && tg.isVersionAtLeast && tg.isVersionAtLeast(v);
  function haptic(kind) {
    if (!inTelegram || !tg.HapticFeedback) return;
    if (kind === "select") tg.HapticFeedback.selectionChanged(); else tg.HapticFeedback.notificationOccurred(kind);
  }
  function el(tag, props) {
    const e = document.createElement(tag);
    Object.assign(e, props || {});
    return e;
  }
  function icon(name, cls) {
    const s = el("span", { className: cls || "" });
    s.innerHTML = ICON[name];
    s.setAttribute("aria-hidden", "true");
    return cls ? s : s.firstChild;
  }

  function applyTheme() {
    const dark = tg.colorScheme === "dark";
    root.classList.toggle("dark", dark);
    root.classList.toggle("light", !dark);
    root.style.colorScheme = dark ? "dark" : "light";
    try {
      if (can("6.1")) { tg.setHeaderColor("secondary_bg_color"); tg.setBackgroundColor("secondary_bg_color"); }
      if (can("7.10")) tg.setBottomBarColor("secondary_bg_color");
    } catch (e) { /* older client: keep its defaults */ }
  }

  function showProblem(title, text) {
    const box = el("div", { className: "empty-state" });
    box.append(icon("alert"), el("h1", { textContent: title }), el("p", { className: "sub", textContent: text }));
    $("main").replaceChildren(box);
  }

  // Header: the title, then only what it doesn't already say. Everything is set as text.
  function header(spec) {
    const title = spec.title || "Friday";
    const site = String(spec.site || "").trim().replace(/^[a-z]+:\/\//i, "").replace(/[/?#].*$/, "");
    const norm = (s) => String(s || "").toLowerCase().replace(/[\s\p{P}\p{S}]+/gu, "");
    const titleKey = norm(title);
    const titleHasSite = !!site && (title.toLowerCase().includes(site.toLowerCase()) ||
                                    title.toLowerCase().includes(site.toLowerCase().replace(/^www\./, "")));
    const words = String(spec.description || "").split(/\s+/).map(norm).filter(Boolean);
    const addsSomething = words.length > 0 && !words.every((w) => titleKey.includes(w) || norm(site).includes(w));
    document.title = title;
    $("title").textContent = title;
    if (addsSomething) { $("desc").textContent = spec.description; $("desc").hidden = false; }
    if (site && !titleHasSite) {
      $("site").append(icon("globe"), el("span", { textContent: site }));
      $("site").hidden = false;
    }
    $("header").hidden = false;
  }

  function start() {
    if (inTelegram) {
      applyTheme();
      tg.onEvent("themeChanged", applyTheme);
      tg.ready();
      tg.expand();
      try { if (can("7.7")) tg.disableVerticalSwipes(); } catch (e) { /* not supported */ }
    }
    const spec = decodeSpec(window.FRIDAY_HASH);
    if (!spec) return showProblem("Can't open this request", "Open it again from the button under Friday's message.");
    const render = components[spec.kind || "form"];
    if (!render) return showProblem("Can't open this request", "This page is older than your Friday. Try again in a few minutes.");
    header(spec);

    let sending = false, dirty = false, view = null;
    // Ask before closing only once something was entered.
    function changed(now) {
      if (now === dirty) return;
      dirty = now;
      if (can("6.2")) { if (dirty) tg.enableClosingConfirmation(); else tg.disableClosingConfirmation(); }
    }
    function fail(text) {
      haptic("error");
      if (can("6.2")) { tg.showAlert(text); return; }
      $("error").textContent = text;
      $("error").hidden = false;
    }
    async function submit() {
      if (sending) return;
      $("error").hidden = true;
      const answer = view.collect();
      if (!answer) { haptic("error"); return; }
      if (!inTelegram) { // a preview in a browser: point at the note instead of sending
        const p = $("preview");
        p.classList.remove("flash");
        void p.offsetWidth;
        p.classList.add("flash");
        p.scrollIntoView({ block: "nearest", behavior: "smooth" });
        return;
      }
      sending = true;
      tg.MainButton.showProgress(false);
      tg.MainButton.disable();
      try {
        const data = await encryptForServer(spec.key, { rid: spec.rid, values: answer.values, skipped: answer.skipped || [] });
        if (data.length > LIMIT) throw new Error("too long");
        haptic("success");
        if (can("6.2")) tg.disableClosingConfirmation();
        tg.sendData(data); // Telegram closes the Mini App after this
      } catch (e) {
        sending = false;
        tg.MainButton.hideProgress();
        tg.MainButton.enable();
        fail(e && e.message === "too long" ? "That's too long to send." : "Couldn't encrypt it. Please try again.");
      }
    }

    view = render(spec, { body: $("body"), el, icon, haptic, changed, submit, inTelegram });
    const label = spec.submit || view.submitLabel || "Send";
    if (inTelegram) {
      tg.MainButton.setParams({ text: label, is_active: true, is_visible: true });
      tg.MainButton.onClick(submit);
    } else {
      $("preview").append(icon("info"), el("span", { textContent: "Preview · sending works only in Telegram" }));
      $("preview").hidden = false;
      $("send").textContent = label;
      $("send").hidden = false;
      $("send").addEventListener("click", submit);
    }
  }

  // Components register as their files load; start once they all have.
  document.addEventListener("DOMContentLoaded", start);
  return { register: (kind, render) => { components[kind] = render; } };
})();
