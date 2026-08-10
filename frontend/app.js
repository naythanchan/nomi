(function () {
  "use strict";
  const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const $ = (s) => document.querySelector(s);

  const state = {
    me: null,
    ai: false,
    mode: "schedule",     // 'schedule' | 'ask'
    chips: [],            // attendee emails/names (authoritative, shared by both modes)
    duration: 30,
    location: "virtual",
    resp: null,           // last schedule response
    sel: 0,               // selected slot index (0 = proposal)
    ctx: {},              // conversational scheduling context (Ask mode)
    scheduleSeq: 0,       // invalidates stale async responses
    askSeq: 0,
    scheduleBusy: false,
    askBusy: false,
  };

  // ---------- helpers ----------
  async function api(path, opts) {
    try {
      const res = await fetch(path, { credentials: "include", ...opts });
      let data = null;
      try { data = await res.json(); } catch { /* no body */ }
      return { ok: res.ok, status: res.status, data };
    } catch {
      return { ok: false, status: 0, data: { detail: "Couldn't reach Nomi. Check your connection and try again." } };
    }
  }
  const fmtTime = (iso, tz) => new Date(iso).toLocaleTimeString("en-US", { timeZone: tz, hour: "numeric", minute: "2-digit" });
  const fmtDay = (iso, tz) => new Date(iso).toLocaleDateString("en-US", { timeZone: tz, weekday: "long", month: "short", day: "numeric" });
  const fmtDayShort = (iso, tz) => new Date(iso).toLocaleDateString("en-US", { timeZone: tz, weekday: "short", month: "short", day: "numeric" });
  function tzAbbr(iso, tz) {
    const p = new Intl.DateTimeFormat("en-US", { timeZone: tz, timeZoneName: "short" }).formatToParts(new Date(iso));
    const t = p.find((x) => x.type === "timeZoneName");
    return t ? t.value : "";
  }
  function localHour(iso, tz) {
    const s = new Date(iso).toLocaleString("en-US", { timeZone: tz, hour12: false, hour: "2-digit", minute: "2-digit" });
    const [h, m] = s.split(":").map(Number);
    return h + m / 60;
  }
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
  const firstName = (n) => (n || "").split(" ")[0] || n;
  const participantName = (p) => p.organizer ? "You" : firstName(p.name);

  // ---------- people chips (shared) ----------
  function addChips(raw) {
    const value = (raw || "").trim();
    // Commas are the unambiguous separator. Preserve spaces in directory names;
    // still accept a pasted whitespace-separated list when every item is an email.
    let tokens = value.includes(",") ? value.split(",") : [value];
    const words = value.split(/\s+/).filter(Boolean);
    if (!value.includes(",") && words.length > 1 && words.every((x) => x.includes("@"))) {
      tokens = words;
    }
    tokens.map((x) => x.trim()).filter(Boolean).forEach((tok) => {
      const key = tok.toLowerCase();
      if (!state.chips.some((c) => c.toLowerCase() === key)) state.chips.push(tok);
    });
    renderChips();
  }
  function removeChip(tok) {
    state.chips = state.chips.filter((c) => c !== tok);
    renderChips();
  }
  function renderChips() {
    const box = $("#chips");
    box.innerHTML = state.chips.map((c) =>
      `<span class="chip">${esc(c)}<button type="button" data-chip="${esc(c)}" aria-label="Remove">×</button></span>`).join("");
    box.querySelectorAll("button").forEach((b) => b.onclick = () => removeChip(b.dataset.chip));
  }
  function flushInput() { addChips($("#chipInput").value); $("#chipInput").value = ""; }

  // ---------- pet ----------
  const clock = () => $("#clock");
  function happy() { if (REDUCED) return; const c = clock(); c.classList.remove("happy"); void c.offsetWidth; c.classList.add("happy"); }
  function think(on) { if (!REDUCED) clock().classList.toggle("thinking", on); }
  function syncThinking() { think(state.scheduleBusy || state.askBusy); }
  function setScheduleBusy(on) {
    state.scheduleBusy = on;
    const btn = $("#go");
    btn.disabled = on; btn.setAttribute("aria-busy", String(on));
    btn.textContent = on ? "Confuzzling…" : "Annoy";
    syncThinking();
  }
  function setAskBusy(on) {
    state.askBusy = on;
    const btn = $("#askGo");
    btn.disabled = on; btn.setAttribute("aria-busy", String(on));
    btn.textContent = on ? "Confuzzling…" : "Annoy";
    syncThinking();
  }

  // ---------- bubble / mode state ----------
  function openBubble() {
    $("#bubble").classList.add("open"); $("#hint").classList.add("gone");
    $("#pet").setAttribute("aria-expanded", "true");
  }
  function closeBubble() {
    $("#bubble").classList.remove("open");
    $("#pet").setAttribute("aria-expanded", "false");
  }

  function applyMode() {
    const authed = !!state.me;
    $("#signin").hidden = authed;
    $("#people").hidden = !authed;
    $("#modes").hidden = !(authed && state.ai);

    const scheduling = authed && state.mode === "schedule";
    const asking = authed && state.mode === "ask";

    $("#form").hidden = !scheduling;
    $("#ask").hidden = !asking;

    if (scheduling) {
      $("#nl").hidden = !state.ai;      // NL timing box only when AI is on
      $("#what").hidden = state.ai;     // title input only when AI is off
      $("#frow").hidden = false;
      $("#result").classList.remove("show");
    }
    renderChips();
    document.querySelectorAll("#modes button").forEach((b) => {
      const on = b.dataset.mode === state.mode;
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", String(on));
    });
  }

  function setMode(m) {
    if (m === state.mode) return;
    // A response from the mode we're leaving must not redraw stale UI later.
    state.scheduleSeq += 1;
    state.askSeq += 1;
    if (state.askBusy) $("#chat .c-me.pending")?.remove();
    setScheduleBusy(false);
    setAskBusy(false);
    state.mode = m;
    // A result belongs to Schedule mode. Leaving it visible underneath Ask
    // creates two competing interfaces and duplicate actions.
    $("#result").classList.remove("show");
    applyMode();
  }

  // ---------- schedule ----------
  async function go() {
    if (state.scheduleBusy) return;
    $("#formErr").hidden = true;
    flushInput();
    const attendees = state.chips.slice();
    if (!attendees.length) { showErr("#formErr", "Add at least one person to invite."); return; }

    let body, path;
    if (state.ai) {
      path = "/api/smart-schedule";
      body = { attendees, text: $("#nl").value.trim(),
               duration_minutes: state.duration, location_type: state.location };
    } else {
      path = "/api/schedule";
      body = { title: $("#what").value.trim() || "Meeting", attendees,
               duration_minutes: state.duration, location_type: state.location };
    }

    const requestId = ++state.scheduleSeq;
    setScheduleBusy(true);
    const started = Date.now();
    const { ok, status, data } = await api(path, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const wait = Math.max(0, 700 - (Date.now() - started));
    if (!REDUCED && wait) await new Promise((resolve) => setTimeout(resolve, wait));
    if (requestId !== state.scheduleSeq || state.mode !== "schedule") return;
    setScheduleBusy(false);
    if (status === 401) { state.me = null; applyMode(); return; }
    if (!ok) { showErr("#formErr", (data && data.detail) || "Something went wrong. Try again."); return; }
    state.resp = data;
    state.sel = 0;
    $("#form").hidden = true;
    $("#result").classList.add("show");
    renderResult();
    happy();
  }

  // combined [proposal, ...alternatives] with a stable index
  function slotList(r) {
    const out = [];
    if (r.proposal) out.push(r.proposal);
    (r.alternatives || []).forEach((a) => out.push(a));
    return out;
  }

  function participantsLine(r) {
    const unknown = r.calendar_unknown || [];
    const checked = (r.participants || []).filter((p) => !unknown.includes(p.email));
    let html = "";
    if (checked.length) {
      html += `<p class="r-checked">Checked ${esc(checked.map(participantName).join(", "))}.</p>`;
    }
    if (unknown.length) {
      html += `<p class="r-flag">⚠ Couldn't check ${unknown.length === 1 ? "this calendar" : "these calendars"}: ${esc(unknown.join(", "))} — inviting anyway.</p>`;
    }
    return html;
  }

  function tzLines(slot, r, loc) {
    const people = r.participants.filter((p) => p.timezone);
    const zones = [...new Set(people.map((p) => p.timezone))];
    if (loc !== "virtual" || zones.length <= 1) return "";
    const orgZone = r.org_timezone, groups = {};
    people.forEach((p) => { (groups[p.timezone] = groups[p.timezone] || []).push(participantName(p)); });
    const keys = Object.keys(groups).sort((a, b) => (b === orgZone) - (a === orgZone));
    return '<div class="r-tz">' + keys.map((z) => {
      const names = groups[z];
      const nl = names.length > 1 ? names.slice(0, -1).join(", ") + " & " + names.slice(-1) : names[0];
      const pain = localHour(slot.start, z) < 8.5 || localHour(slot.end, z) > 17.5;
      return `<span class="${pain ? "pain" : ""}">${esc(nl)} · <b>${fmtTime(slot.start, z)}</b> ${tzAbbr(slot.start, z)}</span>`;
    }).join("") + "</div>";
  }

  function renderResult() {
    const R = $("#resInner"), r = state.resp;
    const all = slotList(r);
    const loc = (r.intent && r.intent.location_type) || state.location;

    if (!all.length) {
      R.innerHTML = `<p class="none">${loc === "in-person"
        ? "No time within everyone's waking hours leaves room to travel. Try virtual, a shorter meeting, or another day."
        : "No time works within everyone's waking hours. Try another day or a shorter meeting."}</p>
        ${participantsLine(r)}
        <div class="r-actions"><button class="again" id="again">Start over</button></div>`;
      $("#again").onclick = toForm; return;
    }

    const s = all[state.sel] || all[0], orgTz = r.org_timezone;
    const nChecked = (r.participants || []).length - (r.calendar_unknown || []).length;

    // "requested time not available" banner
    let banner = "";
    if (r.requested_time_available === false && state.sel === 0) {
      banner = `<p class="r-warn">That time isn't free — here's the best alternative.</p>`;
    }

    const others = all.map((slot, i) => ({ slot, i })).filter((o) => o.i !== state.sel);
    const alts = others.length ? '<div class="alts">' + others.map(({ slot, i }) =>
      `<button class="alt" data-i="${i}"${r.booked ? " disabled" : ""}><span class="a-day">${fmtDayShort(slot.start, orgTz)}</span><span class="a-time">${fmtTime(slot.start, orgTz)}</span></button>`).join("") + "</div>" : "";

    R.innerHTML = `
      ${banner}
      <div class="r-day">${fmtDay(s.start, orgTz)}${loc === "in-person" ? " · in person" : ""}</div>
      <div class="r-time">${fmtTime(s.start, orgTz)} – ${fmtTime(s.end, orgTz)}</div>
      <span class="r-pill"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6"><path d="M20 6 9 17l-5-5"/></svg>${(r.calendar_unknown || []).length ? `Everyone I could check is free` : `All ${nChecked} free`}${loc === "in-person" ? " · room to travel" : ""}</span>
      ${tzLines(s, r, loc)}
      ${participantsLine(r)}
      <div class="r-actions"><button class="confirm" id="confirm">Confirm &amp; invite</button><button class="again" id="again">Start over</button></div>
      <p class="err" id="bookErr" hidden></p>
      ${alts}`;
    $("#confirm").onclick = book;
    $("#again").onclick = toForm;
    R.querySelectorAll(".alt").forEach((b) => b.onclick = () => { state.sel = +b.dataset.i; renderResult(); });
  }

  async function book(e) {
    const btn = e.target, r = state.resp, s = slotList(r)[state.sel];
    const emails = r.participants.map((p) => p.email).filter((em) => em !== state.me.email);
    const loc = (r.intent && r.intent.location_type) || state.location;
    btn.disabled = true; btn.textContent = "Sending…";
    const { ok, data } = await api("/api/book", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: (r.intent && r.intent.title) || "Meeting",
        location: loc === "virtual" ? "Virtual" : "In person",
        location_type: loc, attendees: emails, start: s.start, end: s.end,
      }),
    });
    if (ok) {
      r.booked = true;
      btn.textContent = "✓ Invites sent"; btn.style.filter = "brightness(.9)";
      $("#resInner").querySelectorAll(".alt").forEach((x) => { x.disabled = true; });
      if (data && data.html_link) {
        const a = document.createElement("a");
        a.className = "ask-link booked-link"; a.href = data.html_link;
        a.target = "_blank"; a.rel = "noopener"; a.textContent = "Open in Google Calendar →";
        btn.closest(".r-actions").after(a);
      }
      happy();
    }
    else {
      btn.disabled = false; btn.textContent = "Try again";
      showErr("#bookErr", (data && data.detail) || "Couldn't send the invites. Try again.");
    }
  }

  function toForm() { $("#result").classList.remove("show"); $("#form").hidden = false; }

  // ---------- ask / conversational ----------
  function chatBubble(cls, html) {
    const el = document.createElement("div");
    el.className = "c-turn " + cls;
    el.innerHTML = html;
    $("#chat").appendChild(el);
    el.scrollIntoView({ block: "nearest" });
    return el;
  }

  async function ask() {
    if (state.askBusy) return;
    $("#askErr").hidden = true;
    flushInput();
    const text = $("#askq").value.trim();
    if (!text) { showErr("#askErr", "Ask a question first."); return; }
    if (!state.chips.length) { showErr("#askErr", "Add at least one person first."); return; }

    const userTurn = chatBubble("c-me pending", esc(text));
    $("#askq").value = "";
    const requestId = ++state.askSeq;
    setAskBusy(true);
    const { ok, status, data } = await api("/api/ask", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, attendees: state.chips.slice(), context: state.ctx }),
    });
    if (requestId !== state.askSeq || state.mode !== "ask") return;
    userTurn.classList.remove("pending");
    setAskBusy(false);
    if (status === 401) { state.me = null; applyMode(); return; }
    if (!ok) {
      $("#askq").value = text;
      showErr("#askErr", (data && data.detail) || "Something went wrong.");
      return;
    }

    if (data.context) state.ctx = data.context;
    if (data.action === "booked") {
      chatBubble("c-nomi", `<p class="ask-reply">${esc(data.answer)}</p>
        ${data.html_link ? `<a class="ask-link" href="${esc(data.html_link)}" target="_blank" rel="noopener">Open in Google Calendar →</a>` : ""}`);
      happy(); return;
    }
    renderAskTurn(data);
    happy();
  }

  function renderAskTurn(d) {
    const tz = d.org_timezone;
    let html = `<p class="ask-reply">${esc(d.answer || "")}</p>`;
    const windows = d.windows || [];
    if (windows.length) {
      html += `<div class="availability-windows">` + windows.map((w) =>
        `<div><span>${fmtDayShort(w.start, tz)}</span><b>${fmtTime(w.start, tz)}–${fmtTime(w.end, tz)}</b></div>`
      ).join("") + `</div>`;
    }
    const p = d.proposal;
    if (p) {
      html += `<div class="a-prop"><b>${fmtDay(p.start, tz)}</b> · ${fmtTime(p.start, tz)}–${fmtTime(p.end, tz)}${(d.intent && d.intent.location_type === "in-person") ? " · in person" : ""}</div>`;
      const unknown = d.calendar_unknown || [];
      const dots = (d.participants || []).map((pt) => {
        const un = unknown.includes(pt.email);
        const color = un ? "var(--amber)" : "var(--accent)";
        const label = un ? "couldn't check" : "free";
        const name = participantName(pt);
        const detail = un ? ` title="Google doesn't expose this calendar to the signed-in account"` : "";
        return `<span class="p"${detail}><span class="d" style="background:${color}"></span><b>${esc(name)}</b> · ${label}</span>`;
      }).join("");
      if (dots) html += `<div class="ask-people">${dots}</div>`;
      const alts = (d.alternatives || []);
      if (alts.length) {
        html += `<div class="a-alts">` + alts.map((a) =>
          `<button class="a-chip" data-start="${esc(a.start)}" data-end="${esc(a.end)}">${fmtDayShort(a.start, tz)} ${fmtTime(a.start, tz)}</button>`).join("") + `</div>`;
      }
      html += `<button class="confirm mini" data-book>Schedule it</button>`;
    }
    const el = chatBubble("c-nomi", html);

    if (p) {
      const bookBtn = el.querySelector("[data-book]");
      bookBtn.onclick = () => bookProposal(bookBtn);
      el.querySelectorAll(".a-chip").forEach((b) => b.onclick = () => {
        // choose this alternative as the current proposal, then confirm
        state.ctx.current_proposal = {
          start: b.dataset.start, end: b.dataset.end,
          title: (d.intent && d.intent.title) || state.ctx.title || "Meeting",
          location_type: (d.intent && d.intent.location_type) || "virtual",
        };
        chatBubble("c-me", `${fmtDayShort(b.dataset.start, tz)} ${fmtTime(b.dataset.start, tz)} works`);
        chatBubble("c-nomi", `<p class="ask-reply">Great — ${fmtDay(b.dataset.start, tz)} at ${fmtTime(b.dataset.start, tz)} it is.</p>
          <button class="confirm mini" data-book2>Schedule it</button>`);
        $("#chat").lastChild.querySelector("[data-book2]").onclick = (e) => bookProposal(e.target);
      });
    }
  }

  async function bookProposal(btn) {
    const prop = state.ctx.current_proposal;
    if (!prop) return;
    const emails = state.chips.slice();  // resolved server-side; organizer excluded there
    btn.disabled = true; btn.textContent = "Sending…";
    const { ok, data } = await api("/api/book", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: prop.title || "Meeting",
        location: prop.location_type === "virtual" ? "Virtual" : "In person",
        location_type: prop.location_type || "virtual",
        attendees: emails, start: prop.start, end: prop.end,
      }),
    });
    if (ok) {
      btn.textContent = "✓ Invites sent";
      delete state.ctx.current_proposal;
      $("#chat").querySelectorAll(".a-chip, [data-book], [data-book2]").forEach((x) => {
        x.disabled = true;
      });
      if (data && data.html_link) {
        const a = document.createElement("a");
        a.className = "ask-link booked-link"; a.href = data.html_link; a.target = "_blank"; a.rel = "noopener";
        a.textContent = "Open in Google Calendar →";
        btn.after(a);
      }
      happy();
    } else {
      btn.disabled = false; btn.textContent = "Try again";
      const err = document.createElement("span");
      err.className = "book-inline-error";
      err.textContent = (data && data.detail) || "Couldn't send the invites.";
      btn.after(err);
    }
  }

  function showErr(sel, msg) { const el = $(sel); el.textContent = msg; el.hidden = false; }

  // ---------- wiring ----------
  $("#pet").addEventListener("click", () => {
    if ($("#bubble").classList.contains("open")) { closeBubble(); return; }
    applyMode(); openBubble();
  });
  $("#modes").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setMode(b.dataset.mode); });
  $("#go").addEventListener("click", go);
  $("#askGo").addEventListener("click", ask);
  $("#nl").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
  $("#askq").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(); } });
  $("#what").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
  $("#chipInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") { e.preventDefault(); flushInput(); }
    else if (e.key === "Backspace" && !$("#chipInput").value && state.chips.length) { state.chips.pop(); renderChips(); }
  });
  $("#chipInput").addEventListener("blur", flushInput);
  $("#dur").addEventListener("change", (e) => {
    state.duration = +e.target.value;
  });
  $("#loc").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    document.querySelectorAll("#loc button").forEach((x) => {
      const on = x === b;
      x.classList.toggle("on", on);
      x.setAttribute("aria-pressed", String(on));
    });
    state.location = b.dataset.loc;
  });
  $("#theme").addEventListener("click", () => {
    const c = document.documentElement.getAttribute("data-theme");
    document.documentElement.setAttribute("data-theme",
      c === "dark" ? "light" : c === "light" ? "dark" : (matchMedia("(prefers-color-scheme: dark)").matches ? "light" : "dark"));
  });
  $("#signout").addEventListener("click", () => { window.location.href = "/auth/logout"; });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".bubble") && !e.target.closest(".pet")) closeBubble();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeBubble(); $("#pet").focus(); }
  });

  // ---------- init ----------
  (async function init() {
    const { data } = await api("/api/me");
    state.ai = !!(data && data.ai);
    if (data && data.authenticated) { state.me = data.user; $("#signout").hidden = false; }
  })();
})();
