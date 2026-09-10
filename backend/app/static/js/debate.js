/* Live debate view. Turns arrive over SSE as each model finishes, with a
   polling fallback for proxies that buffer event streams. */

(function () {
  const form = document.getElementById("ask-form");
  const box = document.getElementById("transcript");
  if (!form || !box) return;

  const askBtn = document.getElementById("ask-btn");
  const stopBtn = document.getElementById("stop-btn");
  const stage = document.getElementById("stage");
  const stageText = document.getElementById("stage-text");
  const questionEl = document.getElementById("question");

  const urls = {
    ask: form.dataset.endpoint,
    poll: box.dataset.poll,
    stream: box.dataset.stream,
    cancel: box.dataset.cancel,
  };
  const withId = (tpl, id) => tpl.replace(/\/0(\/|$)/, "/" + id + "$1");

  let discussionId = box.dataset.discussion || null;
  let lastId = 0;
  let lastRound = -1;
  let source = null;
  let poller = null;

  Array.from(box.querySelectorAll(".round-band b")).forEach((b) => {
    const n = parseInt(b.textContent.replace(/\D/g, ""), 10);
    if (!isNaN(n)) lastRound = n;
  });

  function setLive(on, text) {
    stage.dataset.live = on ? "1" : "0";
    stageText.textContent = text || "";
    askBtn.disabled = on;
    askBtn.textContent = on ? "Debate running" : "Start the debate";
    stopBtn.hidden = !on;
  }

  function el(tag, cls, html) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (html !== undefined) node.innerHTML = html;
    return node;
  }

  function roundBand(n) {
    const band = el("div", "round-band");
    band.appendChild(el("b", null, "Round " + n));
    band.appendChild(
      document.createTextNode(
        n === 1 ? " opening positions" : " cross-examination"
      )
    );
    return band;
  }

  function append(m) {
    if (m.role === "question") {
      const t = el("div", "turn turn--question");
      const who = el("div", "turn__who");
      who.style.setProperty("--speaker", "#172033");
      who.appendChild(el("b", null, escapeHtml(m.speaker)));
      who.appendChild(el("span", null, "asked"));
      t.appendChild(who);
      t.appendChild(el("div", "turn__body", escapeHtml(m.content)));
      box.appendChild(t);
      return;
    }

    if (m.role === "verdict") {
      const v = el("div", "verdict");
      v.appendChild(el("h2", null, "Verdict"));
      v.appendChild(
        el(
          "div",
          "verdict__by",
          "written by " +
            escapeHtml(m.speaker) +
            " (" + escapeHtml(m.model) + ") after reading the whole record"
        )
      );
      v.appendChild(el("div", "verdict__body", m.html));
      box.appendChild(v);
      v.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }

    if (m.role !== "turn" && m.role !== "error") return;

    if (m.round !== lastRound) {
      lastRound = m.round;
      box.appendChild(roundBand(m.round));
    }

    const t = el("div", "turn" + (m.role === "error" ? " turn--error" : ""));
    const who = el("div", "turn__who");
    who.style.setProperty("--speaker", m.color);
    who.appendChild(el("b", null, escapeHtml(m.speaker)));
    who.appendChild(el("span", null, escapeHtml(m.provider + " · " + m.model)));
    if (m.input_chars) {
      who.appendChild(
        el("div", "turn__meta", "read " + m.input_chars + " chars of transcript")
      );
    }
    t.appendChild(who);
    t.appendChild(
      el("div", "turn__body", m.role === "error" ? escapeHtml(m.content) : m.html)
    );
    box.appendChild(t);
    t.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : s;
    return d.innerHTML;
  }

  function finish(status, error) {
    setLive(false);
    if (source) { source.close(); source = null; }
    if (poller) { clearInterval(poller); poller = null; }
    if (error) {
      const warn = el("div", "flash flash--error", escapeHtml(error));
      warn.style.marginTop = "20px";
      box.appendChild(warn);
    } else if (status === "cancelled") {
      box.appendChild(el("div", "flash flash--ok", "Debate stopped."));
    }
  }

  function listen(id) {
    setLive(true, "Starting");
    if (window.EventSource) {
      source = new EventSource(withId(urls.stream, id) + "?after=" + lastId);
      source.addEventListener("message", (e) => {
        const m = JSON.parse(e.data);
        lastId = Math.max(lastId, m.id);
        append(m);
      });
      source.addEventListener("stage", (e) => {
        setLive(true, JSON.parse(e.data).stage);
      });
      source.addEventListener("end", (e) => {
        const d = JSON.parse(e.data);
        finish(d.status, d.error);
      });
      source.onerror = () => {
        // Proxy dropped the stream — fall back to polling.
        if (source) { source.close(); source = null; }
        if (!poller) startPolling(id);
      };
    } else {
      startPolling(id);
    }
  }

  function startPolling(id) {
    const tick = async () => {
      try {
        const r = await fetch(withId(urls.poll, id) + "?after=" + lastId, {
          headers: { Accept: "application/json" },
        });
        if (!r.ok) return;
        const data = await r.json();
        data.messages.forEach((m) => {
          lastId = Math.max(lastId, m.id);
          append(m);
        });
        setLive(true, data.stage);
        if (["done", "failed", "cancelled"].includes(data.status)) {
          finish(data.status, data.error);
        }
      } catch (_) { /* keep polling */ }
    };
    poller = setInterval(tick, 1500);
    tick();
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const question = questionEl.value.trim();
    if (!question) { questionEl.focus(); return; }

    setLive(true, "Queued");
    box.innerHTML = "";
    lastId = 0;
    lastRound = -1;

    try {
      const r = await fetch(urls.ask, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question,
          rounds: document.getElementById("rounds").value,
        }),
      });
      const data = await r.json();
      if (!r.ok) { finish("failed", data.error || "Could not start the debate."); return; }
      discussionId = data.discussion_id;
      box.dataset.discussion = discussionId;
      history.replaceState({}, "", "?discussion=" + discussionId);
      listen(discussionId);
    } catch (err) {
      finish("failed", "Network error while starting the debate.");
    }
  });

  stopBtn.addEventListener("click", async () => {
    if (!discussionId) return;
    stopBtn.disabled = true;
    await fetch(withId(urls.cancel, discussionId), { method: "POST" });
    stopBtn.disabled = false;
  });

  // Resume watching a debate that is still running from an earlier page load.
  if (discussionId && ["queued", "running"].includes(box.dataset.status)) {
    // Redraw from scratch so nothing is duplicated when the stream replays.
    lastId = 0;
    lastRound = -1;
    box.innerHTML = "";
    listen(discussionId);
  }
})();
