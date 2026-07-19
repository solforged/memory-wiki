(function () {
  "use strict";

  const PLUGIN_NAME = "memory-wiki";
  const NATIVE_BASE = "/api/plugins/memory-wiki";
  const LEGACY_BASE = "http://127.0.0.1:9119/api/plugins/memory-wiki";

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }

  function append(parent) {
    for (let index = 1; index < arguments.length; index += 1) {
      const child = arguments[index];
      if (child) parent.appendChild(child);
    }
    return parent;
  }

  function clear(root) {
    while (root.firstChild) root.removeChild(root.firstChild);
  }

  function number(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
  }

  function formatTime(value) {
    if (!value) return "Never";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(date);
  }

  function badge(text, tone) {
    return node("span", "mw-badge" + (tone ? " mw-badge-" + tone : ""), text);
  }

  function card(title, content, trailing) {
    const shell = node("section", "mw-card");
    const heading = node("div", "mw-card-heading");
    append(heading, node("h2", "mw-card-title", title), trailing);
    append(shell, heading, content);
    return shell;
  }

  function stat(label, value, note) {
    const shell = node("article", "mw-card mw-stat");
    append(
      shell,
      node("div", "mw-stat-label", label),
      node("div", "mw-stat-value", number(value).toLocaleString()),
      node("div", "mw-stat-note", note),
    );
    return shell;
  }

  function empty(text) {
    return node("div", "mw-empty", text);
  }

  function shellQuote(value) {
    return "'" + String(value || "").replace(/'/g, "'\"'\"'") + "'";
  }

  function copyButton(label, command) {
    const button = node("button", "mw-button mw-button-secondary", label);
    button.type = "button";
    button.addEventListener("click", function () {
      if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") return;
      navigator.clipboard.writeText(command).then(function () {
        const original = button.textContent;
        button.textContent = "Copied";
        window.setTimeout(function () { button.textContent = original; }, 1200);
      });
    });
    return button;
  }

  function itemList(items, emptyText) {
    if (!Array.isArray(items) || items.length === 0) return empty(emptyText);
    const list = node("ul", "mw-list");
    items.slice(0, 12).forEach(function (item) {
      const row = node("li", "mw-list-item");
      const title = item && (item.title || item.ref);
      const detail = item && (item.summary || item.detail || item.reason);
      if (title) row.appendChild(node("div", "mw-item-title", title));
      row.appendChild(node("div", "mw-item-detail", detail || "No details available."));
      list.appendChild(row);
    });
    return list;
  }

  function renderLoading(root) {
    clear(root);
    const page = node("div", "mw-dashboard");
    const header = node("header", "mw-header");
    append(
      header,
      append(
        node("div"),
        node("h1", "mw-title", "Memory wiki"),
        node("p", "mw-subtitle", "Loading claim health and dream state."),
      ),
    );
    const skeleton = node("div", "mw-loading");
    for (let index = 0; index < 4; index += 1) {
      skeleton.appendChild(node("div", "mw-skeleton"));
    }
    append(page, header, skeleton);
    root.appendChild(page);
  }

  function renderError(root, message, retry, legacy) {
    clear(root);
    const page = node("div", "mw-dashboard");
    const header = node("header", "mw-header");
    const copy = legacy
      ? "The native memory API on port 9119 could not be reached from the legacy dashboard. Open the native Hermes dashboard, ensure memory-wiki is enabled, or check its service."
      : "The memory-wiki backend did not return an overview. Restart the Hermes dashboard after enabling the plugin, then try again.";
    const action = node("button", "mw-button", "Try again");
    action.type = "button";
    action.addEventListener("click", retry);
    append(
      header,
      append(node("div"), node("h1", "mw-title", "Memory wiki"), node("p", "mw-subtitle", copy)),
      action,
    );
    append(page, header, node("div", "mw-alert", message || "Overview unavailable."));
    root.appendChild(page);
  }

  function renderHealth(data) {
    const claims = data.claims || {};
    const categories = [
      {
        key: "contradictions",
        label: "Contradictions",
        count: number(claims.contradiction_count),
        items: claims.contradictions,
        empty: "No contradictions were reported.",
      },
      {
        key: "low_confidence",
        label: "Low confidence",
        count: number(claims.low_confidence_count),
        items: claims.low_confidence,
        empty: "No low-confidence claims were reported.",
      },
      {
        key: "stale",
        label: "Stale",
        count: number(claims.stale_count),
        items: claims.stale,
        empty: "No stale pages were reported.",
      },
      {
        key: "provenance_gaps",
        label: "Provenance gaps",
        count: number(claims.provenance_gap_count),
        items: claims.provenance_gaps,
        empty: "No provenance gaps were reported.",
      },
    ];
    let active = categories.find(function (entry) { return entry.count > 0; }) || categories[0];
    const content = node("div");
    const tabs = node("div", "mw-health-tabs");
    tabs.setAttribute("role", "tablist");
    const panel = node("div");

    function show(next) {
      active = next;
      Array.from(tabs.children).forEach(function (button) {
        button.setAttribute("aria-selected", button.dataset.key === active.key ? "true" : "false");
      });
      clear(panel);
      panel.appendChild(itemList(active.items, active.empty));
    }

    categories.forEach(function (entry) {
      const button = node("button", "mw-health-tab", entry.label + " " + entry.count);
      button.type = "button";
      button.dataset.key = entry.key;
      button.setAttribute("role", "tab");
      button.addEventListener("click", function () { show(entry); });
      tabs.appendChild(button);
    });
    append(content, tabs, panel);
    show(active);
    const issueCount = categories.reduce(function (sum, entry) { return sum + entry.count; }, 0);
    return card("Claim health", content, badge(issueCount ? issueCount + " issue(s)" : "Healthy", issueCount ? "warn" : "good"));
  }

  function renderPhases(dream) {
    const phases = Array.isArray(dream.phases) ? dream.phases : [];
    const grid = node("div", "mw-phase-grid");
    ["light", "rem", "deep"].forEach(function (name) {
      const phase = phases.find(function (item) { return String(item.name).toLowerCase() === name; }) || { name: name, status: "never" };
      const shell = node("article", "mw-phase");
      const status = String(phase.status || "never").toLowerCase();
      const tone = status === "running" ? "warn" : status === "complete" ? "good" : "";
      const metric = name === "deep"
        ? number(phase.candidate_count) + " candidates"
        : number(phase.hits) + " hits";
      append(
        shell,
        append(node("div", "mw-phase-head"), node("span", "mw-phase-name", name), badge(status, tone)),
        node("div", "mw-phase-count", metric),
        node("div", "mw-meta", "Last run: " + formatTime(phase.last_run_at)),
      );
      grid.appendChild(shell);
    });
    return card("Dream phases", grid, dream.exists ? badge("State tracked", "good") : badge("No state"));
  }

  function renderRuns(dream) {
    const runs = Array.isArray(dream.recent_runs) ? dream.recent_runs : [];
    if (runs.length === 0) return card("Recent sweeps", empty("No light, REM, or deep runs have been recorded."));
    const list = node("div", "mw-run-list");
    runs.slice(0, 8).forEach(function (run) {
      const row = node("div", "mw-run");
      append(
        row,
        node("div", "mw-run-phase", run.phase || "dream"),
        append(node("div"), node("div", "mw-item-title", formatTime(run.at)), node("div", "mw-item-detail", run.summary || "Completed")),
        badge(run.status || "complete", run.status === "failed" ? "bad" : "good"),
      );
      list.appendChild(row);
    });
    return card("Recent sweeps", list);
  }

  function keyValue(rows) {
    const list = node("div", "mw-kv");
    rows.forEach(function (row) {
      append(
        list,
        append(node("div", "mw-kv-row"), node("span", "mw-kv-key", row[0]), node("span", "mw-kv-value", row[1])),
      );
    });
    return list;
  }

  function renderRuntime(data) {
    const compiler = data.compiler || {};
    const qmd = data.qmd || {};
    const scheduler = data.scheduler || {};
    const job = scheduler.job || {};
    const reports = data.reports || {};
    const reportCount = Object.keys(reports).filter(function (key) { return reports[key] && reports[key].exists; }).length;
    const content = keyValue([
      ["Wiki", data.wiki && data.wiki.exists ? data.wiki.root : "Not found"],
      ["Compiled", compiler.generated_at ? formatTime(compiler.generated_at) : "Never"],
      ["Pages", number(compiler.page_count).toLocaleString()],
      ["Health reports", reportCount + " / " + Object.keys(reports).length],
      ["Recall backend", qmd.backend || "local"],
      ["QMD mode", qmd.configured ? (qmd.search_mode || "search") : "Not configured"],
      ["QMD index", qmd.initialized ? "Ready" : qmd.configured ? "Not initialized" : "Inactive"],
      ["QMD updated", qmd.updated_at ? formatTime(qmd.updated_at) : "Never"],
      ["Dream schedule", scheduler.enabled ? (scheduler.schedule || "Enabled") : "Disabled"],
      ["Managed job", job.name ? (job.state || (job.enabled ? "Scheduled" : "Disabled")) : "Not found"],
      ["Schedule status", scheduler.error || (scheduler.in_sync ? "In sync" : scheduler.enabled ? "Needs reconcile" : job.name ? "Managed job remains" : "Inactive")],
      ["Scheduled last run", job.last_run_at ? formatTime(job.last_run_at) : "Never"],
      ["Scheduled next run", job.next_run_at ? formatTime(job.next_run_at) : "Not scheduled"],
    ]);
    const runtimeWarning = (qmd.configured && (!qmd.available || !qmd.initialized))
      || (scheduler.enabled && !scheduler.in_sync);
    const tone = runtimeWarning ? "warn" : "good";
    return card("Compiler and recall", content, badge(compiler.available ? "Compiler ready" : "No artifacts", compiler.available ? tone : "warn"));
  }

  function renderReview(review) {
    if (!review || !review.ok) {
      return card("Review queue", empty("Candidate details are unavailable. The overview remains read-only and usable."), badge("Unavailable", "warn"));
    }
    const candidates = Array.isArray(review.candidates) ? review.candidates : [];
    if (candidates.length === 0) {
      return card("Review queue", empty("No pending or approved candidates are waiting."), badge("Clear", "good"));
    }
    const grid = node("div", "mw-candidate-grid");
    candidates.forEach(function (candidate) {
      const shell = node("article", "mw-candidate");
      const head = node("div", "mw-candidate-head");
      const badges = node("div", "mw-candidate-badges");
      append(
        badges,
        badge(candidate.status || "unknown", candidate.status === "approved" ? "good" : "warn"),
        badge(candidate.kind || "unknown"),
        badge(candidate.confidence || "unknown"),
        candidate.stale ? badge("stale", "bad") : null,
      );
      append(
        head,
        append(node("div"), node("div", "mw-item-title", candidate.title || candidate.id || "Candidate"), node("div", "mw-meta", candidate.id || "No candidate id")),
        badges,
      );
      const content = node("div", "mw-candidate-content", candidate.content || "No candidate content.");
      const metadata = [];
      if (candidate.target) metadata.push("Target: " + candidate.target);
      if (candidate.origin) metadata.push("Origin: " + candidate.origin);
      if (candidate.created) metadata.push("Created: " + candidate.created);
      if (candidate.session_hash) metadata.push("Session: " + candidate.session_hash);
      const actions = node("div", "mw-candidate-actions");
      if (candidate.id) {
        append(
          actions,
          copyButton("Copy approve command", "hermes memory-wiki review --approve " + shellQuote(candidate.id)),
          copyButton("Copy reject template", "hermes memory-wiki review --reject " + shellQuote(candidate.id) + " --reason " + shellQuote("not useful")),
        );
      }
      append(
        shell,
        head,
        content,
        candidate.content_truncated ? node("div", "mw-meta", "Preview truncated.") : null,
        metadata.length ? node("div", "mw-candidate-meta", metadata.join(" · ")) : null,
        actions,
      );
      grid.appendChild(shell);
    });
    const summary = review.summary || {};
    const trailing = append(
      node("div", "mw-actions"),
      badge("Read only"),
      badge(number(summary.matched) + " waiting", number(summary.stale) ? "warn" : "good"),
    );
    return card("Review queue", grid, trailing);
  }

  function renderMaintenance(data) {
    const reports = data.reports || {};
    const categories = [
      ["open-questions", "Open questions"],
      ["privacy-review", "Privacy review"],
      ["claim-health", "Claim health notes"],
    ];
    const content = node("div", "mw-maintenance-grid");
    let total = 0;
    categories.forEach(function (pair) {
      const report = reports[pair[0]] || {};
      const shell = node("section", "mw-maintenance-section");
      total += number(report.count);
      append(
        shell,
        append(node("div", "mw-row-head"), node("div", "mw-item-title", pair[1]), badge(number(report.count))),
        itemList(report.items, "Nothing reported."),
      );
      content.appendChild(shell);
    });
    return card("Maintenance inbox", content, badge(total ? total + " item(s)" : "Clear", total ? "warn" : "good"));
  }

  function renderHistory(history) {
    if (!history || !history.ok) {
      return card("Recent memory activity", empty("Lifecycle history is unavailable."), badge("Unavailable", "warn"));
    }
    const events = Array.isArray(history.events) ? history.events : [];
    if (events.length === 0) {
      return card("Recent memory activity", empty("No lifecycle events have been recorded."), badge("No events"));
    }
    const list = node("div", "mw-history-list");
    events.slice(0, 16).forEach(function (event) {
      const row = node("div", "mw-history-item");
      const identity = event.id || (event.detail && event.detail.ref) || "Direct write";
      const details = [];
      if (event.target) details.push(event.target);
      if (event.actor) details.push("by " + event.actor);
      if (event.detail && event.detail.reason) details.push(event.detail.reason);
      append(
        row,
        badge(event.action || "event", event.action === "rejected" ? "bad" : event.action === "promoted" ? "good" : ""),
        append(node("div"), node("div", "mw-item-title", identity), node("div", "mw-item-detail", details.join(" · ") || "No details")),
        node("time", "mw-meta", formatTime(event.ts)),
      );
      list.appendChild(row);
    });
    return card("Recent memory activity", list, badge("Read only"));
  }

  function renderOverview(root, data, reload, extras) {
    clear(root);
    const page = node("div", "mw-dashboard");
    const compiler = data.compiler || {};
    const claims = data.claims || {};
    const candidates = data.candidates || {};
    const dream = data.dream || {};
    extras = extras || {};

    const refresh = node("button", "mw-button", "Refresh");
    refresh.type = "button";
    refresh.addEventListener("click", reload);
    const status = compiler.available ? badge("Compiled", "good") : badge("Awaiting compiler", "warn");
    const header = node("header", "mw-header");
    append(
      header,
      append(
        node("div"),
        node("h1", "mw-title", "Memory wiki"),
        node("p", "mw-subtitle", "Claim health, staged promotion, dream phases, and recall state."),
      ),
      append(node("div", "mw-actions"), status, refresh),
    );
    page.appendChild(header);

    if (!data.ok) {
      page.appendChild(node("div", "mw-alert", data.error || "Overview unavailable."));
      root.appendChild(page);
      return;
    }
    if (!data.wiki || !data.wiki.exists) {
      page.appendChild(node("div", "mw-alert", "The configured wiki root was not found. Check plugins.memory-wiki.wiki_root."));
    }
    if (Array.isArray(data.issues) && data.issues.length) {
      page.appendChild(node("div", "mw-alert", data.issues.join(" ")));
    }

    const stats = node("section", "mw-status-grid");
    append(
      stats,
      stat("Claims", claims.total, compiler.available ? "Normalized compiler claims" : "Compiler cache is empty"),
      stat("Contradictions", claims.contradiction_count, "Claims needing reconciliation"),
      stat("Low confidence", claims.low_confidence_count, "Claims needing stronger evidence"),
      stat("Stale", claims.stale_count, "Pages beyond freshness policy"),
      stat("Pending", candidates.pending, "Candidates awaiting review"),
    );
    page.appendChild(stats);

    const topColumns = node("section", "mw-columns");
    append(topColumns, renderHealth(data), renderRuntime(data));
    page.appendChild(topColumns);
    page.appendChild(renderReview(extras.review));
    page.appendChild(renderMaintenance(data));
    page.appendChild(renderHistory(extras.history));
    page.appendChild(renderPhases(dream));
    page.appendChild(renderRuns(dream));

    if (!compiler.available) {
      page.appendChild(card("Compiler artifacts", empty("No digest, claim cache, or health reports exist yet. Run the memory-wiki compiler to populate this view.")));
    }
    root.appendChild(page);
  }

  function nativeMount() {
    const SDK = window.__HERMES_PLUGIN_SDK__;
    const registry = window.__HERMES_PLUGINS__;
    if (!SDK || !registry || typeof registry.register !== "function") return false;
    const React = SDK.React;
    const useEffect = SDK.hooks.useEffect;
    const useRef = SDK.hooks.useRef;

    function MemoryWikiPage() {
      const rootRef = useRef(null);
      useEffect(function () {
        let active = true;
        function load() {
          if (!rootRef.current) return;
          renderLoading(rootRef.current);
          Promise.all([
            SDK.fetchJSON(NATIVE_BASE + "/overview"),
            SDK.fetchJSON(NATIVE_BASE + "/review").catch(function () { return null; }),
            SDK.fetchJSON(NATIVE_BASE + "/history").catch(function () { return null; }),
          ])
            .then(function (payload) {
              if (active && rootRef.current) {
                renderOverview(rootRef.current, payload[0], load, {
                  review: payload[1],
                  history: payload[2],
                });
              }
            })
            .catch(function (error) {
              if (active && rootRef.current) renderError(rootRef.current, error && error.message, load, false);
            });
        }
        load();
        return function () { active = false; };
      }, []);
      return React.createElement("div", { ref: rootRef });
    }

    registry.register(PLUGIN_NAME, MemoryWikiPage);
    return true;
  }

  function standaloneMount() {
    function start() {
      const root = document.getElementById("memoryWikiRoot");
      if (!root) return;
      function load(showLoading) {
        if (showLoading !== false) renderLoading(root);
        function request(path) {
          return fetch(LEGACY_BASE + path, { credentials: "omit", cache: "no-store" })
            .then(function (response) {
              if (!response.ok) throw new Error("Native API returned HTTP " + response.status + ".");
              return response.json();
            });
        }
        Promise.all([
          request("/overview"),
          request("/review").catch(function () { return null; }),
          request("/history").catch(function () { return null; }),
        ])
          .then(function (response) {
            renderOverview(root, response[0], load, {
              review: response[1],
              history: response[2],
            });
          })
          .catch(function (error) { renderError(root, error && error.message, load, true); });
      }
      load(true);
      window.setInterval(function () { load(false); }, 30000);
    }
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", start, { once: true });
    } else {
      start();
    }
  }

  if (!nativeMount()) standaloneMount();
})();
