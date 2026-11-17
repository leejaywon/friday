// The form component: request_form, request_login, request_code and /login (src/friday/forms.py
// builds the spec: {fields: [{name, label, type, required, sensitive, placeholder, help,
// options, default}]}). Sends {name: value}; the server checks every value again.
"use strict";

FridayApp.register("form", function (spec, app) {
  const { el, icon, haptic } = app;
  const INPUT_TYPES = { text: "text", password: "password", code: "text", number: "number", email: "email",
                        url: "url", date: "date", time: "time" };
  const KINDS = ["text", "password", "code", "number", "email", "url", "textarea", "select", "choice",
                 "checkbox", "date", "time"];
  const AUTOCOMPLETE = { username: "username", password: "current-password", code: "one-time-code" };
  const fields = (spec.fields || []).filter((f) => f && typeof f.name === "string" && KINDS.includes(f.type));
  const form = el("form", { className: "group", noValidate: true, autocomplete: "on" });
  const controls = []; // {f, box, input, get(), focus(), initial}

  function placeholderFor(f) {
    if (f.placeholder) return f.placeholder;
    const l = f.label || f.name; // "Member ID" -> "member ID", "OTP" stays
    return (f.type === "select" ? "Choose " : "Enter ") + (/^[A-Z][a-z]/.test(l) ? l[0].toLowerCase() + l.slice(1) : l);
  }
  function clearError(box) {
    box.classList.remove("bad");
    box.querySelector(".msg").textContent = "";
  }
  function changed() {
    app.changed(controls.some((c) => c.get() !== c.initial));
  }

  fields.forEach(function (f, i) {
    const id = "f" + i;
    const name = f.label || f.name;
    const box = el("div", { className: "field" });
    const row = el("div", { className: "control" });
    let get, focus, input = null;

    const label = el(f.type === "choice" ? "span" : "label", { className: "label" });
    if (f.type !== "choice") label.htmlFor = id;
    label.append(el("span", { textContent: name }));
    if (f.required === false && f.type !== "checkbox") label.append(el("span", { className: "opt", textContent: "· optional" }));

    if (f.type === "checkbox") {
      const lab = el("label", { className: "switch-row" });
      input = el("input", { type: "checkbox", id: id, name: f.name, className: "switch", checked: f.default === true });
      input.setAttribute("role", "switch");
      lab.append(el("span", { className: "text", textContent: name }), input);
      row.appendChild(lab);
      input.addEventListener("change", () => haptic("select"));
      get = () => input.checked;
    } else if (f.type === "choice") {
      const group = el("div", { className: "choices", role: "group" });
      group.setAttribute("aria-label", name);
      let chosen = f.default || "";
      (f.options || []).forEach(function (o) {
        const b = el("button", { type: "button", textContent: o.label || o.value });
        b.setAttribute("aria-pressed", String(o.value === chosen));
        b.addEventListener("click", function () {
          chosen = o.value;
          for (const other of group.children) other.setAttribute("aria-pressed", String(other === b));
          clearError(box);
          haptic("select");
          changed();
        });
        group.appendChild(b);
      });
      row.appendChild(group);
      get = () => chosen;
      focus = () => group.firstChild && group.firstChild.focus();
    } else if (f.type === "select") {
      input = el("select", { id: id, name: f.name });
      input.appendChild(el("option", { value: "", textContent: placeholderFor(f), disabled: true }));
      (f.options || []).forEach((o) => input.appendChild(el("option", { value: o.value, textContent: o.label || o.value })));
      input.value = f.default || "";
      const sync = () => input.classList.toggle("empty", input.value === "");
      input.addEventListener("change", sync);
      sync();
      row.append(input, icon("chevron", "chev"));
      get = () => input.value;
    } else {
      input = el(f.type === "textarea" ? "textarea" : "input", { id: id, name: f.name });
      if (f.type !== "textarea") input.type = INPUT_TYPES[f.type];
      input.maxLength = f.type === "textarea" ? 1000 : 256;
      if (f.type === "textarea") {
        input.rows = 2;
        input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = input.scrollHeight + "px"; });
      }
      if (f.type === "number") { input.step = "any"; input.inputMode = "decimal"; }
      if (f.type === "code") { input.className = "code"; input.inputMode = "numeric"; }
      if (f.type === "email") input.inputMode = "email";
      if (f.type === "url") input.inputMode = "url";
      input.autocapitalize = "off";
      input.setAttribute("autocorrect", "off");
      input.spellcheck = false;
      if (f.type !== "date" && f.type !== "time") input.placeholder = placeholderFor(f);
      input.autocomplete = AUTOCOMPLETE[f.name] || AUTOCOMPLETE[f.type] || "off";
      if (f.default) input.value = f.default;
      if (f.type === "date" || f.type === "time") {
        const sync = () => input.classList.toggle("empty", input.value === "");
        input.addEventListener("input", sync);
        input.addEventListener("change", sync);
        sync();
      }
      row.appendChild(input);
      if (f.type === "password") {
        const t = el("button", { type: "button", className: "eye" });
        t.setAttribute("aria-label", "Show password");
        t.appendChild(icon("eye"));
        t.addEventListener("mousedown", (ev) => ev.preventDefault()); // keep the keyboard up
        t.addEventListener("click", function () {
          const show = input.type === "password";
          input.type = show ? "text" : "password";
          t.replaceChildren(icon(show ? "eyeOff" : "eye"));
          t.setAttribute("aria-label", show ? "Hide password" : "Show password");
          input.focus();
        });
        row.appendChild(t);
      }
      get = () => (f.type === "password" ? input.value : input.value.trim());
      if (f.type !== "textarea") {
        input.addEventListener("keydown", function (ev) {
          if (ev.key !== "Enter" || ev.isComposing) return;
          ev.preventDefault();
          const next = controls[controls.findIndex((c) => c.f === f) + 1];
          if (next) next.focus(); else app.submit();
        });
      }
    }
    if (input) {
      focus = focus || (() => input.focus());
      input.addEventListener("input", function () { clearError(box); changed(); });
      input.addEventListener("change", function () { clearError(box); changed(); });
    }
    if (f.type !== "checkbox") box.append(label);
    box.append(row);
    if (f.help) box.appendChild(el("div", { className: "help", textContent: f.help }));
    const msg = el("div", { className: "msg" });
    msg.setAttribute("role", "alert");
    box.appendChild(msg);
    form.appendChild(box);
    controls.push({ f: f, box: box, input: input, get: get, focus: focus || (() => {}), initial: get() });
  });
  // Enter key hints: "next" until the last single-line field, which sends.
  const typed = controls.filter((c) => c.input && c.input.tagName === "INPUT" && c.input.type !== "checkbox");
  typed.forEach((c, i) => { c.input.enterKeyHint = i === typed.length - 1 ? "send" : "next"; });
  form.addEventListener("submit", function (ev) { ev.preventDefault(); app.submit(); });
  app.body.appendChild(form);

  function collect() {
    let first = null;
    for (const c of controls) {
      const v = c.get();
      let problem = "";
      if (c.f.required !== false && (v === "" || v === false)) {
        problem = c.f.type === "checkbox" ? "Turn this on to send"
          : ["select", "choice", "date", "time"].includes(c.f.type) ? "Choose one" : "Required";
      } else if (c.input && c.input.validity && !c.input.validity.valid) {
        problem = "Check the format";
      }
      c.box.classList.toggle("bad", !!problem);
      c.box.querySelector(".msg").textContent = problem;
      if (problem && !first) first = c;
    }
    if (first) {
      first.focus();
      first.box.scrollIntoView({ block: "center", behavior: "smooth" });
      return null;
    }
    const values = {};
    for (const c of controls) values[c.f.name] = c.get();
    return values;
  }

  return { collect: collect, submitLabel: "Send" };
});
