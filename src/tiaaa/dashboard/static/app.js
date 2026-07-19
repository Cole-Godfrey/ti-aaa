const state = { jobs: [], stats: null, searchTimer: null };

const pipelineOptions = [
  ["discovered", "Discovered"], ["queued", "Queued"], ["ready", "Ready"],
  ["applying", "Applying"], ["manual_review", "Needs review"], ["applied", "Applied"],
  ["failed", "Failed"], ["skipped", "Skipped"], ["expired", "Expired"],
  ["withdrawn", "Withdrawn"],
];
const outcomeOptions = [
  ["none", "No update"], ["oa", "OA received"], ["interview", "Interview"],
  ["offer", "Offer"], ["rejected", "Rejected"], ["withdrawn", "Withdrawn"],
];

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try { message = (await response.json()).detail || message; } catch (_) { /* response was not JSON */ }
    throw new Error(message);
  }
  return response.json();
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
}

function initials(company) {
  return String(company || "?").split(/\s+/).filter(Boolean).slice(0, 2)
    .map(word => word[0]).join("").toUpperCase();
}

function relativeTime(value) {
  if (!value) return "Unknown";
  const when = new Date(value);
  const seconds = Math.max(0, Math.round((Date.now() - when.getTime()) / 1000));
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
  return when.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function shortDate(value) {
  if (!value) return "—";
  return new Date(value).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function showToast(message, error = false) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.className = `toast show${error ? " error" : ""}`;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.className = "toast"; }, 2600);
}

function renderStats(stats) {
  state.stats = stats;
  document.getElementById("applicationsValue").textContent = stats.applications.toLocaleString();
  document.getElementById("applicationsHint").textContent = stats.applications
    ? `${stats.active.toLocaleString()} active internships tracked`
    : "Waiting for your first application";
  document.getElementById("oaRateValue").textContent = `${stats.oa_rate}%`;
  document.getElementById("oaHint").textContent = `${stats.oas} assessment${stats.oas === 1 ? "" : "s"} recorded`;
  document.getElementById("oaProgress").style.width = `${Math.min(100, stats.oa_rate)}%`;
  document.getElementById("interviewRateValue").textContent = `${stats.interview_rate}%`;
  document.getElementById("interviewHint").textContent = `${stats.interviews} interview${stats.interviews === 1 ? "" : "s"} recorded`;
  document.getElementById("interviewProgress").style.width = `${Math.min(100, stats.interview_rate)}%`;
  document.getElementById("activeValue").textContent = stats.active.toLocaleString();
  document.getElementById("readyValue").textContent = stats.ready.toLocaleString();
  document.getElementById("offersValue").textContent = stats.offers.toLocaleString();
  document.getElementById("eligibleBadge").textContent = `${stats.eligible.toLocaleString()} eligible`;

  const funnelData = [
    ["Discovered", stats.total_discovered],
    ["Eligible", stats.eligible],
    ["Ready", stats.ready + stats.applications],
    ["Applied", stats.applications],
    ["Interviews", stats.interviews],
  ];
  const maximum = Math.max(1, ...funnelData.map(row => row[1]));
  document.getElementById("funnel").innerHTML = funnelData.map(([label, count]) => `
    <div class="funnel-row">
      <span class="funnel-label">${escapeHtml(label)}</span>
      <span class="funnel-track"><i class="funnel-fill" style="width:${Math.max(count ? 2 : 0, count / maximum * 100)}%"></i></span>
      <strong class="funnel-count">${count.toLocaleString()}</strong>
    </div>`).join("");

  const recent = stats.recent_applications || [];
  const container = document.getElementById("recentApplications");
  if (!recent.length) {
    container.innerHTML = '<div class="empty-state compact"><span>↗</span><p>No applications yet.</p></div>';
  } else {
    container.innerHTML = recent.slice(0, 4).map(job => `
      <div class="recent-item">
        <span class="company-avatar">${escapeHtml(initials(job.company))}</span>
        <div class="recent-copy"><strong>${escapeHtml(job.company)}</strong><span>${escapeHtml(job.role)}</span></div>
        <time class="recent-time">${escapeHtml(relativeTime(job.applied_at))}</time>
      </div>`).join("");
  }
}

function optionMarkup(options, current) {
  return options.map(([value, label]) => `<option value="${value}"${value === current ? " selected" : ""}>${escapeHtml(label)}</option>`).join("");
}

function renderJobs(jobs) {
  state.jobs = jobs;
  const body = document.getElementById("jobsTableBody");
  if (!jobs.length) {
    body.innerHTML = '<tr><td colspan="6"><div class="empty-state"><span>⌕</span><p>No internships match this view.</p></div></td></tr>';
    document.getElementById("tableSummary").textContent = "0 internships shown";
    return;
  }
  body.innerHTML = jobs.map(job => `
    <tr data-job-id="${job.id}">
      <td class="role-cell"><strong>${escapeHtml(job.company)}</strong><span title="${escapeHtml(job.role)}">${escapeHtml(job.role)}</span></td>
      <td class="location-cell">${escapeHtml(job.location || "Not listed")}</td>
      <td><select class="status-select pipeline-select" aria-label="Pipeline status for ${escapeHtml(job.company)}">${optionMarkup(pipelineOptions, job.pipeline_status)}</select></td>
      <td><select class="status-select outcome-select" aria-label="Outcome for ${escapeHtml(job.company)}">${optionMarkup(outcomeOptions, job.outcome_status)}</select></td>
      <td class="date-cell">${escapeHtml(shortDate(job.first_seen_at))}</td>
      <td><a class="open-link" href="${escapeHtml(job.application_url)}" target="_blank" rel="noopener noreferrer" title="Open application">↗</a></td>
    </tr>`).join("");
  document.getElementById("tableSummary").textContent = `${jobs.length.toLocaleString()} internship${jobs.length === 1 ? "" : "s"} shown · Changes save instantly`;

  body.querySelectorAll(".pipeline-select").forEach(select => {
    select.addEventListener("change", event => updateJob(event.target.closest("tr").dataset.jobId, { pipeline_status: event.target.value }));
  });
  body.querySelectorAll(".outcome-select").forEach(select => {
    select.addEventListener("change", event => updateJob(event.target.closest("tr").dataset.jobId, { outcome_status: event.target.value }));
  });
}

async function updateJob(jobId, payload) {
  try {
    await api(`/api/jobs/${jobId}`, { method: "PATCH", body: JSON.stringify(payload) });
    showToast("Tracker updated");
    const stats = await api("/api/stats");
    renderStats(stats);
  } catch (error) {
    showToast(error.message, true);
    await loadJobs();
  }
}

async function loadJobs() {
  const search = document.getElementById("searchInput").value.trim();
  const status = document.getElementById("statusFilter").value;
  const params = new URLSearchParams({ limit: "150" });
  if (search) params.set("search", search);
  if (status !== "all") params.set("status", status);
  const data = await api(`/api/jobs?${params}`);
  renderJobs(data.items);
}

function renderSources(items) {
  const grouped = new Map();
  items.forEach(source => {
    if (!grouped.has(source.source_key)) grouped.set(source.source_key, []);
    grouped.get(source.source_key).push(source);
  });
  const container = document.getElementById("sourceCards");
  if (!items.length) {
    container.innerHTML = '<div class="empty-state"><span>⌘</span><p>Run tiaaa sync to initialize sources.</p></div>';
    return;
  }
  container.innerHTML = [...grouped.values()].map(documents => {
    const representative = documents[0];
    const hasError = documents.some(document => document.last_error);
    const successes = documents.map(document => document.last_success_at).filter(Boolean).sort();
    const lastSuccess = successes.length ? successes[successes.length - 1] : null;
    const title = representative.label.replace(/ · .*/, "");
    return `<article class="source-card">
      <div class="source-card-head"><strong>${escapeHtml(title)}</strong><span class="health-pill${hasError ? " error" : ""}" title="${hasError ? "Source error" : "Healthy"}"></span></div>
      <p>${escapeHtml(representative.repo_url.replace("https://github.com/", "github.com/"))}</p>
      <div class="source-meta"><span>${documents.length} active document${documents.length === 1 ? "" : "s"}</span><span>${lastSuccess ? relativeTime(lastSuccess) : "Not synced"}</span></div>
    </article>`;
  }).join("");
}

async function refreshAll() {
  const button = document.getElementById("refreshButton");
  button.classList.add("spinning");
  try {
    const [stats, , sources] = await Promise.all([
      api("/api/stats"), loadJobs(), api("/api/sources"),
    ]);
    renderStats(stats);
    renderSources(sources.items);
    document.getElementById("lastUpdated").textContent = `Updated ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
  } catch (error) {
    showToast(error.message, true);
    document.getElementById("lastUpdated").textContent = "Could not refresh";
  } finally {
    button.classList.remove("spinning");
  }
}

document.getElementById("refreshButton").addEventListener("click", refreshAll);
document.getElementById("statusFilter").addEventListener("change", () => loadJobs().catch(error => showToast(error.message, true)));
document.getElementById("searchInput").addEventListener("input", () => {
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(() => loadJobs().catch(error => showToast(error.message, true)), 250);
});
document.querySelectorAll(".nav-link").forEach(link => link.addEventListener("click", () => {
  document.querySelectorAll(".nav-link").forEach(item => item.classList.remove("active"));
  link.classList.add("active");
}));

refreshAll();
setInterval(refreshAll, 60000);
