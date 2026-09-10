/* multiai.online — static client for the /api/v1 JSON backend.
 *
 * No build step and no framework: this file is served as-is. Routing is
 * hash-based (#/groups/3) so the server needs no rewrite rules — any static
 * host will do, and a refresh on a deep link always resolves.
 */

(function () {
  "use strict";

  var API = window.MULTIAI_API || "/api/v1";
  var app = document.getElementById("app");
  var nav = document.getElementById("nav");
  var navAnon = document.getElementById("nav-anon");
  var toasts = document.getElementById("toasts");

  var S = {
    csrf: null,
    user: null,
    providers: [],
    suggested: {},
    limits: { max_rounds: 4, max_agents_per_group: 6, min_password_length: 10 },
    googleEnabled: false,
  };

  var live = {
    source: null, poller: null,
    discussionId: null, groupId: null,
    status: "idle", mode: "step", lastId: 0,
  };

  // ---------------------------------------------------------------- helpers
  // The server hands us <span class="math"> holding raw TeX. KaTeX turns it
  // into layout; if KaTeX never loaded, the TeX stays visible and readable.
  function renderMath(root) {
    if (!root || !window.katex) return;
    root.querySelectorAll(".math:not([data-done])").forEach(function (node) {
      var tex = node.textContent;
      try {
        window.katex.render(tex, node, {
          displayMode: node.dataset.display === "1",
          throwOnError: false,
          output: "html",
        });
        node.dataset.done = "1";
      } catch (err) {
        node.dataset.done = "1";  // leave the source text in place
      }
    });
  }

  function esc(value) {
    var d = document.createElement("div");
    d.textContent = value == null ? "" : String(value);
    return d.innerHTML;
  }

  function toast(message, kind) {
    var node = document.createElement("div");
    node.className = "toast toast--" + (kind || "ok");
    node.textContent = message;
    toasts.appendChild(node);
    setTimeout(function () { node.remove(); }, kind === "error" ? 7000 : 4000);
  }

  function ApiError(message, status) {
    this.message = message;
    this.status = status;
  }
  ApiError.prototype = Object.create(Error.prototype);

  async function api(path, options) {
    options = options || {};
    var init = {
      method: options.method || "GET",
      credentials: "same-origin",
      headers: {},
    };
    if (options.data !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.data);
    }
    if (init.method !== "GET") init.headers["X-CSRF-Token"] = S.csrf || "";

    var response;
    try {
      response = await fetch(API + path, init);
    } catch (err) {
      throw new ApiError("Cannot reach the server. Check your connection.", 0);
    }

    var payload = {};
    if ((response.headers.get("content-type") || "").indexOf("json") !== -1) {
      payload = await response.json();
    }
    if (!response.ok) {
      throw new ApiError(payload.error || "Request failed (" + response.status + ")",
                         response.status);
    }
    return payload;
  }

  async function upload(path, formData) {
    var response;
    try {
      response = await fetch(API + path, {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": S.csrf || "" },  // no Content-Type: the
        body: formData,                             // browser sets the boundary
      });
    } catch (err) {
      throw new ApiError("Cannot reach the server. Check your connection.", 0);
    }
    var payload = {};
    if ((response.headers.get("content-type") || "").indexOf("json") !== -1) {
      payload = await response.json();
    }
    if (!response.ok) {
      throw new ApiError(
        payload.error ||
          (response.status === 413
            ? "That file is too large for the server to accept."
            : "Upload failed (" + response.status + ")"),
        response.status
      );
    }
    return payload;
  }

  function ordinal(n) {
    return ["first", "second", "third", "fourth", "fifth", "sixth"][n - 1] ||
      n + "th";
  }

  function humanSize(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " KB";
    return (bytes / 1024 / 1024).toFixed(1) + " MB";
  }

  function renderFiles(files) {
    var strip = document.getElementById("file-strip");
    if (!strip) return;
    files = files || [];
    if (!files.length) { strip.innerHTML = ""; return; }
    strip.innerHTML = files.map(function (f) {
      var detail = f.readable
        ? humanSize(f.size_bytes) + " · " + f.chars.toLocaleString() + " chars read" +
          (f.truncated ? " · truncated" : "")
        : "not readable";
      return '<span class="file-chip' + (f.readable ? "" : " file-chip--dead") + '">' +
        '<a href="' + API + "/files/" + f.id + '/download">' + esc(f.filename) + "</a>" +
        "<em>" + esc(detail) + "</em>" +
        '<button type="button" data-remove-file="' + f.id +
        '" aria-label="Remove ' + esc(f.filename) + '">×</button></span>';
    }).join("");
  }

  function form(id) {
    var out = {};
    var root = document.getElementById(id);
    if (!root) return out;
    root.querySelectorAll("input, select, textarea").forEach(function (field) {
      if (!field.name) return;
      out[field.name] = field.type === "checkbox" ? field.checked : field.value;
    });
    return out;
  }

  function on(selector, event, handler) {
    var node = document.querySelector(selector);
    if (node) node.addEventListener(event, handler);
  }

  function setBusy(selector, busy, label) {
    var button = document.querySelector(selector);
    if (!button) return;
    button.disabled = busy;
    if (label) button.textContent = label;
  }

  // ----------------------------------------------------------------- router
  function parseHash() {
    var raw = (location.hash || "#/").replace(/^#/, "");
    return raw.split("/").filter(Boolean);
  }

  function go(path) {
    if (location.hash === "#" + path) route();
    else location.hash = path;
  }

  async function route() {
    stopStreaming();
    var parts = parseHash();
    var head = parts[0] || "";

    if (!S.user) {
      if (head === "register") return viewAuth("register");
      if (head === "signin") return viewAuth("signin");
      return viewLanding();
    }

    if (head === "agents") return viewAgents();
    if (head === "keys") return viewKeys();
    if ((head === "chats" || head === "groups") && parts[1]) {
      // "d" is the older path segment; old links keep working.
      var topicId = (parts[2] === "t" || parts[2] === "d") && parts[3]
        ? Number(parts[3]) : null;
      return viewChat(Number(parts[1]), topicId);
    }
    return viewDashboard();
  }

  function syncNav() {
    nav.hidden = !S.user;
    navAnon.hidden = !!S.user;
    var avatar = document.getElementById("avatar");
    if (avatar) {
      var url = S.user && S.user.avatar_url;
      avatar.hidden = !url;
      if (url) {
        avatar.src = url;
        avatar.alt = S.user.display_name || "";
      }
    }
    var current = "/" + (parseHash()[0] || "");
    nav.querySelectorAll("a[data-route]").forEach(function (link) {
      link.classList.toggle("active", link.dataset.route === current);
    });
  }

  // ------------------------------------------------------------ auth views
  function viewLanding() {
    syncNav();
    app.className = "wrap";
    app.innerHTML =
      '<section class="hero">' +
        "<h1>One hard problem. Several models. One answer.</h1>" +
        "<p>Start a chat, seat Claude, ChatGPT, Gemini or any model you host " +
        "yourself, and ask your question once. Each model reads what the others " +
        "just said and answers it directly. The chair writes the conclusion.</p>" +
        '<div class="hero__actions">' +
          googleButton("Continue with Google") +
          '<a class="btn btn--primary" href="#/register" style="text-decoration:none">Create account</a>' +
          '<a class="btn btn--quiet" href="#/signin" style="text-decoration:none">Sign in</a>' +
        "</div>" +
      "</section>" +
      '<section class="relay">' +
        '<div class="round-band"><b>Round 1</b> opening positions</div>' +
        relayStep("#2F2BA8", "Claude", "seat 1 · reads the question",
                  "Answers first. Nothing else is in the room yet.") +
        relayStep("#0E7C6B", "Gemini", "seat 2 · reads the question + Claude",
                  "Gets Claude's full answer as its own input, so it can pick the " +
                  "argument up instead of starting over.") +
        relayStep("#A2452F", "ChatGPT", "seat 3 · reads everything above",
                  "Sees both turns and can side with one, or take apart both.") +
        '<div class="round-band"><b>Round 2</b> cross-examination</div>' +
        relayStep("#2F2BA8", "Claude", "seat 1 again",
                  "Now holds every turn from round 1. It is told to name the weakest " +
                  "claim in the room, challenge it, and say what it changed.") +
        '<div class="round-band"><b>Verdict</b> written by the chair</div>' +
        relayStep("#172033", "Chair", "reads the whole record",
                  "One model you nominate reads the entire transcript and writes the " +
                  "answer: the recommendation, the reasoning that survived, where the " +
                  "panel split, and what would change its mind.") +
      "</section>";
  }

  function googleButton(label) {
    if (!S.googleEnabled) return "";
    return '<a class="btn btn--google" href="' + API + '/auth/google/start">' +
      '<svg viewBox="0 0 18 18" width="16" height="16" aria-hidden="true">' +
      '<path fill="#4285F4" d="M17.6 9.2c0-.6-.1-1.3-.2-1.9H9v3.5h4.8a4.1 4.1 0 0 1-1.8 2.7v2.3h2.9c1.7-1.6 2.7-3.9 2.7-6.6z"/>' +
      '<path fill="#34A853" d="M9 18c2.4 0 4.5-.8 6-2.2l-2.9-2.3c-.8.6-1.9.9-3.1.9-2.4 0-4.4-1.6-5.1-3.8H.9v2.4A9 9 0 0 0 9 18z"/>' +
      '<path fill="#FBBC05" d="M3.9 10.6a5.4 5.4 0 0 1 0-3.4V4.8H.9a9 9 0 0 0 0 8.1l3-2.3z"/>' +
      '<path fill="#EA4335" d="M9 3.6c1.3 0 2.5.5 3.4 1.3l2.6-2.6A9 9 0 0 0 .9 4.8l3 2.4C4.6 5 6.6 3.6 9 3.6z"/>' +
      "</svg>" + esc(label) + "</a>";
  }

  function relayStep(color, name, seat, text) {
    return '<div class="relay__step">' +
      '<div class="relay__seat" style="--speaker:' + color + '">' + esc(name) +
      "<span>" + esc(seat) + "</span></div>" +
      "<p>" + esc(text) + "</p></div>";
  }

  function viewAuth(mode) {
    syncNav();
    var registering = mode === "register";
    app.className = "wrap wrap--narrow";
    app.innerHTML =
      '<div class="panel"><div class="panel__head"><h2>' +
        (registering ? "Create your account" : "Sign in") +
      "</h2></div><div class='panel__body'>" +
        (S.googleEnabled
          ? '<div class="oauth-row">' +
            googleButton(registering ? "Continue with Google" : "Sign in with Google") +
            '</div><div class="or-rule"><span>or use an email address</span></div>'
          : "") +
      "<div id='auth-form'>" +
        (registering
          ? '<div class="field"><label for="display_name">Your name</label>' +
            '<input id="display_name" name="display_name" type="text" ' +
            'autocomplete="name" placeholder="How the panel addresses you"></div>'
          : "") +
        '<div class="field"><label for="email">Email</label>' +
        '<input id="email" name="email" type="email" autocomplete="email" required></div>' +
        '<div class="field"><label for="password">Password</label>' +
        '<input id="password" name="password" type="password" autocomplete="' +
        (registering ? "new-password" : "current-password") + '" required>' +
        (registering
          ? '<p class="hint">At least ' + S.limits.min_password_length + " characters.</p>"
          : "") +
        "</div>" +
        '<div style="margin-top:18px; display:flex; align-items:center; gap:16px">' +
          '<button class="btn btn--primary" id="auth-submit" type="button">' +
          (registering ? "Create account" : "Sign in") + "</button>" +
          (registering
            ? '<a href="#/signin">I already have one</a>'
            : '<a href="#/register">Create an account</a>') +
        "</div>" +
      "</div></div></div>";

    var submit = async function () {
      var data = form("auth-form");
      setBusy("#auth-submit", true, registering ? "Creating…" : "Signing in…");
      try {
        var result = await api(registering ? "/auth/register" : "/auth/login",
                               { method: "POST", data: data });
        S.user = result.user;
        await refreshSession();
        go("/");
      } catch (err) {
        toast(err.message, "error");
        setBusy("#auth-submit", false, registering ? "Create account" : "Sign in");
      }
    };

    on("#auth-submit", "click", submit);
    on("#auth-form", "keydown", function (e) {
      if (e.key === "Enter") submit();
    });
    var first = app.querySelector("input");
    if (first) first.focus();
  }

  // ------------------------------------------------------------- dashboard
  async function viewDashboard() {
    syncNav();
    app.className = "wrap";
    app.innerHTML = '<div class="loading">Loading your chats…</div>';

    var groups, agents;
    try {
      groups = (await api("/groups")).groups;
      agents = (await api("/agents")).agents;
    } catch (err) { return handleLoadError(err); }

    app.innerHTML =
      '<div class="split"><div><div class="panel">' +
        '<div class="panel__head"><h2>Your chats</h2>' +
        '<span class="panel__note">' + groups.length + " total</span></div>" +
        '<div class="panel__body">' +
          (groups.length
            ? '<div class="card-list">' + groups.map(function (g) {
                return '<a href="#/groups/' + g.id + '"><b>' + esc(g.name) + "</b><span>" +
                  g.agent_count + " agent" + (g.agent_count === 1 ? "" : "s") +
                  " · " + g.discussion_count + " topic" +
                  (g.discussion_count === 1 ? "" : "s") + "</span></a>";
              }).join("") + "</div>"
            : '<div class="empty"><h3>No chats yet</h3><p style="margin:0">A chat ' +
              "is a room of agents that stays together. Make one for a recurring " +
              "kind of problem — architecture calls, hiring decisions, debugging." +
              "</p></div>") +
        "</div></div></div>" +
        '<div class="stack">' +
          '<div class="panel"><div class="panel__head"><h2>New chat</h2></div>' +
          '<div class="panel__body"><div id="group-form">' +
            '<div class="field"><label for="name">Name</label>' +
            '<input id="name" name="name" type="text" placeholder="Architecture review"></div>' +
            '<div class="field"><label for="purpose">What this chat is for</label>' +
            '<input id="purpose" name="purpose" type="text" placeholder="Optional"></div>' +
            '<p class="hint">A chat is a room of agents that stays together. Open it ' +
            "and start talking — there is no round limit and no fixed ending.</p>" +
            '<button class="btn btn--primary" id="create-group" type="button" ' +
            'style="margin-top:14px">Create chat</button>' +
          "</div></div></div>" +
          agentSidebar(agents) +
        "</div></div>";

    on("#create-group", "click", async function () {
      var data = form("group-form");
      if (!data.name.trim()) return toast("Name the chat.", "error");
      setBusy("#create-group", true, "Creating…");
      try {
        var result = await api("/groups", { method: "POST", data: data });
        go("/chats/" + result.group.id);
      } catch (err) {
        toast(err.message, "error");
        setBusy("#create-group", false, "Create chat");
      }
    });
  }

  function agentSidebar(agents) {
    return '<div class="panel"><div class="panel__head"><h2>Your agents</h2>' +
      '<a class="panel__note" href="#/agents">Manage</a></div>' +
      '<div class="panel__body">' +
        (agents.length
          ? '<ul class="roster">' + agents.map(function (a) {
              return "<li><span class='rail' style='background:" + esc(a.color) +
                "'></span><span class='who'><b>" + esc(a.name) + "</b><span>" +
                esc(a.provider) + " · " + esc(a.model) + "</span></span></li>";
            }).join("") + "</ul>"
          : '<p style="margin:0">No agents yet. <a href="#/agents">Add your first one</a>.</p>') +
      "</div></div>";
  }

  // ----------------------------------------------------------------- agents
  async function viewAgents() {
    syncNav();
    app.className = "wrap";
    app.innerHTML = '<div class="loading">Loading agents…</div>';

    var agents, creds;
    try {
      agents = (await api("/agents")).agents;
      creds = (await api("/credentials")).credentials;
    } catch (err) { return handleLoadError(err); }

    app.innerHTML =
      '<div class="split"><div class="panel">' +
        '<div class="panel__head"><h2>Agents</h2>' +
        '<span class="panel__note">Each one is a model plus a role it plays</span></div>' +
        '<div class="panel__body">' +
          (agents.length
            ? '<ul class="roster">' + agents.map(function (a) {
                return "<li><span class='rail' style='background:" + esc(a.color) + "'></span>" +
                  "<span class='who'><b>" + esc(a.name) + "</b><span>" +
                  esc(a.provider) + " · " + esc(a.model) +
                  (a.role ? " · " + esc(a.role) : "") +
                  (a.has_key ? "" : " · no key attached") +
                  "</span></span>" +
                  '<button class="btn btn--danger btn--small" data-remove-agent="' +
                  a.id + '">Remove</button></li>';
              }).join("") + "</ul>"
            : '<div class="empty"><h3>Nobody in the room yet</h3><p style="margin:0">' +
              "Add two or three agents from different vendors. Different training " +
              "makes for a real argument; three copies of one model mostly agree " +
              "with themselves.</p></div>") +
        "</div></div>" +
        '<div class="panel"><div class="panel__head"><h2>Add an agent</h2></div>' +
        '<div class="panel__body"><div id="agent-form">' +
          '<div class="field"><label for="name">Name in the room</label>' +
          '<input id="name" name="name" type="text" maxlength="60" placeholder="e.g. Ada">' +
          '<p class="hint">Other agents address it by this name.</p></div>' +
          '<div class="field"><label for="provider">Provider</label>' +
          '<select id="provider" name="provider">' + S.providers.map(function (p) {
            return '<option value="' + esc(p.key) + '">' + esc(p.label) + "</option>";
          }).join("") + "</select></div>" +
          '<div class="field"><label for="credential_id">API key</label>' +
          '<select id="credential_id" name="credential_id"></select>' +
          (creds.length ? "" :
            '<p class="hint"><a href="#/keys">Add a key</a> to load the real model list.</p>') +
          "</div>" +
          '<div class="field"><label for="model">Model</label>' +
          '<div id="model-field"><select id="model" name="model" disabled>' +
          "<option>Loading…</option></select></div>" +
          '<div class="model-meta"><span class="hint" id="model-note"></span>' +
          '<button class="linklike" id="model-refresh" type="button">Refresh</button>' +
          '<button class="linklike" id="model-manual" type="button">Type a name</button>' +
          "</div></div>" +
          '<div class="field"><label for="role">Role</label>' +
          '<input id="role" name="role" type="text" maxlength="120" ' +
          'placeholder="Sceptic, cost analyst, security reviewer…"></div>' +
          '<div class="field"><label for="system_prompt">Standing instructions</label>' +
          '<textarea id="system_prompt" name="system_prompt" rows="3" ' +
          'placeholder="Panel rules are added automatically."></textarea></div>' +
          '<div class="field field-row"><div><label for="temperature">Temperature</label>' +
          '<input id="temperature" name="temperature" type="number" step="0.1" min="0" ' +
          'max="2" value="0.7"></div><div><label for="max_tokens">Max tokens per turn</label>' +
          '<input id="max_tokens" name="max_tokens" type="number" min="200" max="8000" ' +
          'value="1200"></div></div>' +
          '<button class="btn btn--primary" id="create-agent" type="button" ' +
          'style="margin-top:16px">Add agent</button>' +
        "</div></div></div></div>";

    var manualModel = false;

    function syncCredentials() {
      var provider = document.getElementById("provider").value;
      var select = document.getElementById("credential_id");
      var matching = creds.filter(function (c) { return c.provider === provider; });
      select.innerHTML =
        '<option value="">Use the server key, if one is set</option>' +
        matching.map(function (c) {
          return '<option value="' + c.id + '">' + esc(c.label) + " · " +
            esc(c.hint) + "</option>";
        }).join("");
      // Pick the first real key so the model list loads without a second click.
      if (matching.length) select.value = String(matching[0].id);
    }

    async function loadModels(refresh) {
      if (manualModel) return;
      var provider = document.getElementById("provider").value;
      var credId = document.getElementById("credential_id").value;
      var field = document.getElementById("model-field");
      var note = document.getElementById("model-note");

      field.innerHTML = '<select id="model" name="model" disabled>' +
        "<option>Loading models…</option></select>";
      note.textContent = "";

      var query = "/models?provider=" + encodeURIComponent(provider) +
        (credId ? "&credential_id=" + credId : "") +
        (refresh ? "&refresh=1" : "");

      try {
        var data = await api(query);
        if (!data.models.length) {
          field.innerHTML = '<input id="model" name="model" type="text" ' +
            'placeholder="model name">';
          note.textContent = data.note || "No models came back. Type a name instead.";
          return;
        }
        field.innerHTML = '<select id="model" name="model">' +
          data.models.map(function (m) {
            var label = m.label && m.label !== m.id ? m.label + " (" + m.id + ")" : m.id;
            return '<option value="' + esc(m.id) + '">' + esc(label) + "</option>";
          }).join("") + "</select>";
        note.textContent = data.source === "live"
          ? data.models.length + " models this key can use" + (data.cached ? ", cached" : "")
          : (data.note || "Built-in list — add a key to load the real one.");
      } catch (err) {
        field.innerHTML = '<input id="model" name="model" type="text" ' +
          'placeholder="claude-sonnet-4-5">';
        note.textContent = err.message;
      }
    }

    on("#provider", "change", function () { syncCredentials(); loadModels(); });
    on("#credential_id", "change", function () { loadModels(); });
    on("#model-refresh", "click", function () { loadModels(true); });
    on("#model-manual", "click", function () {
      manualModel = !manualModel;
      var button = document.getElementById("model-manual");
      if (manualModel) {
        document.getElementById("model-field").innerHTML =
          '<input id="model" name="model" type="text" placeholder="claude-sonnet-4-5">';
        document.getElementById("model-note").textContent =
          "Any model name the provider accepts.";
        button.textContent = "Pick from list";
      } else {
        button.textContent = "Type a name";
        loadModels();
      }
    });

    syncCredentials();
    loadModels();

    on("#create-agent", "click", async function () {
      setBusy("#create-agent", true, "Adding…");
      try {
        await api("/agents", { method: "POST", data: form("agent-form") });
        toast("Agent added.");
        viewAgents();
      } catch (err) {
        toast(err.message, "error");
        setBusy("#create-agent", false, "Add agent");
      }
    });

    app.querySelectorAll("[data-remove-agent]").forEach(function (button) {
      button.addEventListener("click", async function () {
        try {
          await api("/agents/" + button.dataset.removeAgent, { method: "DELETE" });
          viewAgents();
        } catch (err) { toast(err.message, "error"); }
      });
    });
  }

  // ------------------------------------------------------------------- keys
  async function viewKeys() {
    syncNav();
    app.className = "wrap";
    app.innerHTML = '<div class="loading">Loading keys…</div>';

    var creds;
    try { creds = (await api("/credentials")).credentials; }
    catch (err) { return handleLoadError(err); }

    app.innerHTML =
      '<div class="split"><div class="panel">' +
        '<div class="panel__head"><h2>Provider keys</h2>' +
        '<span class="panel__note">Encrypted before they are stored</span></div>' +
        '<div class="panel__body">' +
          (creds.length
            ? creds.map(function (c) {
                return '<div class="key-row"><span class="who"><b>' + esc(c.label) +
                  "</b><br><code>" + esc(c.provider) + " · " + esc(c.hint) + "</code>" +
                  (c.base_url ? "<br><code>" + esc(c.base_url) + "</code>" : "") +
                  '</span><button class="btn btn--danger btn--small" data-remove-key="' +
                  c.id + '">Delete</button></div>';
              }).join("")
            : '<div class="empty"><h3>No keys stored</h3><p style="margin:0">Add a key ' +
              "from any provider you want on the panel. Vendors bill your account " +
              "directly for every turn its model takes.</p></div>") +
        "</div></div>" +
        '<div class="panel"><div class="panel__head"><h2>Add a key</h2></div>' +
        '<div class="panel__body"><div id="key-form">' +
          '<div class="field"><label for="provider">Provider</label>' +
          '<select id="provider" name="provider">' + S.providers.map(function (p) {
            return '<option value="' + esc(p.key) + '">' + esc(p.label) + "</option>";
          }).join("") + "</select></div>" +
          '<div class="field"><label for="label">Label</label>' +
          '<input id="label" name="label" type="text" placeholder="Work account"></div>' +
          '<div class="field"><label for="api_key">API key</label>' +
          '<input id="api_key" name="api_key" type="password" autocomplete="off" ' +
          'spellcheck="false"></div>' +
          '<div class="field"><label for="base_url">Base URL</label>' +
          '<input id="base_url" name="base_url" type="text" ' +
          'placeholder="https://openrouter.ai/api/v1">' +
          '<p class="hint">Custom models only. Any OpenAI-compatible endpoint — ' +
          "Ollama, vLLM, OpenRouter, your own gateway.</p></div>" +
          '<button class="btn btn--primary" id="create-key" type="button" ' +
          'style="margin-top:16px">Save key</button>' +
        "</div></div></div></div>";

    on("#create-key", "click", async function () {
      setBusy("#create-key", true, "Saving…");
      try {
        await api("/credentials", { method: "POST", data: form("key-form") });
        toast("Key saved and encrypted.");
        viewKeys();
      } catch (err) {
        toast(err.message, "error");
        setBusy("#create-key", false, "Save key");
      }
    });

    app.querySelectorAll("[data-remove-key]").forEach(function (button) {
      button.addEventListener("click", async function () {
        try {
          await api("/credentials/" + button.dataset.removeKey, { method: "DELETE" });
          toast("Key deleted. Agents using it will need a new one.");
          viewKeys();
        } catch (err) { toast(err.message, "error"); }
      });
    });
  }

  // ------------------------------------------------------------------ room
  async function viewChat(groupId, discussionId) {
    syncNav();
    app.className = "wrap";
    app.innerHTML = '<div class="loading">Opening the room…</div>';

    var group, agents, discussion = null;
    try {
      group = (await api("/groups/" + groupId)).group;
      agents = (await api("/agents")).agents;
      var target = discussionId ||
        (group.discussions.length ? group.discussions[0].id : null);
      if (target) discussion = (await api("/discussions/" + target)).discussion;
    } catch (err) { return handleLoadError(err); }

    var seated = group.members;
    var seatedIds = seated.map(function (m) { return m.id; });
    var choosable = agents.filter(function (a) { return seatedIds.indexOf(a.id) === -1; });

    live.discussionId = discussion ? discussion.id : null;
    live.groupId = groupId;
    live.status = discussion ? discussion.status : "idle";
    live.mode = discussion ? discussion.mode : "step";

    app.innerHTML =
      '<a class="crumb" href="#/">← All chats</a>' +
      '<div class="group-head"><h1>' + esc(group.name) + "</h1>" +
        (group.purpose ? '<span style="color:var(--ink-soft)">' + esc(group.purpose) + "</span>" : "") +
        '<span class="seats-line">' + seated.map(function (m) {
          return '<span class="seat-tag"><i style="background:' + esc(m.color) + '"></i>' +
            esc(m.name) + "</span>";
        }).join("") + "</span></div>" +

      '<div class="split"><div>' +
        '<div class="transcript" id="transcript"></div>' +
        '<div class="room-controls" id="controls"></div>' +
        '<div class="panel composer-panel"><div class="panel__body">' +
          '<div class="composer" id="say-form">' +
            '<textarea id="message" rows="3" placeholder="' +
            (seated.length ? "Say something to the room…"
                           : "Seat an agent before you start talking") + '"' +
            (seated.length ? "" : " disabled") + "></textarea>" +
            '<div class="file-strip" id="file-strip"></div>' +
            '<div class="composer__bar">' +
              '<button class="btn btn--quiet btn--small" id="attach-btn" type="button"' +
              (seated.length ? "" : " disabled") + ">Attach files</button>" +
              '<input type="file" id="file-input" multiple hidden>' +
              '<div class="mode-switch" role="group" aria-label="Conversation mode">' +
                '<button type="button" data-mode="step" class="mode-btn">Step</button>' +
                '<button type="button" data-mode="flow" class="mode-btn">Flow</button>' +
              "</div>" +
              '<select id="reply-to" aria-label="Who replies">' +
                '<option value="all">Everyone replies</option>' +
                seated.map(function (m) {
                  return '<option value="agent:' + m.id + '">Only ' + esc(m.name) + "</option>";
                }).join("") +
                '<option value="none">Nobody — just post it</option>' +
              "</select>" +
              '<span class="stage" id="stage"><i></i><span id="stage-text"></span></span>' +
              '<span class="spacer"></span>' +
              '<button class="btn btn--quiet" id="stop-btn" type="button" hidden>Stop</button>' +
              '<button class="btn btn--primary" id="send-btn" type="button"' +
              (seated.length ? "" : " disabled") + ">Send</button>" +
            "</div>" +
            '<p class="hint" id="mode-hint"></p>' +
          "</div>" +
        "</div></div>" +
      "</div>" +

      '<div class="stack">' +
        '<div class="panel"><div class="panel__head"><h2>In this chat</h2></div>' +
          '<div class="panel__body">' +
            (seated.length
              ? '<ul class="roster roster--ordered">' + seated.map(function (m, i) {
                  return "<li><span class='rail' style='background:" + esc(m.color) +
                    "'></span><span class='who'><b>" + esc(m.name) + "</b><span>speaks " +
                    ordinal(i + 1) + " · " + esc(m.provider) + " · " + esc(m.model) +
                    "</span></span>" +
                    "<span class='seat-move'>" +
                      "<button type='button' data-move='up' data-agent='" + m.id + "'" +
                      (i === 0 ? " disabled" : "") + " aria-label='Move " +
                      esc(m.name) + " earlier'>&#9650;</button>" +
                      "<button type='button' data-move='down' data-agent='" + m.id + "'" +
                      (i === seated.length - 1 ? " disabled" : "") + " aria-label='Move " +
                      esc(m.name) + " later'>&#9660;</button>" +
                    "</span>" +
                    "<button class='btn btn--danger btn--small' " +
                    "data-remove-member='" + m.id + "'>Remove</button></li>";
                }).join("") + "</ul>" +
                '<p class="hint">Order matters. The first agent answers you alone; ' +
                "everyone after it also reads the turns taken just before, so whoever " +
                "speaks first frames the discussion.</p>"
              : '<p style="margin:0">Empty room. Add an agent below.</p>') +
            (choosable.length
              ? '<div class="inline-form" style="margin-top:14px">' +
                '<select id="add-agent-select" aria-label="Agent to add">' +
                choosable.map(function (a) {
                  return '<option value="' + a.id + '">' + esc(a.name) + " — " +
                    esc(a.provider) + "</option>";
                }).join("") + "</select>" +
                '<button class="btn btn--quiet" id="add-member" type="button">Add</button></div>'
              : '<p class="hint" style="margin-top:14px">' +
                '<a href="#/agents">Create another agent</a> to seat it here.</p>') +
          "</div></div>" +

        '<div class="panel"><div class="panel__head"><h2>Chat settings</h2></div>' +
          '<div class="panel__body"><div id="settings-form">' +
            '<div class="field"><label for="s-name">Name</label>' +
            '<input id="s-name" name="name" type="text" value="' + esc(group.name) + '"></div>' +
            '<div class="field"><label for="s-purpose">Purpose</label>' +
            '<input id="s-purpose" name="purpose" type="text" value="' +
            esc(group.purpose) + '"></div>' +
            '<button class="btn btn--quiet" id="save-settings" type="button" ' +
            'style="margin-top:14px">Save changes</button>' +
          "</div></div></div>" +

        '<div class="panel"><div class="panel__head"><h2>Topics</h2>' +
          '<button class="linklike" id="new-topic" type="button">New</button></div>' +
          '<div class="panel__body">' +
            (group.discussions.length
              ? '<div class="card-list">' + group.discussions.map(function (d) {
                  return '<a href="#/groups/' + group.id + "/d/" + d.id + '"' +
                    (discussion && d.id === discussion.id ? ' class="current"' : "") +
                    "><b>" + esc(d.question.slice(0, 70)) +
                    (d.question.length > 70 ? "…" : "") + "</b><span>" +
                    esc(d.mode === "flow" ? "flow" : "step") + " · " +
                    '<span class="status-dot" data-status="' + esc(d.status) + '">' +
                    esc(d.status) + "</span></span></a>";
                }).join("") + "</div>"
              : '<p style="margin:0">Nothing yet. Type below to start.</p>') +
          "</div></div>" +

        '<button class="btn btn--danger btn--small" id="delete-group" type="button">' +
        "Delete chat</button>" +
      "</div></div>";

    setMode(live.mode);
    if (discussion) renderConversation(discussion);
    refreshControls();

    // ---- wiring ----------------------------------------------------------
    app.querySelectorAll(".mode-btn").forEach(function (button) {
      button.addEventListener("click", function () { setMode(button.dataset.mode); });
    });

    renderFiles(discussion ? discussion.files : []);

    on("#attach-btn", "click", function () {
      document.getElementById("file-input").click();
    });

    on("#file-input", "change", async function (e) {
      var chosen = Array.prototype.slice.call(e.target.files || []);
      if (!chosen.length) return;
      e.target.value = "";

      var data = new FormData();
      chosen.forEach(function (file) { data.append("file", file); });
      if (live.discussionId) data.append("discussion_id", live.discussionId);

      setBusy("#attach-btn", true, "Uploading…");
      try {
        var result = await upload("/groups/" + groupId + "/files", data);
        var wasNew = !live.discussionId;
        live.discussionId = result.discussion_id;
        if (wasNew) {
          history.replaceState({}, "", "#/chats/" + groupId + "/t/" + live.discussionId);
        }
        var fresh = (await api("/discussions/" + live.discussionId)).discussion;
        renderConversation(fresh);
        renderFiles(fresh.files);
        refreshControls();
        result.files.forEach(function (f) {
          if (!f.readable) toast(f.filename + ": " + f.note, "error");
        });
      } catch (err) {
        toast(err.message, "error");
      }
      setBusy("#attach-btn", false, "Attach files");
    });

    document.getElementById("file-strip").addEventListener("click", async function (e) {
      var remove = e.target.closest("[data-remove-file]");
      if (!remove) return;
      try {
        await api("/files/" + remove.dataset.removeFile, { method: "DELETE" });
        var fresh = (await api("/discussions/" + live.discussionId)).discussion;
        renderFiles(fresh.files);
        toast("File removed. The agents will not see it again.");
      } catch (err) { toast(err.message, "error"); }
    });

    on("#send-btn", "click", send);
    on("#message", "keydown", function (e) {
      // Enter sends, Shift+Enter makes a new line — as in any chat.
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
    });

    on("#stop-btn", "click", async function () {
      if (!live.discussionId) return;
      try { await api("/discussions/" + live.discussionId + "/cancel", { method: "POST" }); }
      catch (err) { toast(err.message, "error"); }
    });

    on("#new-topic", "click", function () {
      live.discussionId = null;
      live.status = "idle";
      document.getElementById("transcript").innerHTML =
        '<div class="empty"><h3>New topic</h3><p style="margin:0">' +
        "Type below. Everyone seated reads what you write.</p></div>";
      refreshControls();
      history.replaceState({}, "", "#/chats/" + groupId);
      document.getElementById("message").focus();
    });

    on("#add-member", "click", async function () {
      var id = document.getElementById("add-agent-select").value;
      try {
        await api("/groups/" + groupId + "/members", {
          method: "POST", data: { agent_id: Number(id) },
        });
        viewChat(groupId, live.discussionId);
      } catch (err) { toast(err.message, "error"); }
    });

    app.querySelectorAll("[data-move]").forEach(function (button) {
      button.addEventListener("click", async function () {
        var order = seated.map(function (m) { return m.id; });
        var from = order.indexOf(Number(button.dataset.agent));
        var to = button.dataset.move === "up" ? from - 1 : from + 1;
        if (from < 0 || to < 0 || to >= order.length) return;
        order.splice(to, 0, order.splice(from, 1)[0]);
        try {
          await api("/groups/" + groupId + "/members/order", {
            method: "PATCH", data: { agent_ids: order },
          });
          viewChat(groupId, live.discussionId);
        } catch (err) { toast(err.message, "error"); }
      });
    });

    app.querySelectorAll("[data-remove-member]").forEach(function (button) {
      button.addEventListener("click", async function () {
        try {
          await api("/groups/" + groupId + "/members/" + button.dataset.removeMember,
                    { method: "DELETE" });
          viewChat(groupId, live.discussionId);
        } catch (err) { toast(err.message, "error"); }
      });
    });

    on("#save-settings", "click", async function () {
      setBusy("#save-settings", true, "Saving…");
      try {
        await api("/groups/" + groupId, { method: "PATCH", data: form("settings-form") });
        toast("Chat updated.");
        viewChat(groupId, live.discussionId);
      } catch (err) {
        toast(err.message, "error");
        setBusy("#save-settings", false, "Save changes");
      }
    });

    on("#delete-group", "click", async function () {
      if (!confirm("Delete this chat and everything in it?")) return;
      try {
        await api("/groups/" + groupId, { method: "DELETE" });
        toast("Chat deleted.");
        go("/");
      } catch (err) { toast(err.message, "error"); }
    });

    // Retry a failed turn, from the message itself.
    document.getElementById("transcript").addEventListener("click", async function (e) {
      var button = e.target.closest("[data-retry]");
      if (!button) return;
      button.disabled = true;
      button.textContent = "Retrying…";
      try {
        await api("/discussions/" + live.discussionId + "/retry/" + button.dataset.retry,
                  { method: "POST" });
        // The old error message is replaced, so redraw from scratch.
        var fresh = (await api("/discussions/" + live.discussionId)).discussion;
        renderConversation(fresh);
        listen(live.discussionId);
      } catch (err) {
        toast(err.message, "error");
        button.disabled = false;
        button.textContent = "Try again";
      }
    });

    if (discussion && (discussion.status === "running" || discussion.status === "queued")) {
      listen(discussion.id);
    }
  }

  // ---- mode ---------------------------------------------------------------
  function setMode(mode) {
    live.mode = mode === "flow" ? "flow" : "step";
    app.querySelectorAll(".mode-btn").forEach(function (button) {
      button.classList.toggle("active", button.dataset.mode === live.mode);
    });
    var replySelect = document.getElementById("reply-to");
    var hint = document.getElementById("mode-hint");
    if (replySelect) replySelect.hidden = live.mode === "flow";
    if (hint) {
      hint.textContent = live.mode === "flow"
        ? "The agents talk to each other continuously. You can type at any time — the next speaker will answer you. Press Stop when you have enough."
        : "Everyone speaks once, then the room waits for you.";
    }
  }

  // ---- sending ------------------------------------------------------------
  async function send() {
    var box = document.getElementById("message");
    var text = box.value.trim();
    if (!text) { box.focus(); return; }

    var replySelect = document.getElementById("reply-to");
    var payload = {
      mode: live.mode,
      reply: live.mode === "flow" ? "all" : (replySelect ? replySelect.value : "all"),
    };

    setBusy("#send-btn", true, "Sending…");
    try {
      if (!live.discussionId) {
        payload.question = text;
        var created = await api("/groups/" + live.groupId + "/ask", {
          method: "POST", data: payload,
        });
        live.discussionId = created.discussion.id;
        live.lastId = 0;
        document.getElementById("transcript").innerHTML = "";
        history.replaceState({}, "", "#/chats/" + live.groupId + "/t/" + live.discussionId);
      } else {
        payload.text = text;
        var said = await api("/discussions/" + live.discussionId + "/say", {
          method: "POST", data: payload,
        });
        if (said.message) {
          live.lastId = Math.max(live.lastId, said.message.id);
          appendMessage(said.message);
        }
        if (said.interjected) {
          // A flow is already running; it will pick the message up.
          box.value = "";
          setBusy("#send-btn", false, "Send");
          return;
        }
      }
      box.value = "";
      listen(live.discussionId);
    } catch (err) {
      toast(err.message, "error");
      setBusy("#send-btn", false, "Send");
    }
  }

  async function continueRoom(mode, reply) {
    try {
      await api("/discussions/" + live.discussionId + "/continue", {
        method: "POST", data: { mode: mode, reply: reply || "all" },
      });
      setMode(mode);
      listen(live.discussionId);
    } catch (err) { toast(err.message, "error"); }
  }

  // ---- controls between turns ---------------------------------------------
  function refreshControls() {
    var box = document.getElementById("controls");
    if (!box) return;
    if (!live.discussionId || live.status === "running" || live.status === "queued") {
      box.innerHTML = "";
      return;
    }
    box.innerHTML =
      '<span class="count-note">Room is waiting.</span>' +
      '<button class="btn btn--quiet btn--small" data-act="step">Let them go once more</button>' +
      '<button class="btn btn--quiet btn--small" data-act="flow">' +
      (live.mode === "flow" ? "Resume flow" : "Switch to flow") + "</button>";

    box.querySelectorAll("[data-act]").forEach(function (button) {
      button.addEventListener("click", function () {
        continueRoom(button.dataset.act);
      });
    });
  }

  // ------------------------------------------------------------- transcript
  function renderConversation(discussion) {
    var box = document.getElementById("transcript");
    box.innerHTML = "";
    live.lastId = 0;
    live.status = discussion.status;
    live.mode = discussion.mode || "step";
    (discussion.messages || []).forEach(function (m) {
      live.lastId = Math.max(live.lastId, m.id);
      appendMessage(m);
    });
    if (discussion.error) {
      var warn = document.createElement("div");
      warn.className = "flash flash--error";
      warn.style.marginTop = "20px";
      warn.textContent = discussion.error;
      box.appendChild(warn);
    }
  }

  function appendMessage(m) {
    var box = document.getElementById("transcript");
    if (!box) return;
    var node = document.createElement("div");

    if (m.role === "question") {
      node.className = "turn turn--question";
      node.innerHTML =
        '<div class="turn__who" style="--speaker:#172033"><b>' + esc(m.speaker) +
        "</b><span>you</span></div>" +
        '<div class="turn__body">' + esc(m.content) + "</div>";
    } else if (m.role === "note") {
      node.className = "room-note";
      node.textContent = m.content;
    } else if (m.role === "error") {
      node.className = "turn turn--error";
      node.innerHTML =
        '<div class="turn__who" style="--speaker:' + esc(m.color) + '"><b>' +
        esc(m.speaker) + "</b><span>" + esc(m.provider) + " · " + esc(m.model) +
        "</span></div><div class='turn__body'>" + esc(m.content) +
        '<div><button class="linklike" data-retry="' + m.id + '">Try again</button></div>' +
        "</div>";
    } else {
      // "turn", and "verdict" from conversations made before rooms existed.
      node.className = "turn";
      node.innerHTML =
        '<div class="turn__who" style="--speaker:' + esc(m.color) + '"><b>' +
        esc(m.speaker) + "</b><span>" + esc(m.provider) + " · " + esc(m.model) + "</span>" +
        (m.input_chars ? '<div class="turn__meta">read ' + m.input_chars +
                         " chars of the room</div>" : "") +
        "</div><div class='turn__body'>" + m.html + "</div>";
    }

    box.appendChild(node);
    renderMath(node);
    node.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  // ---- live updates -------------------------------------------------------
  function setLive(on, text) {
    var stage = document.getElementById("stage");
    var stageText = document.getElementById("stage-text");
    var sendBtn = document.getElementById("send-btn");
    var stopBtn = document.getElementById("stop-btn");
    if (!stage) return;
    stage.dataset.live = on ? "1" : "0";
    stageText.textContent = text || "";
    if (stopBtn) stopBtn.hidden = !on;
    if (sendBtn) {
      // In flow mode you can keep typing while they talk; in step mode the
      // room is busy and a second message would arrive out of order.
      var blocked = on && live.mode !== "flow";
      sendBtn.disabled = blocked;
      sendBtn.textContent = on && live.mode === "flow" ? "Interject" : "Send";
    }
  }

  function stopStreaming() {
    if (live.source) { live.source.close(); live.source = null; }
    if (live.poller) { clearInterval(live.poller); live.poller = null; }
  }

  function listen(discussionId) {
    stopStreaming();
    live.status = "running";
    setLive(true, "Starting");
    refreshControls();

    if (window.EventSource) {
      var url = API + "/discussions/" + discussionId + "/stream?after=" + live.lastId;
      live.source = new EventSource(url, { withCredentials: true });
      live.source.addEventListener("message", function (e) {
        var m = JSON.parse(e.data);
        live.lastId = Math.max(live.lastId, m.id);
        appendMessage(m);
      });
      live.source.addEventListener("stage", function (e) {
        setLive(true, JSON.parse(e.data).stage);
      });
      live.source.addEventListener("end", function (e) {
        var data = JSON.parse(e.data);
        finish(data.status, data.error);
      });
      live.source.onerror = function () {
        // A proxy dropped the stream. Fall back to polling.
        if (live.source) { live.source.close(); live.source = null; }
        if (!live.poller) poll(discussionId);
      };
    } else {
      poll(discussionId);
    }
  }

  function poll(discussionId) {
    var tick = async function () {
      try {
        var data = await api("/discussions/" + discussionId +
                             "/messages?after=" + live.lastId);
        data.messages.forEach(function (m) {
          live.lastId = Math.max(live.lastId, m.id);
          appendMessage(m);
        });
        setLive(true, data.stage);
        if (["idle", "done", "failed", "cancelled"].indexOf(data.status) !== -1) {
          finish(data.status, data.error);
        }
      } catch (err) { /* keep polling */ }
    };
    live.poller = setInterval(tick, 1500);
    tick();
  }

  function finish(status, error) {
    stopStreaming();
    live.status = status === "failed" ? "failed" : "idle";
    setLive(false);
    setBusy("#send-btn", false, "Send");
    refreshControls();
    if (error) toast(error, "error");
    var box = document.getElementById("message");
    if (box && !box.disabled) box.focus();
  }

  // ------------------------------------------------------------------- boot
  function handleLoadError(err) {
    if (err.status === 401) {
      S.user = null;
      syncNav();
      return go("/signin");
    }
    if (err.status === 404) {
      app.innerHTML = '<div class="empty"><h3>Not found</h3><p style="margin:0">' +
        'That group no longer exists. <a href="#/">Back to your groups</a>.</p></div>';
      return;
    }
    app.innerHTML = '<div class="empty"><h3>Could not load</h3><p>' + esc(err.message) +
      '</p><p style="margin:0"><a href="#/">Try again</a></p></div>';
  }

  async function refreshSession() {
    var data = await api("/session");
    S.csrf = data.csrf_token;
    S.user = data.authenticated ? data.user : null;
    S.providers = data.providers || [];
    S.suggested = data.suggested_models || {};
    S.limits = data.limits || S.limits;
    S.googleEnabled = !!data.google_enabled;
    syncNav();
  }

  document.getElementById("signout").addEventListener("click", async function () {
    try { await api("/auth/logout", { method: "POST" }); } catch (err) { /* ignore */ }
    S.user = null;
    await refreshSession();
    go("/");
  });

  window.addEventListener("hashchange", route);
  window.addEventListener("load", function () { renderMath(app); });

  function consumeAuthError() {
    var params = new URLSearchParams(location.search);
    var message = params.get("auth_error");
    if (!message) return;
    toast(message, "error");
    // Strip it so a refresh does not show the same message again.
    history.replaceState({}, "", location.pathname + location.hash);
  }

  (async function boot() {
    consumeAuthError();
    try {
      await refreshSession();
    } catch (err) {
      app.innerHTML = '<div class="empty"><h3>Backend unreachable</h3>' +
        "<p>The site loaded, but nothing is answering at <code>" + esc(API) +
        "</code>.</p><p style='margin:0'>Check that the backend is running on port " +
        "30303 and that Apache is proxying <code>/api</code> to it.</p></div>";
      return;
    }
    route();
  })();
})();
