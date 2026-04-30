/**
 * Taiwan Strait EWS — Dashboard
 * Reads state.json and renders all indicators.
 * Auto-refreshes every 60 seconds.
 */

const PRIMARY_IDS = [1, 2, 3, 4, 5, 6];
const SECONDARY_IDS = [7, 8, 9, 10];
const REFRESH_MS = 60_000;

const ACTION_GUIDANCE = {
  green: "",
  yellow: "Review departure plans. Confirm passports, cash, and go-bag are ready. Check flight availability.",
  amber: "Book flexible flights out. Move important documents to carry-on. Alert family and contacts outside Taiwan.",
  red: "If flights are available, go to the airport now. If not, move to a safe location away from military targets and follow Taiwan civil defense guidance.",
};

async function fetchState() {
  try {
    const resp = await fetch("state.json", { cache: "no-store" });
    if (!resp.ok) return null;
    return await resp.json();
  } catch {
    return null;
  }
}

function countActive(state) {
  const indicators = state.indicators || {};
  let active = 0;
  let total = 0;
  let noData = 0;
  for (const [, ind] of Object.entries(indicators)) {
    total++;
    if (!ind.feed_healthy) noData++;
    else if (ind.active) active++;
  }
  return { active, total, noData };
}

function renderBanner(state) {
  const banner = document.getElementById("state-banner");
  const label = document.getElementById("action-label");
  const detail = document.getElementById("state-detail");
  const convergence = document.getElementById("convergence-count");
  const guidance = document.getElementById("action-guidance");
  const degraded = document.getElementById("degraded-badge");
  const updated = document.getElementById("last-updated");
  const since = document.getElementById("state-since");

  banner.className = state.alert_state;
  label.textContent = state.alert_label;
  detail.textContent = state.score_detail;

  // Convergence count
  const counts = countActive(state);
  let countText = `${counts.active} of ${counts.total} signals active`;
  if (counts.noData > 0) {
    countText += ` \u00b7 ${counts.noData} with no data`;
  }
  convergence.textContent = countText;

  // Action guidance
  const guidanceText = ACTION_GUIDANCE[state.alert_state] || "";
  if (guidanceText) {
    guidance.textContent = guidanceText;
    guidance.classList.remove("hidden");
  } else {
    guidance.classList.add("hidden");
  }

  // Degraded
  if (state.degraded) {
    degraded.textContent = "DEGRADED \u2014 no data from: " + state.degraded_feeds.join(", ");
    degraded.classList.remove("hidden");
  } else {
    degraded.classList.add("hidden");
  }

  updated.textContent = "Last updated: " + formatTime(state.evaluated_at);

  if (state.state_since) {
    const sinceDate = new Date(state.state_since * 1000);
    since.textContent = "State since: " + sinceDate.toLocaleString();
  }

  // Threshold display — read-only. Set on the server via TAIWAN_ALERT_THRESHOLD env var.
  const thresholdEl = document.getElementById("threshold-value");
  if (thresholdEl) {
    thresholdEl.textContent = state.threshold ?? "—";
  }
}

function renderIndicators(state) {
  const indicators = state.indicators || {};
  renderGrid("primary-grid", PRIMARY_IDS, indicators);
  renderGrid("secondary-grid", SECONDARY_IDS, indicators);
}

function renderGrid(gridId, ids, indicators) {
  const grid = document.getElementById(gridId);
  grid.innerHTML = "";

  for (const id of ids) {
    const ind = indicators[String(id)];
    if (!ind) continue;

    const card = document.createElement("div");
    const statusClass = !ind.feed_healthy ? "no-data" : ind.active ? "active" : "inactive";
    card.className = `indicator-card ${statusClass}`;

    const statusLabel = !ind.feed_healthy ? "no data" : ind.active ? "active" : "inactive";

    const confLevel = (ind.confidence && ind.confidence !== "none") ? ind.confidence : "na";
    const confLabel = confLevel === "na" ? "N/A" : confLevel;
    const confBadge = `<span class="confidence-badge ${confLevel}">${confLabel}</span>`;

    const evidenceHtml = renderEvidence(ind);
    const manipulationBadge = ind.manipulation_flagged_count > 0
      ? `<span class="manipulation-badge" title="LLM flagged ${ind.manipulation_flagged_count} input chunk(s) as injection attempts">⚠ ${ind.manipulation_flagged_count} flagged</span>`
      : "";

    card.innerHTML = `
      <div class="card-header">
        <span><span class="card-id">#${ind.id}</span> <span class="card-name">${ind.name || ""}</span></span>
        <span class="card-status ${statusClass}">${statusLabel}</span>
      </div>
      <div class="card-summary">${escapeHtml(ind.summary || "")}</div>
      <div class="card-meta">
        ${confBadge}
        ${manipulationBadge}
        <span>${formatTime(ind.last_checked)}</span>
      </div>
      ${evidenceHtml}
    `;

    grid.appendChild(card);
  }
}

function renderEvidence(ind) {
  const quotes = Array.isArray(ind.evidence_quotes) ? ind.evidence_quotes : [];
  if (quotes.length === 0 && !ind.rationale) return "";

  const items = quotes.slice(0, 6).map((q) => {
    const family = q.family || "";
    const claim = q.claim_type || "";
    const directness = q.directness || "";
    const phrase = escapeHtml(q.key_phrase || "");
    const why = escapeHtml(q.why || "");
    const sourceLabel = escapeHtml(q.source || "unknown");
    return `
      <li class="evidence-item">
        <div class="evidence-header">
          <span class="evidence-source">${sourceLabel}</span>
          <span class="evidence-family family-${family}">${family}</span>
          <span class="evidence-claim claim-${claim}">${claim}/${directness}</span>
        </div>
        <blockquote class="evidence-quote">${phrase}</blockquote>
        ${why ? `<div class="evidence-why">${why}</div>` : ""}
      </li>
    `;
  }).join("");

  // For active indicators, pin the rationale visibly outside the toggle so
  // users see the *why* without an extra tap. Verbose source quotes stay in
  // the collapsible to avoid clutter.
  const rationaleVisible = ind.active && ind.rationale
    ? `<div class="evidence-rationale-visible">${escapeHtml(ind.rationale)}</div>`
    : "";
  const rationaleInside = (!ind.active && ind.rationale)
    ? `<div class="evidence-rationale">${escapeHtml(ind.rationale)}</div>`
    : "";

  const toggleLabel = quotes.length > 0
    ? `Source quotes (${quotes.length})`
    : (ind.active ? "Why this is firing" : "Why this is inactive");

  return `
    ${rationaleVisible}
    <details class="evidence-block"${ind.active && quotes.length > 0 ? " open" : ""}>
      <summary class="evidence-toggle">${toggleLabel}</summary>
      ${rationaleInside}
      <ul class="evidence-list">${items}</ul>
    </details>
  `;
}

function formatTime(iso) {
  if (!iso) return "\u2014";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

async function refresh() {
  const state = await fetchState();
  if (state) {
    renderBanner(state);
    renderIndicators(state);
  } else {
    document.getElementById("state-detail").textContent = "Failed to load state.json";
  }
}

// Initial load + auto-refresh
refresh();
setInterval(refresh, REFRESH_MS);
