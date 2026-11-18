// The planner component: this week's tasks, calendar events and reminders (src/friday/planner.py
// builds the spec: {today, tasks: [{id, t, d}], events: [{i, t, s, e, l}], rem: [{id, t, at, r}]},
// times as local 'YYYY-MM-DDTHH:MM', all-day events as 'YYYY-MM-DD'). Changes stay on the page until
// Save, which sends them in one go: {done, del, edit: [{id, t, d}], new: [{t, d}], evdel, remdel}.
"use strict";

FridayApp.register("planner", function (spec, app) {
  const { el, icon, haptic, body } = app;
  const today = /^\d{4}-\d{2}-\d{2}$/.test(spec.today || "") ? spec.today : new Date().toISOString().slice(0, 10);
  const tasks = (spec.tasks || []).filter((x) => Number.isInteger(x.id) && typeof x.t === "string");
  const events = (spec.events || []).filter((x) => Number.isInteger(x.i) && typeof x.t === "string");
  const rems = (spec.rem || []).filter((x) => Number.isInteger(x.id) && typeof x.t === "string");
  const done = new Set(), del = new Set(), evdel = new Set(), remdel = new Set();
  const edits = new Map(); // task id -> {t, d}
  const news = []; // {t, d}
  let open = null; // the task whose editor is showing

  // ---- dates: plain 'YYYY-MM-DD' strings, compared as text; arithmetic in UTC so no zone shifts them.
  const day = (s) => (s || "").slice(0, 10);
  const time = (s) => (s && s.length > 10 ? s.slice(11, 16) : "");
  function plus(iso, n) {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d + n)).toISOString().slice(0, 10);
  }
  const week = Array.from({ length: 7 }, (_, n) => plus(today, n));
  function section(dateStr) {
    if (!dateStr) return "none";
    if (dateStr < today) return "overdue";
    return week.includes(dateStr) ? dateStr : "later";
  }
  const dayName = new Intl.DateTimeFormat(undefined, { weekday: "short", month: "numeric", day: "numeric", timeZone: "UTC" });
  function sectionTitle(key) {
    if (key === "overdue") return "Overdue";
    if (key === "none") return "No date";
    if (key === "later") return "Later";
    const label = dayName.format(new Date(key + "T00:00:00Z"));
    return key === today ? "Today · " + label : key === plus(today, 1) ? "Tomorrow · " + label : label;
  }
  const ORDER = ["overdue", ...week, "later", "none"];

  // A time, with its date when the section doesn't say the day (overdue, later).
  function stamp(s) {
    const t = time(s), d = day(s);
    if (!d) return "";
    const own = section(d) === d;
    return (own ? "" : d.slice(5).replace("-", "/") + (t ? " " : "")) + t;
  }

  function anyChange() {
    return done.size + del.size + evdel.size + remdel.size + edits.size + news.length > 0;
  }
  function update() {
    app.changed(anyChange());
    render();
  }
  function taskNow(t) {
    const e = edits.get(t.id) || {};
    return { t: e.t !== undefined ? e.t : t.t, d: e.d !== undefined ? e.d : t.d };
  }
  function setEdit(t, key, value) {
    const e = Object.assign({}, edits.get(t.id) || {});
    if (value === t[key]) delete e[key]; else e[key] = value;
    if (Object.keys(e).length) edits.set(t.id, e); else edits.delete(t.id);
    app.changed(anyChange());
  }

  // ---- rows
  function iconButton(name, label, onClick, cls) {
    const b = el("button", { type: "button", className: "icon-btn " + (cls || "") });
    b.setAttribute("aria-label", label);
    b.append(icon(name));
    b.addEventListener("click", (ev) => { ev.stopPropagation(); onClick(); });
    return b;
  }
  function row(kind, parts) {
    const r = el("div", { className: "row " + kind + (parts.struck ? " struck" : "") });
    if (parts.lead) r.append(parts.lead);
    const main = el("div", { className: "main" });
    main.append(el("div", { className: "title", textContent: parts.title }));
    if (parts.meta) main.append(el("div", { className: "meta", textContent: parts.meta }));
    r.append(main);
    if (parts.trail) r.append(parts.trail);
    return { r, main };
  }

  function taskRow(t) {
    const cur = taskNow(t);
    const removed = del.has(t.id), checked = done.has(t.id);
    const check = el("button", { type: "button", className: "check" + (checked ? " on" : "") });
    check.setAttribute("aria-label", checked ? "Not done" : "Done");
    check.setAttribute("aria-pressed", String(checked));
    if (checked) check.append(icon("check"));
    check.addEventListener("click", () => {
      if (removed) return;
      if (checked) done.delete(t.id); else done.add(t.id);
      haptic("select");
      update();
    });
    const meta = [stamp(cur.d), edits.has(t.id) ? "edited" : ""].filter(Boolean).join(" · ");
    const trail = removed ? el("button", { type: "button", className: "undo", textContent: "Undo" }) : null;
    if (trail) trail.addEventListener("click", () => { del.delete(t.id); update(); });
    const { r, main } = row("task", { lead: check, title: cur.t, meta, struck: removed || checked, trail });
    if (!removed) {
      main.classList.add("tappable");
      main.addEventListener("click", () => { open = open === t.id ? null : t.id; render(); });
    }
    const out = [r];
    if (open === t.id && !removed) out.push(editor(t, cur));
    return out;
  }

  function editor(t, cur) {
    const box = el("div", { className: "editor" });
    const title = el("input", { type: "text", value: cur.t, maxLength: 200, placeholder: "Task" });
    title.addEventListener("input", () => setEdit(t, "t", title.value.trim() || t.t));
    const due = el("input", { type: "datetime-local", value: cur.d || "", className: cur.d ? "" : "empty" });
    due.addEventListener("change", () => { if (due.value) setEdit(t, "d", due.value); });
    const actions = el("div", { className: "editor-actions" });
    const tmr = el("button", { type: "button", className: "pill", textContent: "Tomorrow" });
    tmr.addEventListener("click", () => {
      due.value = plus(today, 1) + "T" + (time(cur.d) || "09:00");
      setEdit(t, "d", due.value);
      haptic("select");
      render();
    });
    const remove = el("button", { type: "button", className: "pill danger", textContent: "Remove" });
    remove.addEventListener("click", () => { del.add(t.id); done.delete(t.id); open = null; haptic("warning"); update(); });
    const close = el("button", { type: "button", className: "pill", textContent: "Done" });
    close.addEventListener("click", () => { open = null; render(); });
    actions.append(tmr, remove, close);
    box.append(title, due, actions);
    return box;
  }

  function newRow(n, index) {
    const trail = iconButton("trash", "Remove", () => { news.splice(index, 1); update(); });
    const meta = [stamp(n.d), "new"].filter(Boolean).join(" · ");
    return [row("task new", { lead: el("span", { className: "check ghost" }), title: n.t, meta, trail }).r];
  }

  function eventRow(e) {
    const gone = evdel.has(e.i);
    const meta = [e.s.length > 10 ? stamp(e.s) + (e.e ? "–" + e.e : "") : (section(e.s) === e.s ? "All day" : stamp(e.s) + " all day"),
                  e.l].filter(Boolean).join(" · ");
    const trail = gone ? el("button", { type: "button", className: "undo", textContent: "Undo" })
      : iconButton("trash", "Remove from calendar", () => { evdel.add(e.i); haptic("warning"); update(); });
    if (gone) trail.addEventListener("click", () => { evdel.delete(e.i); update(); });
    return [row("event", { lead: el("span", { className: "dot" }), title: e.t, meta, struck: gone, trail }).r];
  }

  function remRow(m) {
    const gone = remdel.has(m.id);
    const trail = gone ? el("button", { type: "button", className: "undo", textContent: "Undo" })
      : iconButton("trash", "Cancel reminder", () => { remdel.add(m.id); haptic("warning"); update(); });
    if (gone) trail.addEventListener("click", () => { remdel.delete(m.id); update(); });
    const lead = el("span", { className: "lead-icon" });
    lead.append(icon("bell"));
    return [row("rem", { lead, title: m.t, meta: stamp(m.at) + (m.r ? " · repeats" : ""), struck: gone, trail }).r];
  }

  // ---- adding
  function adder() {
    const g = el("div", { className: "group adder" });
    const line = el("div", { className: "row" });
    const plusIcon = el("span", { className: "lead-icon accent" });
    plusIcon.append(icon("plus"));
    const title = el("input", { type: "text", placeholder: "Add a task", maxLength: 200, enterKeyHint: "done" });
    line.append(plusIcon, title);
    const more = el("div", { className: "editor", hidden: true });
    const due = el("input", { type: "datetime-local", className: "empty" });
    due.addEventListener("change", () => due.classList.toggle("empty", !due.value));
    const add = el("button", { type: "button", className: "pill primary", textContent: "Add" });
    const actions = el("div", { className: "editor-actions" });
    actions.append(add);
    more.append(due, actions);
    title.addEventListener("input", () => { more.hidden = !title.value.trim(); });
    function addIt() {
      const t = title.value.trim();
      if (!t) return;
      news.push({ t, d: due.value || "" });
      haptic("success");
      update();
    }
    add.addEventListener("click", addIt);
    title.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); addIt(); } });
    g.append(line, more);
    return g;
  }

  // ---- the whole page, redrawn on every change (a week fits easily)
  function render() {
    const buckets = new Map(ORDER.map((k) => [k, []]));
    const put = (key, sortKey, rows) => buckets.get(key).push({ sortKey, rows });
    tasks.forEach((t) => { const d = taskNow(t).d; put(section(day(d)), (d || "~") + "1", taskRow(t)); });
    news.forEach((n, i) => put(section(day(n.d)), (n.d || "~") + "2", newRow(n, i)));
    events.forEach((e) => put(section(day(e.s)), (e.s.length > 10 ? e.s : e.s + "T00:00") + "0", eventRow(e)));
    rems.forEach((m) => put(section(day(m.at)), m.at + "3", remRow(m)));

    const parts = [adder()];
    let shown = 0;
    for (const key of ORDER) {
      const items = buckets.get(key);
      if (!items.length) continue;
      items.sort((a, b) => (a.sortKey < b.sortKey ? -1 : a.sortKey > b.sortKey ? 1 : 0));
      parts.push(el("h2", { className: "section" + (key === "overdue" ? " warn" : ""), textContent: sectionTitle(key) }));
      const g = el("div", { className: "group list" });
      items.forEach((it) => g.append(...it.rows));
      parts.push(g);
      shown += items.length;
    }
    if (!shown) parts.push(el("p", { className: "sub center", textContent: "Nothing this week. Add a task above." }));
    const counts = [done.size && done.size + " done", del.size + evdel.size + remdel.size && (del.size + evdel.size + remdel.size) + " removed",
                    edits.size && edits.size + " edited", news.length && news.length + " new"].filter(Boolean);
    if (counts.length) parts.push(el("p", { className: "sub center", textContent: counts.join(" · ") + " — tap Save" }));
    body.replaceChildren(...parts);
  }

  render();
  return {
    submitLabel: "Save",
    collect() {
      const values = {};
      if (done.size) values.done = [...done];
      if (del.size) values.del = [...del];
      const ed = [...edits].filter(([id]) => !del.has(id)).map(([id, e]) => Object.assign({ id }, e));
      if (ed.length) values.edit = ed;
      if (news.length) values.new = news;
      if (evdel.size) values.evdel = [...evdel];
      if (remdel.size) values.remdel = [...remdel];
      return { values };
    },
  };
});
