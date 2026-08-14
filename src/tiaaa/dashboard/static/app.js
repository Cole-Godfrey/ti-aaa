const state = {
  config: null,
  onboarding: null,
  resumes: [],
  jobs: [],
  latestJobs: [],
  selectedJob: null,
  activeView: "overview",
  searchTimer: null,
  latestSearchTimer: null,
  onboardingStep: 0,
  claudeAuth: null,
  agentInputSignature: null,
  workerSignature: null,
  queueSignature: null,
  manualApplySignature: null,
  previewSockets: new Map(),
  analytics: null,
  analyticsDimension: "resume",
  webPush: {
    supported: false,
    subscribed: false,
    permission: "default",
  },
};

const viewCopy = {
  overview: ["DESK / 01", "Situation report", "Signals from the repository feed and your application register."],
  latest: ["DESK / 02", "Repository inbox", "Inspect current internship listings, open a dossier, and choose what the agent works on."],
  applications: ["DESK / 03", "Application register", "A spreadsheet-style record of submitted applications, resumes, and outcomes."],
  analytics: ["DESK / 04", "Response notebook", "Compare application outcomes across resumes, roles, sources, locations, and portals."],
  live: ["DESK / 05", "Agent wire", "Watch the browser, handle live checkpoints, and follow the serial application queue."],
  resumes: ["DESK / 06", "Fact archive", "Original resumes the agent compares, selects, and submits without rewriting."],
  settings: ["DESK / 07", "Operating rules", "Change polling, matching, and application boundaries."],
};
const applicationPipelineOptions = [
  ["manual_review", "Confirm in Agent"], ["applied", "Applied"], ["withdrawn", "Withdrawn"],
];
const outcomeOptions = [
  ["none", "No update"], ["oa", "OA received"], ["interview", "Interview"],
  ["offer", "Offer"], ["rejected", "Rejected"], ["withdrawn", "Withdrawn"],
];

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && !(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try { message = (await response.json()).detail || message; } catch (_) { /* not JSON */ }
    throw new Error(message);
  }
  if (response.status === 204) return null;
  return response.json();
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
}

function safeExternalUrl(value) {
  const text = String(value || "");
  return /^https?:\/\//i.test(text) ? escapeHtml(text) : "#";
}

function clone(value) { return JSON.parse(JSON.stringify(value)); }
function splitList(value) { return String(value || "").split(",").map(item => item.trim()).filter(Boolean); }
function joinList(value) { return Array.isArray(value) ? value.join(", ") : ""; }
function element(id) { return document.getElementById(id); }
function checked(id) { return element(id).checked; }
function value(id) { return element(id).value.trim(); }
function setValue(id, content) { element(id).value = content ?? ""; }
function setChecked(id, content) { element(id).checked = Boolean(content); }

function initials(company) {
  return String(company || "?").split(/\s+/).filter(Boolean).slice(0, 2)
    .map(word => word[0]).join("").toUpperCase();
}

function relativeTime(raw) {
  if (!raw) return "never";
  const time = new Date(raw);
  if (Number.isNaN(time.getTime())) return "unknown";
  const seconds = Math.round((Date.now() - time.getTime()) / 1000);
  if (seconds < -60) {
    const future = Math.abs(seconds);
    if (future < 3600) return `in ${Math.ceil(future / 60)}m`;
    return `in ${Math.ceil(future / 3600)}h`;
  }
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
  return time.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function shortDate(raw) {
  if (!raw) return "—";
  const time = new Date(raw);
  return Number.isNaN(time.getTime()) ? "—" : time.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "2-digit" });
}

function listingDate(postingDate, firstSeenAt) {
  const raw = postingDate || firstSeenAt;
  if (!raw) return "DATE NOT LISTED";
  const dateOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(raw));
  const time = dateOnly
    ? new Date(Number(dateOnly[1]), Number(dateOnly[2]) - 1, Number(dateOnly[3]))
    : new Date(raw);
  return Number.isNaN(time.getTime())
    ? "DATE NOT LISTED"
    : time.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function showToast(message, error = false) {
  const toast = element("toast");
  toast.textContent = message;
  toast.className = `toast show${error ? " error" : ""}`;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.className = "toast"; }, 3200);
}

function renderClaudeAuth(auth) {
  state.claudeAuth = auth;
  const connected = Boolean(auth?.logged_in);
  const pending = Boolean(auth?.login_pending);
  const apiKey = auth?.auth_method === "api_key";
  let title = "Claude account not connected";
  let detail = "Connect a Claude Pro or Max account for browser form filling. You do not need an Anthropic API key.";
  if (!auth?.installed) {
    title = "Claude Code is not installed";
    detail = "Docker includes Claude Code. Native installs need the Claude Code CLI before browser automation can run.";
  } else if (connected && apiKey) {
    title = "Connected with an API key";
    detail = "Browser automation will use separate Anthropic API billing. Remove ANTHROPIC_API_KEY from the local environment to use your Claude subscription instead.";
  } else if (connected) {
    title = "Claude account connected";
    detail = "Browser automation will use your saved Claude Code account login; no API key is required.";
  } else if (pending) {
    title = "Waiting for your one-time code";
    detail = "Complete the Claude sign-in page, then paste the one-time code below.";
  }
  ["claudeAuthState", "onboardClaudeAuthState"].forEach(id => { element(id).textContent = title; });
  ["claudeAuthDetail", "onboardClaudeAuthDetail"].forEach(id => { element(id).textContent = detail; });
  ["claudeAuthDot", "onboardClaudeAuthDot"].forEach(id => {
    element(id).className = `auth-dot${connected ? " connected" : pending ? " pending" : ""}`;
  });
  element("connectClaude").classList.toggle("hidden", connected);
  element("onboardConnectClaude").classList.toggle("hidden", connected);
  element("disconnectClaude").classList.toggle("hidden", !connected || apiKey);
  ["claudeCodePanel", "onboardClaudeCodePanel"].forEach(id => element(id).classList.toggle("hidden", !pending));
  const loginUrl = /^https:\/\/claude\.com\//.test(auth?.login_url || "") ? auth.login_url : "";
  ["claudeLoginLink", "onboardClaudeLoginLink"].forEach(id => {
    const link = element(id);
    if (loginUrl) link.href = loginUrl;
    else link.removeAttribute("href");
  });
}

async function refreshClaudeAuth() {
  const auth = await api("/api/claude-auth");
  renderClaudeAuth(auth);
  return auth;
}

async function startClaudeLogin(button) {
  button.disabled = true;
  const loginWindow = window.open("", "_blank");
  if (loginWindow) loginWindow.opener = null;
  try {
    const auth = await api("/api/claude-auth/login", { method: "POST" });
    renderClaudeAuth(auth);
    if (loginWindow && auth.login_url) loginWindow.location.replace(auth.login_url);
    showToast("Claude sign-in opened; paste its one-time code when finished");
  } catch (error) {
    if (loginWindow) loginWindow.close();
    showToast(error.message, true);
  } finally { button.disabled = false; }
}

async function completeClaudeLogin(inputId, button) {
  const code = value(inputId);
  if (!code) { showToast("Paste the one-time code from Claude", true); return; }
  button.disabled = true;
  try {
    const auth = await api("/api/claude-auth/complete", {
      method: "POST", body: JSON.stringify({ code }),
    });
    setValue("claudeLoginCode", ""); setValue("onboardClaudeLoginCode", "");
    renderClaudeAuth(auth);
    showToast("Claude account connected—no API key needed");
  } catch (error) { showToast(error.message, true); }
  finally { button.disabled = false; }
}

function setView(view) {
  if (!viewCopy[view]) return;
  if (state.activeView === "live" && view !== "live") closePreviewStreams();
  state.activeView = view;
  const url = new URL(window.location.href);
  if (view === "overview") url.searchParams.delete("view");
  else url.searchParams.set("view", view);
  window.history.replaceState({}, "", url);
  document.querySelectorAll(".view").forEach(node => node.classList.toggle("active", node.dataset.view === view));
  document.querySelectorAll(".nav-item").forEach(node => node.classList.toggle("active", node.dataset.viewTarget === view));
  const [kicker, title, subtitle] = viewCopy[view];
  element("viewKicker").textContent = kicker;
  element("viewTitle").textContent = title;
  element("viewSubtitle").textContent = subtitle;
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (view === "live") refreshLive().catch(error => showToast(error.message, true));
  if (view === "latest") loadLatestJobs().catch(error => showToast(error.message, true));
  if (view === "analytics") loadAnalytics().catch(error => showToast(error.message, true));
}

function renderStats(stats) {
  element("applicationsValue").textContent = stats.applications.toLocaleString();
  element("applicationsHint").textContent = stats.applications ? `${stats.active} active listings in the ledger` : "No submissions recorded yet";
  element("oaRateValue").textContent = `${stats.oa_rate}%`;
  element("oaHint").textContent = `${stats.oas} assessment${stats.oas === 1 ? "" : "s"}`;
  element("interviewRateValue").textContent = `${stats.interview_rate}%`;
  element("interviewHint").textContent = `${stats.interviews} interview${stats.interviews === 1 ? "" : "s"}`;
  element("queuedValue").textContent = stats.queued.toLocaleString();
  element("readyValue").textContent = stats.ready.toLocaleString();
  element("offersValue").textContent = stats.offers.toLocaleString();
  element("eligibleBadge").textContent = `${stats.eligible.toLocaleString()} eligible`;

  const funnel = [
    ["Discovered", stats.total_discovered], ["Eligible", stats.eligible],
    ["Prepared", stats.ready + stats.applications], ["Applied", stats.applications],
    ["Interview", stats.interviews],
  ];
  const maximum = Math.max(1, ...funnel.map(item => item[1]));
  element("funnel").innerHTML = funnel.map(([label, count]) => `
    <div class="funnel-row"><span class="funnel-label">${label}</span>
      <span class="funnel-track"><i class="funnel-fill" style="width:${Math.max(count ? 1 : 0, count / maximum * 100)}%"></i></span>
      <strong class="funnel-count">${count.toLocaleString()}</strong></div>`).join("");

  const recent = stats.recent_applications || [];
  element("recentApplications").innerHTML = recent.length ? recent.slice(0, 4).map(job => `
    <div class="recent-item"><span class="avatar">${escapeHtml(initials(job.company))}</span>
      <div class="recent-copy"><strong>${escapeHtml(job.company)}</strong><span>${escapeHtml(job.role)}</span>
      <span class="recent-resume">${escapeHtml(job.submitted_resume_name || "Resume not recorded")}</span></div>
      <time>${escapeHtml(relativeTime(job.applied_at))}</time></div>`).join("") : '<div class="empty">No applications yet. Choose a role from Latest jobs when you are ready.</div>';
}

const analyticsDimensionLabels = {
  resume: "Resume",
  role_family: "Role family",
  source: "Source repository",
  location: "Location",
  portal: "Application portal",
};

function renderAnalyticsBreakdown() {
  const analytics = state.analytics;
  if (!analytics) return;
  const dimension = state.analyticsDimension;
  const rows = analytics.dimensions?.[dimension] || [];
  const total = Math.max(1, analytics.summary?.applications || 0);
  element("analyticsDimensionHeading").textContent = analyticsDimensionLabels[dimension];
  document.querySelectorAll("[data-analytics-dimension]").forEach(button => {
    button.classList.toggle("active", button.dataset.analyticsDimension === dimension);
  });
  element("analyticsBody").innerHTML = rows.length ? rows.map(row => {
    const share = Math.max(2, row.applications / total * 100);
    return `<tr>
      <td><span class="segment-label" style="--segment-share:${share}%"><i></i><span>${escapeHtml(row.label)}</span></span></td>
      <td>${row.applications.toLocaleString()}</td>
      <td>${row.oas.toLocaleString()}</td>
      <td class="rate-cell"><strong>${row.oa_rate}%</strong></td>
      <td>${row.interviews.toLocaleString()}</td>
      <td class="rate-cell"><strong>${row.interview_rate}%</strong></td>
      <td>${row.offers.toLocaleString()} <span class="rate-cell">· ${row.offer_rate}%</span></td>
    </tr>`;
  }).join("") : '<tr><td colspan="7" class="loading">No submitted applications to analyze yet.</td></tr>';
  element("analyticsSummary").textContent = rows.length
    ? `${rows.length} ${analyticsDimensionLabels[dimension].toLowerCase()} segment${rows.length === 1 ? "" : "s"} · rates use submitted applications as the denominator`
    : "Submit an application to begin this breakdown.";
}

function renderAnalytics(analytics) {
  state.analytics = analytics;
  const summary = analytics.summary || {};
  element("analyticsApplications").textContent = (summary.applications || 0).toLocaleString();
  element("analyticsOaRate").textContent = `${summary.oa_rate || 0}%`;
  element("analyticsInterviewRate").textContent = `${summary.interview_rate || 0}%`;
  element("analyticsOfferRate").textContent = `${summary.offer_rate || 0}%`;
  renderAnalyticsBreakdown();
}

async function loadAnalytics() {
  renderAnalytics(await api("/api/analytics"));
}

function optionsMarkup(options, current, disabled = []) {
  return options.map(([key, label]) => `<option value="${key}"${key === current ? " selected" : ""}${disabled.includes(key) ? " disabled" : ""}>${label}</option>`).join("");
}

function renderJobs(jobs) {
  state.jobs = jobs;
  const body = element("jobsTableBody");
  if (!jobs.length) {
    body.innerHTML = '<tr><td colspan="6" class="loading">No applications match this view.</td></tr>';
    element("tableSummary").textContent = "0 application records shown";
    return;
  }
  body.innerHTML = jobs.map(job => {
    const submitted = Boolean(job.applied_at);
    const resumeName = job.submitted_resume_name || job.base_resume_name;
    const resumeState = job.submitted_resume_name ? "submitted" : "prepared";
    const retryAction = job.pipeline_status === "manual_review" && job.availability_status !== "manual_only"
      ? `<button class="mini-apply retry-application" type="button" aria-label="Retry application for ${escapeHtml(job.company)}">Retry</button>`
      : "";
    const appliedNote = submitted && job.apply_origin === "self" ? "<span>applied by you</span>" : "";
    return `<tr data-job-id="${job.id}">
      <td class="role-cell"><strong>${escapeHtml(job.company)}</strong><span>${escapeHtml(job.role)} · ${escapeHtml(job.location || "location not listed")}</span></td>
      <td><select class="status-select pipeline-select" aria-label="Application status for ${escapeHtml(job.company)}">${optionsMarkup(applicationPipelineOptions, job.pipeline_status)}</select></td>
      <td class="resume-cell">${resumeName ? `<a href="/api/jobs/${job.id}/resume" target="_blank">${escapeHtml(resumeName)}</a><span>${resumeState}</span>` : "Not recorded"}</td>
      <td><select class="status-select outcome-select" aria-label="Outcome for ${escapeHtml(job.company)}"${submitted ? "" : " disabled"}>${optionsMarkup(outcomeOptions, job.outcome_status)}</select></td>
      <td class="applied-cell">${submitted ? `${escapeHtml(shortDate(job.applied_at))}${appliedNote}` : "Not submitted"}</td>
      <td><div class="application-actions">${retryAction}<a class="open-link" href="${safeExternalUrl(job.application_url)}" target="_blank" rel="noopener noreferrer" title="Open application">↗</a></div></td>
    </tr>`;
  }).join("");
  element("tableSummary").textContent = `${jobs.length} application record${jobs.length === 1 ? "" : "s"} shown · tracker changes save immediately`;
  body.querySelectorAll(".pipeline-select").forEach(select => select.addEventListener("change", event => {
    updateJob(event.target.closest("tr").dataset.jobId, { pipeline_status: event.target.value });
  }));
  body.querySelectorAll(".outcome-select").forEach(select => select.addEventListener("change", event => {
    updateJob(event.target.closest("tr").dataset.jobId, { outcome_status: event.target.value });
  }));
  body.querySelectorAll(".retry-application").forEach(button => button.addEventListener("click", event => {
    retryJobApplication(event.target.closest("tr").dataset.jobId, button);
  }));
}

async function retryJobApplication(jobId, button) {
  if (!state.claudeAuth?.logged_in) {
    setView("settings");
    showToast("Connect Claude Code in Settings before retrying the agent", true);
    return;
  }
  if (!window.confirm("Retry this application from the beginning?\n\nAny form currently open in Agent will close, and pending checkpoint answers will be cleared.")) return;
  button.disabled = true;
  try {
    await api(`/api/jobs/${jobId}/retry`, { method: "POST" });
    state.agentInputSignature = null;
    showToast("Application queued for a clean retry");
    await Promise.all([loadJobs(), loadLatestJobs(), refreshLive()]);
  } catch (error) {
    showToast(error.message, true);
    button.disabled = false;
  }
}

async function updateJob(jobId, payload) {
  try {
    await api(`/api/jobs/${jobId}`, { method: "PATCH", body: JSON.stringify(payload) });
    showToast("Application tracker updated");
    renderStats(await api("/api/stats"));
    await loadAnalytics();
  } catch (error) {
    showToast(error.message, true);
    await loadJobs();
  }
}

async function loadJobs() {
  const parameters = new URLSearchParams({ view: "applications", limit: "250" });
  const search = value("searchInput");
  const status = element("statusFilter").value;
  if (search) parameters.set("search", search);
  if (status !== "all") parameters.set("status", status);
  const response = await api(`/api/jobs?${parameters}`);
  renderJobs(response.items);
}

function jobActionLabel(job) {
  if (job.availability_status === "manual_only") return "Open manually";
  if (job.manual_requested && ["queued", "ready", "failed"].includes(job.pipeline_status)) return "Queued";
  const labels = {
    queued: "Start agent",
    ready: "Start agent",
    applying: "Applying…",
    manual_review: "Confirm in Agent",
    applied: "Applied",
    expired: "Closed",
    withdrawn: "Withdrawn",
    failed: "Retry",
  };
  return labels[job.pipeline_status] || "Apply";
}

function jobActionDisabled(job) {
  if (job.availability_status === "manual_only") return !job.is_active;
  return !job.is_active || Boolean(job.manual_requested) || ["applying", "manual_review", "applied", "expired", "withdrawn"].includes(job.pipeline_status);
}

function renderLatestJobs() {
  const eligibility = element("latestEligibility").value;
  const jobs = state.latestJobs.filter(job => eligibility === "all" || job.eligibility === eligibility);
  const body = element("latestJobsBody");
  element("latestCount").textContent = jobs.length.toLocaleString();
  if (!jobs.length) {
    body.innerHTML = '<tr><td colspan="7" class="loading">No active listings match this filter.</td></tr>';
    element("latestSummary").textContent = "Try another phrase or matching-rule filter.";
    return;
  }
  body.innerHTML = jobs.map(job => {
    const qualified = job.eligibility === "eligible";
    const qualificationReason = qualified && job.eligibility_reason === "eligible"
      ? "Known requirements match"
      : job.eligibility_reason || "Qualification not evaluated";
    return `<tr data-job-id="${job.id}">
    <td class="date-cell">${escapeHtml(listingDate(job.posting_date, job.first_seen_at))}</td>
    <td class="role-cell"><button class="row-detail" type="button"><strong>${escapeHtml(job.company)}</strong><span>${escapeHtml(job.role)}</span></button></td>
    <td>${escapeHtml(job.location || "Not listed")}</td>
    <td><span class="fit-mark ${Number(job.fit_score) >= 7 ? "good" : "warn"}">${escapeHtml(job.fit_score ?? "—")}/10</span></td>
    <td class="qualification-cell"><span class="qualification-mark ${qualified ? "qualified" : "unqualified"}">${qualified ? "Yes" : "No"}</span><small>${escapeHtml(qualificationReason)}</small></td>
    <td><span class="state-stamp state-${escapeHtml(job.pipeline_status)}">${escapeHtml(job.pipeline_status.replaceAll("_", " "))}</span></td>
    <td class="row-actions"><button class="text-button detail-job" type="button">Details</button>${job.availability_status === "manual_only"
      ? `<a class="mini-apply manual-link" href="${safeExternalUrl(job.application_url)}" target="_blank" rel="noopener noreferrer">Open manually</a>`
      : `<button class="mini-apply" type="button"${jobActionDisabled(job) ? " disabled" : ""}>${escapeHtml(jobActionLabel(job))}</button>`}</td>
  </tr>`;
  }).join("");
  element("latestSummary").textContent = `${jobs.length} active listing${jobs.length === 1 ? "" : "s"} · ordered by repository posting date`;
  body.querySelectorAll(".row-detail, .detail-job").forEach(button => button.addEventListener("click", event => openJobDetail(event.target.closest("tr").dataset.jobId)));
  body.querySelectorAll(".mini-apply").forEach(button => button.addEventListener("click", event => requestJobApplication(event.target.closest("tr").dataset.jobId, button)));
}

async function loadLatestJobs() {
  const parameters = new URLSearchParams({ view: "latest", limit: "500" });
  const search = value("latestSearch");
  if (search) parameters.set("search", search);
  const response = await api(`/api/jobs?${parameters}`);
  state.latestJobs = response.items;
  renderLatestJobs();
}

function closeJobDetail() {
  element("jobDetailScrim").classList.add("hidden");
  state.selectedJob = null;
}

async function openJobDetail(jobId) {
  try {
    const job = await api(`/api/jobs/${jobId}`);
    state.selectedJob = job;
    element("jobDetailNumber").textContent = `#${job.id}`;
    const sources = String(job.source_labels || "Repository source").split(",");
    const events = (job.events || []).slice(0, 6);
    const applicationBoundary = job.application_mode === "auto"
      ? "Unattended Auto mode"
      : job.application_mode === "manual_auto_submit"
        ? "Submits after your manual Apply selection"
        : "Final Submit requires confirmation in Agent";
    element("jobDetailContent").innerHTML = `
      <p class="drawer-kicker">${escapeHtml(listingDate(job.posting_date, job.first_seen_at))}</p>
      <h2 id="jobDetailTitle">${escapeHtml(job.role)}</h2><h3>${escapeHtml(job.company)}</h3>
      <dl class="job-facts">
        <div><dt>Location</dt><dd>${escapeHtml(job.location || "Not listed")}</dd></div>
        <div><dt>Fit</dt><dd>${escapeHtml(job.fit_score ?? "—")}/10 · ${escapeHtml(job.score_reasoning || "Not scored")}</dd></div>
        <div><dt>Eligibility</dt><dd>${escapeHtml(job.eligibility)} · ${escapeHtml(job.eligibility_reason || "No rule note")}</dd></div>
        <div><dt>Agent boundary</dt><dd>${applicationBoundary}</dd></div>
        <div><dt>Resume</dt><dd>${escapeHtml(job.submitted_resume_name || job.base_resume_name || "Selected during preparation")}</dd></div>
      </dl>
      <section class="drawer-section"><span>FOUND IN</span>${sources.map(source => `<p>${escapeHtml(source)}</p>`).join("")}</section>
      <section class="drawer-section"><span>RECENT ACTIVITY</span>${events.length ? events.map(event => `<p><b>${escapeHtml(event.event_type.replaceAll("_", " "))}</b> ${escapeHtml(relativeTime(event.created_at))}${event.detail ? ` · ${escapeHtml(event.detail)}` : ""}</p>`).join("") : "<p>No application activity yet.</p>"}</section>`;
    const external = /^https?:\/\//i.test(job.application_url || "") ? job.application_url : "#";
    element("jobExternalLink").href = external;
    const applyButton = element("applyJobButton");
    applyButton.textContent = jobActionLabel(job);
    applyButton.disabled = jobActionDisabled(job);
    applyButton.dataset.jobId = job.id;
    element("jobDetailScrim").classList.remove("hidden");
  } catch (error) { showToast(error.message, true); }
}

async function requestJobApplication(jobId, button) {
  const selected = state.latestJobs.find(job => String(job.id) === String(jobId)) || state.selectedJob;
  if (selected?.availability_status === "manual_only") {
    window.open(selected.application_url, "_blank", "noopener,noreferrer");
    return;
  }
  if (!state.claudeAuth?.logged_in) {
    closeJobDetail();
    setView("settings");
    showToast("Connect Claude Code in Settings before starting the agent", true);
    return;
  }
  const manualAutoSubmit = Boolean(state.config?.settings?.automation?.manual_auto_submit);
  const confirmationDetail = manualAutoSubmit
    ? "The agent will complete and submit the form. It will still pause in Agent for missing facts or a one-time verification code."
    : "The agent will complete the form and wait in Agent for your final Submit confirmation.";
  if (!window.confirm(`Apply to this role with TI-AAA?\n\n${confirmationDetail}`)) return;
  button.disabled = true;
  try {
    await api(`/api/jobs/${jobId}/apply`, { method: "POST" });
    showToast("Application queued; follow progress in Agent");
    await Promise.all([loadLatestJobs(), loadJobs()]);
    if (!element("jobDetailScrim").classList.contains("hidden")) await openJobDetail(jobId);
  } catch (error) {
    showToast(error.message, true);
    button.disabled = false;
  }
}

function renderSources(items) {
  const groups = new Map();
  items.forEach(source => {
    if (!groups.has(source.source_key)) groups.set(source.source_key, []);
    groups.get(source.source_key).push(source);
  });
  element("sourceCards").innerHTML = items.length ? [...groups.values()].map(documents => {
    const source = documents[0];
    const errors = documents.some(item => item.last_error);
    const successes = documents.map(item => item.last_success_at).filter(Boolean).sort();
    const latest = successes.at(-1);
    return `<article class="source-card"><header><strong>${escapeHtml(source.label.replace(/ · .*/, ""))}</strong><i class="health${errors ? " error" : ""}"></i></header>
      <p>${escapeHtml(source.repo_url.replace("https://github.com/", "github.com/"))}</p>
      <div class="source-meta"><span>${documents.length} document${documents.length === 1 ? "" : "s"}</span><span>${escapeHtml(latest ? relativeTime(latest) : "awaiting first import")}</span></div></article>`;
  }).join("") : '<div class="empty">The first background poll will initialize all three repositories.</div>';
}

function renderService(service) {
  const status = String(service.process_running ? (service.service_status || "starting") : "dashboard_only");
  const banner = element("serviceBanner");
  const good = ["waiting", "syncing", "preparing", "applying", "starting", "requested"].includes(status);
  banner.className = `service-banner${good ? " good" : status === "error" || status === "offline" ? " error" : ""}`;
  element("serviceStatus").textContent = status.replaceAll("_", " ");
  element("serviceMessage").textContent = service.process_running
    ? (service.service_message || "Background service is active")
    : "Dashboard-only mode. Start with `tiaaa serve` to run the agent.";
  element("nextCycle").textContent = (
    service.next_cycle_at && !["disabled", "paused", "dashboard_only"].includes(status)
  ) ? `Next check ${relativeTime(service.next_cycle_at)}` : "";
  element("railStatus").textContent = status.replaceAll("_", " ");
  element("railStatusDot").className = good ? "good" : status === "error" || status === "offline" ? "error" : "";
  element("pauseButton").textContent = status === "paused" ? "Resume" : "Pause";
  element("pauseButton").disabled = !service.process_running;
  element("runButton").disabled = !service.process_running || ["syncing", "preparing", "applying"].includes(status);
}

function renderResumes(items) {
  state.resumes = items;
  element("resumeCount").textContent = `${items.length} resume${items.length === 1 ? "" : "s"}`;
  element("resumeGrid").innerHTML = items.length ? items.map(resume => `
    <article class="resume-card" data-resume-id="${resume.id}"><header><div><h3>${escapeHtml(resume.name)}</h3><p>${escapeHtml(resume.original_filename)}</p></div>
      <div class="resume-actions"><a class="icon-link" href="/api/resumes/${resume.id}/download" target="_blank" title="Open PDF">↗</a><button class="icon-delete" type="button" title="Archive resume">×</button></div></header>
      <div class="resume-tags">${(resume.tags || []).map(tag => `<span>${escapeHtml(tag)}</span>`).join("") || "<span>general</span>"}</div>
      <div class="resume-stats"><span>${resume.selected_count} selections</span><span>${resume.submitted_count} submissions</span><span>added ${escapeHtml(relativeTime(resume.created_at))}</span></div>
    </article>`).join("") : '<div class="empty tall">No active resumes. Upload one to prepare applications.</div>';
  element("resumeGrid").querySelectorAll(".icon-delete").forEach(button => button.addEventListener("click", async event => {
    const card = event.target.closest(".resume-card");
    if (!window.confirm("Archive this resume? Existing application records keep their reference.")) return;
    try {
      await api(`/api/resumes/${card.dataset.resumeId}`, { method: "DELETE" });
      showToast("Resume archived");
      await loadResumes();
    } catch (error) { showToast(error.message, true); }
  }));
}

async function loadResumes() {
  const response = await api("/api/resumes");
  renderResumes(response.items);
}

async function drawPreviewBlob(workerId, blob, record) {
  const canvas = document.querySelector(`[data-preview-canvas="${workerId}"]`);
  if (!canvas) return;
  const drawSequence = (record.drawSequence || 0) + 1;
  record.drawSequence = drawSequence;
  try {
    if ("createImageBitmap" in window) {
      const bitmap = await createImageBitmap(blob);
      if (drawSequence !== record.drawSequence) { bitmap.close(); return; }
      canvas.width = bitmap.width;
      canvas.height = bitmap.height;
      canvas.getContext("2d").drawImage(bitmap, 0, 0);
      bitmap.close();
    } else {
      const url = URL.createObjectURL(blob);
      try {
        const image = new Image();
        await new Promise((resolve, reject) => {
          image.onload = resolve;
          image.onerror = reject;
          image.src = url;
        });
        if (drawSequence !== record.drawSequence) return;
        canvas.width = image.naturalWidth;
        canvas.height = image.naturalHeight;
        canvas.getContext("2d").drawImage(image, 0, 0);
      } finally { URL.revokeObjectURL(url); }
    }
    const frame = canvas.closest(".preview-frame");
    const streaming = Boolean(record.receivedStream && record.worker.stream_active);
    frame.classList.add("has-frame");
    frame.classList.toggle("streaming", streaming);
    frame.classList.toggle("fallback", !streaming);
  } catch (_) { /* keep the previous frame visible when one decode fails */ }
}

async function loadPreviewFallback(worker, record) {
  if (!worker.preview_available || !worker.preview_url || record.receivedStream) return;
  try {
    const response = await fetch(`${worker.preview_url}?t=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) return;
    record.lastBlob = await response.blob();
    await drawPreviewBlob(worker.worker_id, record.lastBlob, record);
  } catch (_) { /* the worker may not have emitted its first JPEG yet */ }
}

function closePreviewStreams() {
  state.previewSockets.forEach(record => {
    record.closedByClient = true;
    clearTimeout(record.fallbackTimer);
    clearInterval(record.fallbackInterval);
    clearTimeout(record.reconnectTimer);
    try { record.socket.close(); } catch (_) { /* already closed */ }
  });
  state.previewSockets.clear();
}

function openPreviewStream(worker) {
  if (state.activeView !== "live" || state.previewSockets.has(worker.worker_id)) return;
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}/api/workers/${worker.worker_id}/stream`);
  socket.binaryType = "arraybuffer";
  const record = {
    socket,
    worker,
    receivedStream: false,
    closedByClient: false,
    lastBlob: null,
    drawSequence: 0,
    fallbackTimer: null,
    fallbackInterval: null,
    reconnectTimer: null,
  };
  state.previewSockets.set(worker.worker_id, record);
  record.fallbackTimer = setTimeout(() => {
    loadPreviewFallback(record.worker, record);
    record.fallbackInterval = setInterval(() => loadPreviewFallback(record.worker, record), 2000);
  }, 900);
  socket.addEventListener("message", async event => {
    if (typeof event.data === "string") return;
    record.receivedStream = true;
    clearTimeout(record.fallbackTimer);
    clearInterval(record.fallbackInterval);
    record.lastBlob = new Blob([event.data], { type: "image/jpeg" });
    await drawPreviewBlob(worker.worker_id, record.lastBlob, record);
  });
  socket.addEventListener("close", () => {
    clearTimeout(record.fallbackTimer);
    clearInterval(record.fallbackInterval);
    if (state.previewSockets.get(worker.worker_id) === record) {
      state.previewSockets.delete(worker.worker_id);
    }
    if (!record.closedByClient && state.activeView === "live") {
      loadPreviewFallback(record.worker, record);
      record.reconnectTimer = setTimeout(() => openPreviewStream(record.worker), 1500);
    }
  });
}

function syncPreviewStreams(items) {
  if (state.activeView !== "live") return;
  const activeIds = new Set(items.map(worker => worker.worker_id));
  state.previewSockets.forEach((record, workerId) => {
    if (!activeIds.has(workerId)) {
      record.closedByClient = true;
      clearTimeout(record.fallbackTimer);
      clearInterval(record.fallbackInterval);
      record.socket.close();
      state.previewSockets.delete(workerId);
    }
  });
  items.forEach(worker => {
    const record = state.previewSockets.get(worker.worker_id);
    if (record) {
      record.worker = worker;
      if (record.lastBlob) drawPreviewBlob(worker.worker_id, record.lastBlob, record);
    } else {
      openPreviewStream(worker);
    }
  });
}

function renderWorkers(items) {
  const signature = JSON.stringify(items.map(worker => ({
    worker_id: worker.worker_id,
    status: worker.status,
    job_id: worker.job_id,
    company: worker.company,
    role: worker.role,
    message: worker.message,
    preview_available: worker.preview_available,
    stream_active: worker.stream_active,
    browser_interactive: worker.browser_interactive,
    stoppable: worker.stoppable,
    stop_requested: worker.stop_requested,
    updated_at: worker.updated_at,
  })));
  if (signature !== state.workerSignature) {
    state.workerSignature = signature;
    element("workerGrid").innerHTML = items.length ? items.map(worker => {
      const interactive = Boolean(worker.browser_interactive);
      return `
      <article class="worker-card${interactive ? " human-control" : ""}"><div class="worker-head"><div class="worker-title"><strong>${escapeHtml(worker.company || worker.worker_id)}</strong><span>${escapeHtml(worker.role || "Waiting for a prepared application")}</span></div>
        <span class="worker-state ${escapeHtml(worker.status)}">${escapeHtml(worker.status)}</span></div>
        <div class="preview-frame${interactive ? " interactive" : ""}" data-preview-frame="${escapeHtml(worker.worker_id)}">
          <canvas data-preview-canvas="${escapeHtml(worker.worker_id)}"${interactive ? ` data-browser-control="${escapeHtml(worker.worker_id)}" tabindex="0"` : ""} aria-label="${interactive ? "Interactive" : "Live"} local browser view for ${escapeHtml(worker.worker_id)}"></canvas>
          <p class="preview-empty">The live browser view appears here when a worker starts.</p>
        </div>
        ${interactive ? `<div class="browser-control-bar"><div><strong>YOU HAVE CONTROL OF THIS BROWSER</strong><span>Click the page, type or paste into the focused field, and scroll here. This is the agent's exact retained tab—not a new job link.</span></div><button class="button ink return-browser-control" type="button" data-return-browser-control="${worker.job_id}">Continue agent</button></div>` : ""}
        <p class="worker-message">${escapeHtml(worker.message || "Waiting for the next cycle")}</p>
        ${worker.stoppable ? `<div class="worker-actions" data-stop-job="${worker.job_id}"><span>${worker.stop_requested ? "Stopping this session…" : "Stuck, or did you finish this one yourself?"}</span><button class="button ghost stop-agent" type="button"${worker.stop_requested ? " disabled" : ""}>${worker.stop_requested ? "Stopping…" : "Stop session"}</button></div>` : ""}</article>`;
    }).join("") : '<div class="empty tall">No browser worker has run yet. Enable browser automation in Settings, or keep watch-and-prepare mode.</div>';
  }
  syncPreviewStreams(items);
  renderAgentInputs(items);
}

function sendBrowserControl(canvas, action) {
  const record = state.previewSockets.get(canvas.dataset.browserControl);
  if (!record || record.socket.readyState !== WebSocket.OPEN) {
    showToast("The live browser control channel is reconnecting", true);
    return;
  }
  record.socket.send(JSON.stringify(action));
}

function browserControlPoint(canvas, event) {
  const bounds = canvas.getBoundingClientRect();
  const sourceWidth = Number(canvas.width);
  const sourceHeight = Number(canvas.height);
  if (!bounds.width || !bounds.height || !sourceWidth || !sourceHeight
      || !canvas.closest(".preview-frame")?.classList.contains("has-frame")) return null;
  const scale = Math.min(bounds.width / sourceWidth, bounds.height / sourceHeight);
  const renderedWidth = sourceWidth * scale;
  const renderedHeight = sourceHeight * scale;
  const offsetX = (bounds.width - renderedWidth) / 2;
  const offsetY = (bounds.height - renderedHeight) / 2;
  const localX = event.clientX - bounds.left - offsetX;
  const localY = event.clientY - bounds.top - offsetY;
  if (localX < 0 || localX > renderedWidth || localY < 0 || localY > renderedHeight) return null;
  return {
    x: Math.max(0, Math.min(1, localX / renderedWidth)),
    y: Math.max(0, Math.min(1, localY / renderedHeight)),
  };
}

async function returnBrowserControl(button) {
  button.disabled = true;
  try {
    await api(`/api/jobs/${button.dataset.returnBrowserControl}/continue-agent`, { method: "POST" });
    state.workerSignature = null;
    state.agentInputSignature = null;
    showToast("Control returned; the agent is inspecting this same page");
    await refreshLive();
  } catch (error) {
    showToast(error.message, true);
    button.disabled = false;
  }
}

function renderApplicationQueue(items, summary = {}) {
  const signature = JSON.stringify({ items, summary });
  if (signature === state.queueSignature) return;
  state.queueSignature = signature;
  const active = Number(summary.active || 0);
  const waiting = Number(summary.waiting || 0);
  element("agentQueueCount").textContent = active
    ? `${active} active · ${waiting} waiting`
    : `${waiting} waiting`;
  const list = element("agentQueueList");
  if (!items.length) {
    list.innerHTML = '<div class="empty">No applications are waiting. New matching repository jobs and manual Apply requests appear here.</div>';
    return;
  }
  const stateLabels = {
    active: "Applying now",
    preparing: "Preparing",
    ready: "Waiting",
    retry: "Retry queued",
    waiting: "Waiting",
  };
  list.innerHTML = items.map(item => {
    const statusLabel = item.queue_state === "active" && item.worker_status === "review_ready"
      ? "Confirm submit"
      : item.queue_state === "active" && item.worker_status === "captcha"
        ? "Your control"
      : item.queue_state === "active" && item.worker_status === "needs_review"
        ? "Needs input"
        : stateLabels[item.queue_state] || "Waiting";
    return `
    <button class="agent-queue-row ${escapeHtml(item.queue_state)}" type="button" data-queue-job="${item.id}">
      <span class="queue-position">${String(item.position).padStart(2, "0")}</span>
      <span class="queue-role"><strong>${escapeHtml(item.company)}</strong><span>${escapeHtml(item.role)}${item.location ? ` · ${escapeHtml(item.location)}` : ""}</span>${item.detail ? `<em>${escapeHtml(item.detail)}</em>` : ""}</span>
      <span class="queue-origin">${item.origin === "auto" ? "Auto mode" : "Manual"}</span>
      <span class="queue-fit">${escapeHtml(item.fit_score ?? "—")}<small>/10 fit</small></span>
      <span class="queue-state">${escapeHtml(statusLabel)}</span>
    </button>`;
  }).join("");
  list.querySelectorAll("[data-queue-job]").forEach(row => row.addEventListener("click", () => {
    openJobDetail(row.dataset.queueJob);
  }));
}

async function stopAgentSession(jobId, button) {
  if (!window.confirm("Stop this agent session?\n\nThe browser closes and the application is recorded as stopped, so nothing is queued again. If you completed it yourself, select I applied manually afterwards.")) return;
  button.disabled = true;
  try {
    await api(`/api/jobs/${jobId}/stop`, { method: "POST" });
    state.workerSignature = null;
    state.agentInputSignature = null;
    state.queueSignature = null;
    state.manualApplySignature = null;
    showToast("Stopping the agent session");
    await refreshAll();
  } catch (error) {
    showToast(error.message, true);
    button.disabled = false;
  }
}

function bindStopButtons(root) {
  root.querySelectorAll(".stop-agent").forEach(button => button.addEventListener("click", event => {
    const control = event.currentTarget;
    stopAgentSession(control.closest("[data-stop-job]").dataset.stopJob, control);
  }));
}

function renderManualApplications(items) {
  const signature = JSON.stringify(items);
  if (signature === state.manualApplySignature) return;
  state.manualApplySignature = signature;
  element("manualApplyCount").textContent = `${items.length} open`;
  const list = element("manualApplyList");
  if (!items.length) {
    list.innerHTML = '<div class="empty">No employer has blocked the browser agent, and no session was stopped.</div>';
    return;
  }
  list.innerHTML = items.map(item => `
    <div class="manual-apply-row" data-manual-job="${item.id}">
      <span class="queue-role"><strong>${escapeHtml(item.company)}</strong><span>${escapeHtml(item.role)}${item.location ? ` · ${escapeHtml(item.location)}` : ""}</span><em>${escapeHtml(item.detail)}</em></span>
      <span class="queue-origin">${item.handoff === "stopped" ? "You stopped it" : "Employer block"}</span>
      <span class="manual-apply-actions">
        <a class="text-button" href="${safeExternalUrl(item.application_url)}" target="_blank" rel="noopener noreferrer">Open listing ↗</a>
        <button class="button ink mark-applied-manually" type="button">I applied manually</button>
      </span>
    </div>`).join("");
  bindManualApplyButtons(list);
}

function bindManualApplyButtons(root) {
  root.querySelectorAll(".mark-applied-manually").forEach(button => button.addEventListener("click", event => {
    const control = event.currentTarget;
    markAppliedManually(control.closest("[data-manual-job]").dataset.manualJob, control);
  }));
}

async function markAppliedManually(jobId, button) {
  if (!window.confirm("Record this application as submitted by you?\n\nTI-AAA counts it in your application statistics and stops queueing this role for the browser agent.")) return;
  button.disabled = true;
  try {
    await api(`/api/jobs/${jobId}/applied-manually`, { method: "POST" });
    state.manualApplySignature = null;
    state.agentInputSignature = null;
    state.queueSignature = null;
    showToast("Recorded as applied; the role now appears in Applications");
    await refreshAll();
  } catch (error) {
    showToast(error.message, true);
    button.disabled = false;
  }
}

function agentInputControl(question) {
  const key = escapeHtml(question.input_key);
  const current = question.answer ?? "";
  const required = question.required ? " required" : "";
  if (question.input_type === "select") {
    return `<select data-agent-key="${key}"${required}><option value="">Choose an answer</option>${(question.options || []).map(option => `<option value="${escapeHtml(option)}"${String(option) === String(current) ? " selected" : ""}>${escapeHtml(option)}</option>`).join("")}</select>`;
  }
  if (question.input_type === "boolean") {
    return `<select data-agent-key="${key}" data-agent-kind="boolean"${required}><option value="">Choose yes or no</option><option value="true"${current === true ? " selected" : ""}>Yes</option><option value="false"${current === false ? " selected" : ""}>No</option></select>`;
  }
  if (question.input_type === "textarea") {
    return `<textarea data-agent-key="${key}" rows="3"${required}>${escapeHtml(current)}</textarea>`;
  }
  if (question.input_type === "verification_code") {
    return `<input data-agent-key="${key}" type="text" value="${escapeHtml(current)}" autocomplete="one-time-code" autocapitalize="off" spellcheck="false" maxlength="128" placeholder="Enter the one-time code"${required}>`;
  }
  const type = ["email", "tel", "number", "date"].includes(question.input_type) ? question.input_type : "text";
  return `<input data-agent-key="${key}" type="${type}" value="${escapeHtml(current)}"${required}>`;
}

function renderAgentInputs(workers) {
  const actionable = workers.filter(worker =>
    worker.job_id && (
      (worker.questions || []).length
      || worker.pipeline_status === "manual_review"
      || worker.submission_ready
    )
  );
  const signature = JSON.stringify(actionable.map(worker => ({
    job_id: worker.job_id,
    pipeline_status: worker.pipeline_status,
    availability_status: worker.availability_status,
    message: worker.message,
    review_detail: worker.review_detail,
    questions: worker.questions,
    submission_ready: worker.submission_ready,
    browser_interactive: worker.browser_interactive,
  })));
  if (signature === state.agentInputSignature) return;
  state.agentInputSignature = signature;
  const panel = element("agentInputPanel");
  if (!actionable.length) {
    panel.innerHTML = '<article class="agent-checkpoint quiet"><span>INPUT CHANNEL</span><p>The agent has not requested any information.</p></article>';
    return;
  }
  panel.innerHTML = actionable.map(worker => {
    const questions = worker.questions || [];
    const resumeName = worker.submitted_resume_name || worker.base_resume_name || "Prepared resume";
    if (worker.browser_interactive) {
      return `<article class="agent-checkpoint human-checkpoint" data-manual-job="${worker.job_id}" data-stop-job="${worker.job_id}">
        <header><div><p class="kicker">LIVE BROWSER CONTROL</p><h3>${escapeHtml(worker.company)} · ${escapeHtml(worker.role)}</h3></div><span>Same session</span></header>
        <p class="checkpoint-note">Use the interactive browser above to complete the CAPTCHA or inspect the stalled submission. Click Continue agent only after the challenge is cleared or a receipt is visible. If the challenge keeps coming back, stop the session—and record it if you finished the application yourself.</p>
        <div class="checkpoint-actions"><span>Resume: ${escapeHtml(resumeName)}</span><div><button class="button ghost stop-agent" type="button">Stop session</button><button class="button ink mark-applied-manually" type="button">I applied manually</button></div></div>
      </article>`;
    }
    if (questions.length && worker.availability_status !== "manual_only") {
      const verificationCodeRequested = questions.some(question => question.input_type === "verification_code");
      const checkpointTitle = verificationCodeRequested ? "VERIFICATION CODE NEEDED" : "CANDIDATE INPUT NEEDED";
      const checkpointNote = worker.review_detail || (verificationCodeRequested
        ? "Paste the one-time code sent by the employer. TI-AAA uses it only for this open application and clears it after the agent continues."
        : "Answer only with truthful information. TI-AAA keeps the current form open and continues it with your answers.");
      return `<article class="agent-checkpoint" data-agent-job="${worker.job_id}" data-stop-job="${worker.job_id}">
        <header><div><p class="kicker">${checkpointTitle}</p><h3>${escapeHtml(worker.company)} · ${escapeHtml(worker.role)}</h3></div><span>${questions.length} field${questions.length === 1 ? "" : "s"}</span></header>
        <p class="checkpoint-note">${escapeHtml(checkpointNote)}</p>
        <form class="agent-input-form">${questions.map(question => `<label>${escapeHtml(question.label)}${question.required ? "" : " <span>optional</span>"}${agentInputControl(question)}</label>`).join("")}
          <div class="checkpoint-actions"><span>Resume: ${escapeHtml(resumeName)}</span><div><button class="button ghost stop-agent" type="button">Stop session</button><button class="button ink" type="submit">Save answers & continue</button></div></div>
        </form></article>`;
    }
    if (worker.submission_ready) {
      return `<article class="agent-checkpoint submit-checkpoint" data-submit-job="${worker.job_id}" data-stop-job="${worker.job_id}">
        <header><div><p class="kicker">FINAL CONFIRMATION</p><h3>${escapeHtml(worker.company)} · ${escapeHtml(worker.role)}</h3></div><span>Form complete</span></header>
        <p class="checkpoint-note">The completed form is still open in the browser window above. Confirming below tells the agent to use that same form, click Submit once, and verify the receipt.</p>
        <div class="checkpoint-actions submit-actions"><span>Resume: ${escapeHtml(resumeName)}</span><div><button class="button ghost stop-agent" type="button">Stop session</button><button class="button signal submit-application" type="button">Submit application</button></div></div>
      </article>`;
    }
    const blocked = worker.availability_status === "manual_only";
    return `<article class="agent-checkpoint handoff" data-manual-job="${worker.job_id}">
      <header><div><p class="kicker">${blocked ? "EMPLOYER ACCESS BLOCK" : "MANUAL CHECKPOINT"}</p><h3>${escapeHtml(worker.company)} · ${escapeHtml(worker.role)}</h3></div><span>${blocked ? "Manual browser" : "Your review"}</span></header>
      <p class="checkpoint-note">${escapeHtml(worker.availability_detail || worker.review_detail || worker.message || "The agent cannot safely continue this step on its own.")}</p>
      <div class="checkpoint-actions"><span>Resume: ${escapeHtml(resumeName)}</span><div>${worker.resume_url ? `<a class="text-button" href="${escapeHtml(worker.resume_url)}" target="_blank">Open resume</a>` : ""}<button class="button ghost mark-applied-manually" type="button">I applied manually</button>${blocked ? `<a class="button ink" href="${safeExternalUrl(worker.application_url)}" target="_blank" rel="noopener noreferrer">Open manually</a>` : ""}</div></div>
    </article>`;
  }).join("");
  panel.querySelectorAll(".agent-input-form").forEach(form => form.addEventListener("submit", submitAgentInputs));
  panel.querySelectorAll(".submit-application").forEach(button => button.addEventListener("click", confirmApplicationSubmission));
  bindManualApplyButtons(panel);
  bindStopButtons(panel);
}

async function confirmApplicationSubmission(event) {
  const button = event.currentTarget;
  const card = button.closest("[data-submit-job]");
  if (!window.confirm("Submit this completed application now? This action cannot be undone.")) return;
  button.disabled = true;
  try {
    await api(`/api/jobs/${card.dataset.submitJob}/submit`, { method: "POST" });
    state.agentInputSignature = null;
    showToast("Submission confirmed; the agent is using the completed form");
    await refreshLive();
  } catch (error) {
    showToast(error.message, true);
    button.disabled = false;
  }
}

async function submitAgentInputs(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const card = form.closest("[data-agent-job]");
  const button = form.querySelector('button[type="submit"]');
  const answers = {};
  form.querySelectorAll("[data-agent-key]").forEach(control => {
    let answer = control.value;
    if (control.dataset.agentKind === "boolean" && answer) answer = answer === "true";
    answers[control.dataset.agentKey] = answer;
  });
  button.disabled = true;
  try {
    await api(`/api/jobs/${card.dataset.agentJob}/inputs`, {
      method: "POST", body: JSON.stringify({ answers }),
    });
    state.agentInputSignature = null;
    showToast("Answers saved; the agent is continuing");
    await refreshLive();
  } catch (error) {
    showToast(error.message, true);
    button.disabled = false;
  }
}

function renderEvents(items) {
  element("eventList").innerHTML = items.length ? items.map(item => `
    <div class="event-item"><i></i><p>${escapeHtml(item.company || "System")} <span>· ${escapeHtml(item.role || item.event_type)} · ${escapeHtml(item.event_type.replaceAll("_", " "))}${item.detail ? ` · ${escapeHtml(item.detail)}` : ""}</span></p><time>${escapeHtml(relativeTime(item.created_at))}</time></div>`).join("") : '<div class="empty">No activity recorded yet.</div>';
}

async function refreshWorkers() {
  const workers = await api("/api/workers");
  renderWorkers(workers.items);
  renderApplicationQueue(workers.queue || [], workers.queue_summary || {});
  renderManualApplications(workers.manual_applications || []);
}

async function refreshEvents() {
  const events = await api("/api/events?limit=18");
  renderEvents(events.items);
}

async function refreshLive() {
  await Promise.all([refreshWorkers(), refreshEvents()]);
}

function supportsWebPush() {
  return window.isSecureContext
    && "serviceWorker" in navigator
    && "PushManager" in window
    && "Notification" in window;
}

function pushKeyBytes(value) {
  const padding = "=".repeat((4 - value.length % 4) % 4);
  const decoded = atob((value + padding).replaceAll("-", "+").replaceAll("_", "/"));
  return Uint8Array.from(decoded, character => character.charCodeAt(0));
}

function renderWebPushStatus() {
  const autoEnabled = checked("autoMode");
  const option = element("webPushOption");
  const input = element("webPushNotifications");
  option.classList.toggle("hidden", !autoEnabled);
  option.classList.toggle("permission-blocked", state.webPush.permission === "denied");
  input.disabled = !autoEnabled || !state.webPush.supported || state.webPush.permission === "denied";

  let message = "Check this box to request browser permission.";
  if (!state.webPush.supported) {
    message = window.isSecureContext
      ? "This browser does not support Web Push."
      : "Web Push requires localhost or HTTPS.";
  } else if (state.webPush.permission === "denied") {
    message = "Notifications are blocked in this browser's site settings.";
  } else if (input.checked && state.webPush.subscribed) {
    const saved = Boolean(
      state.config?.settings?.automation?.auto_apply_new
      && state.config?.settings?.automation?.web_push_notifications
    );
    message = saved
      ? "Enabled on this browser. Alerts are sent only while Auto mode is on."
      : "Browser permission granted. Save all settings to activate alerts.";
  } else if (input.checked) {
    message = "This browser is not subscribed. Turn the checkbox off and on to reconnect.";
  } else if (state.webPush.subscribed) {
    message = "This browser is registered, but Auto-mode alerts are turned off.";
  }
  element("webPushStatus").textContent = message;
}

async function refreshWebPushState() {
  state.webPush.supported = supportsWebPush();
  state.webPush.permission = "Notification" in window ? Notification.permission : "unsupported";
  state.webPush.subscribed = false;
  if (state.webPush.supported) {
    const registration = await navigator.serviceWorker.getRegistration("/");
    state.webPush.subscribed = Boolean(await registration?.pushManager.getSubscription());
  }
  renderWebPushStatus();
}

async function enableWebPush() {
  if (!supportsWebPush()) throw new Error("This browser does not support Web Push on this address");
  const permission = await Notification.requestPermission();
  state.webPush.permission = permission;
  if (permission !== "granted") throw new Error("Browser notification permission was not granted");

  const push = await api("/api/push");
  const registration = await navigator.serviceWorker.register("/sw.js", {
    scope: "/", updateViaCache: "none",
  });
  await navigator.serviceWorker.ready;
  let subscription = await registration.pushManager.getSubscription();
  const priorKey = window.localStorage.getItem("tiaaaWebPushPublicKey");
  if (subscription && priorKey && priorKey !== push.public_key) {
    await subscription.unsubscribe();
    subscription = null;
  }
  if (!subscription) {
    subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: pushKeyBytes(push.public_key),
    });
  }
  const serialized = subscription.toJSON();
  if (!serialized.keys?.p256dh || !serialized.keys?.auth) {
    throw new Error("The browser returned an incomplete Push subscription");
  }
  await api("/api/push/subscriptions", {
    method: "POST",
    body: JSON.stringify({ endpoint: subscription.endpoint, keys: serialized.keys }),
  });
  window.localStorage.setItem("tiaaaWebPushPublicKey", push.public_key);
  state.webPush.subscribed = true;
}

async function disableWebPush() {
  if (!supportsWebPush()) return;
  const registration = await navigator.serviceWorker.getRegistration("/");
  const subscription = await registration?.pushManager.getSubscription();
  if (subscription) {
    await api("/api/push/subscriptions", {
      method: "DELETE",
      body: JSON.stringify({ endpoint: subscription.endpoint }),
    });
    await subscription.unsubscribe();
  }
  window.localStorage.removeItem("tiaaaWebPushPublicKey");
  state.webPush.subscribed = false;
}

async function handleWebPushToggle(event) {
  const input = event.currentTarget;
  input.disabled = true;
  try {
    if (input.checked) {
      if (!checked("autoMode")) throw new Error("Turn on Auto mode first");
      await enableWebPush();
      showToast("Browser ready. Save all settings to activate alerts.");
    } else {
      await disableWebPush();
      showToast("Browser unsubscribed. Save all settings to keep alerts off.");
    }
  } catch (error) {
    input.checked = false;
    showToast(error.message, true);
  } finally {
    await refreshWebPushState().catch(() => renderWebPushStatus());
  }
}

function renderAutoMode() {
  const enabled = checked("autoMode");
  const threshold = Number(element("autoModeMinimumFit").value) || 7;
  element("autoModePanel").classList.toggle("enabled", enabled);
  element("autoModeState").textContent = enabled
    ? `On · unattended at ${threshold}/10 or better`
    : "Off · manual applications only";
  element("autoModeFitValue").textContent = `${threshold} / 10`;
  renderWebPushStatus();
}

function handleAutoModeToggle() {
  const wasEnabled = Boolean(state.config?.settings?.automation?.auto_apply_new);
  renderAutoMode();
  if (checked("autoMode") && !wasEnabled) {
    showToast("Auto mode is on. Browser alerts are available below.");
  }
}

function populateConfiguration(config) {
  state.config = config;
  const profile = config.profile;
  const personal = profile.personal || {};
  const education = profile.education || {};
  const authorization = profile.work_authorization || {};
  const preferences = profile.preferences || {};
  const experience = profile.experience || {};
  const answers = profile.answers || {};
  const eeo = profile.eeo_voluntary || {};
  const settings = config.settings;
  const automation = settings.automation || {};
  const preparation = settings.preparation || {};
  const service = settings.service || {};
  const filters = settings.filters || {};
  setValue("fullName", personal.full_name); setValue("preferredName", personal.preferred_name);
  setValue("email", personal.email); setValue("phone", personal.phone);
  setValue("city", personal.city); setValue("state", personal.state); setValue("country", personal.country);
  setValue("address", personal.address); setValue("addressLine2", personal.address_line_2);
  setValue("postalCode", personal.postal_code); setValue("county", personal.county);
  setValue("linkedin", personal.linkedin_url); setValue("github", personal.github_url); setValue("portfolio", personal.portfolio_url);
  setValue("school", education.school); setValue("degree", education.degree); setValue("major", education.major);
  setValue("graduation", education.graduation_date); setValue("currentYear", education.current_year); setValue("gpa", education.gpa);
  setValue("workPermitType", authorization.work_permit_type);
  setChecked("authorized", authorization.legally_authorized_to_work_us); setChecked("sponsorship", authorization.requires_sponsorship);
  setChecked("citizen", authorization.us_citizen); setChecked("relocate", preferences.willing_to_relocate);
  setValue("roles", joinList(preferences.roles)); setValue("locations", joinList(preferences.locations)); setValue("terms", joinList(preferences.terms));
  const allSkills = Object.values(profile.skills || {}).flatMap(item => Array.isArray(item) ? item : []);
  setValue("skills", [...new Set(allSkills)].join(", "));
  setValue("previousInternshipCompanies", joinList(experience.previous_internship_companies));
  setValue("includeKeywords", joinList(filters.include_role_keywords)); setValue("excludeKeywords", joinList(filters.exclude_keywords));
  setValue("allowedLocations", joinList(filters.allowed_locations)); setChecked("remoteOnly", filters.remote_only);
  setValue("pollInterval", settings.poll_interval_seconds);
  setChecked("autoMode", automation.auto_apply_new);
  setChecked("manualAutoSubmit", automation.manual_auto_submit);
  setValue("autoModeMinimumFit", automation.auto_apply_minimum_fit_score ?? 7);
  setChecked("autoModeUsePreferences", automation.auto_apply_use_preferences);
  setChecked("webPushNotifications", automation.web_push_notifications);
  setValue("dayCap", automation.max_applications_per_day);
  setValue("cycleCap", automation.max_applications_per_cycle); setValue("claudeModel", automation.claude_model);
  setValue("maxAttempts", automation.max_attempts); setValue("workerTimeout", automation.timeout_seconds);
  setChecked("serviceEnabled", service.enabled); setChecked("autoPrepare", service.auto_prepare);
  setChecked("useLlm", preparation.use_llm);
  setChecked("generateCoverLetters", preparation.generate_cover_letters);
  setChecked("headless", automation.headless);
  renderAutoMode();
  setValue("availableStartDate", answers.available_start_date); setValue("howHeard", answers.how_heard);
  setChecked("age18", answers.age_18_or_older); setChecked("previouslyWorked", answers.previously_worked_here);
  setValue("eeoGender", eeo.gender); setValue("eeoRace", eeo.race_ethnicity);
  setValue("eeoVeteran", eeo.veteran_status); setValue("eeoDisability", eeo.disability_status);
  setValue("onboardName", personal.full_name?.startsWith("YOUR ") ? "" : personal.full_name);
  setValue("onboardEmail", personal.email === "you@example.com" ? "" : personal.email);
  setValue("onboardPhone", personal.phone); setValue("onboardSchool", education.school?.startsWith("YOUR ") ? "" : education.school);
  setValue("onboardMajor", education.major || "Computer Science"); setValue("onboardGraduation", education.graduation_date);
  setChecked("onboardAuthorized", authorization.legally_authorized_to_work_us);
  setChecked("onboardSponsorship", authorization.requires_sponsorship);
}

function configurationPayload() {
  const profile = clone(state.config.profile);
  profile.personal = profile.personal || {};
  Object.assign(profile.personal, {
    full_name: value("fullName"), preferred_name: value("preferredName"), email: value("email"),
    phone: value("phone"), city: value("city"), state: value("state"), country: value("country"),
    address: value("address"), address_line_2: value("addressLine2"), county: value("county"),
    postal_code: value("postalCode"), linkedin_url: value("linkedin"),
    github_url: value("github"), portfolio_url: value("portfolio"),
  });
  profile.education = profile.education || {};
  Object.assign(profile.education, {
    school: value("school"), degree: value("degree"), major: value("major"),
    graduation_date: value("graduation"), current_year: value("currentYear"), gpa: value("gpa"),
  });
  profile.work_authorization = profile.work_authorization || {};
  Object.assign(profile.work_authorization, {
    legally_authorized_to_work_us: checked("authorized"), requires_sponsorship: checked("sponsorship"),
    us_citizen: checked("citizen"), work_permit_type: value("workPermitType"),
  });
  profile.preferences = profile.preferences || {};
  Object.assign(profile.preferences, {
    roles: splitList(value("roles")), locations: splitList(value("locations")), terms: splitList(value("terms")), willing_to_relocate: checked("relocate"),
  });
  profile.skills = { keywords: splitList(value("skills")) };
  profile.experience = profile.experience || {};
  profile.experience.previous_internship_companies = splitList(value("previousInternshipCompanies"));
  profile.answers = profile.answers || {};
  Object.assign(profile.answers, {
    available_start_date: value("availableStartDate"), how_heard: value("howHeard"),
    age_18_or_older: checked("age18"), previously_worked_here: checked("previouslyWorked"),
  });
  profile.eeo_voluntary = {
    gender: value("eeoGender"), race_ethnicity: value("eeoRace"),
    veteran_status: value("eeoVeteran"), disability_status: value("eeoDisability"),
  };

  const settings = clone(state.config.settings);
  settings.poll_interval_seconds = Number(value("pollInterval")) || 300;
  settings.filters = settings.filters || {};
  settings.filters.include_role_keywords = splitList(value("includeKeywords"));
  settings.filters.exclude_keywords = splitList(value("excludeKeywords"));
  settings.filters.allowed_locations = splitList(value("allowedLocations"));
  settings.filters.remote_only = checked("remoteOnly");
  settings.service = settings.service || {};
  settings.service.enabled = checked("serviceEnabled") || checked("autoMode");
  settings.service.auto_prepare = checked("autoPrepare");
  settings.preparation = settings.preparation || {};
  delete settings.preparation.tailor_resumes;
  settings.preparation.use_llm = checked("useLlm");
  settings.preparation.generate_cover_letters = checked("generateCoverLetters");
  settings.automation = settings.automation || {};
  delete settings.automation.auto_apply_eligible_only;
  Object.assign(settings.automation, {
    auto_apply_new: checked("autoMode"), headless: checked("headless"),
    manual_auto_submit: checked("manualAutoSubmit"),
    auto_apply_use_preferences: checked("autoModeUsePreferences"),
    web_push_notifications: checked("autoMode") && checked("webPushNotifications"),
    auto_apply_minimum_fit_score: Number(element("autoModeMinimumFit").value) || 7,
    max_applications_per_day: Number(value("dayCap")) || 25,
    max_applications_per_cycle: Number(value("cycleCap")) || 5, max_attempts: Number(value("maxAttempts")) || 3,
    timeout_seconds: Number(value("workerTimeout")) || 600, claude_model: value("claudeModel") || "sonnet",
  });
  return { profile, settings };
}

async function saveConfiguration(event) {
  event.preventDefault();
  const button = event.submitter;
  if (checked("autoMode") && !state.claudeAuth?.logged_in) {
    showToast("Connect Claude Code or turn off browser automation", true);
    return;
  }
  if (button) button.disabled = true;
  try {
    const config = await api("/api/config", { method: "PUT", body: JSON.stringify(configurationPayload()) });
    populateConfiguration(config);
    await refreshClaudeAuth();
    showToast("Settings saved; the background agent has been notified");
  } catch (error) { showToast(error.message, true); }
  finally { if (button) button.disabled = false; }
}

async function uploadResume(form, onboarding = false) {
  const data = new FormData(form);
  const file = data.get("file");
  if ((!file || !file.size) && onboarding && state.resumes.length) {
    setOnboardingStep(3);
    return;
  }
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    await api("/api/resumes", { method: "POST", body: data });
    form.reset();
    await loadResumes();
    showToast("Resume stored securely");
    if (onboarding) {
      element("onboardResumeStatus").textContent = "Resume ready. You can add more versions later.";
      setOnboardingStep(3);
    }
  } catch (error) {
    showToast(error.message, true);
    if (onboarding) element("onboardResumeStatus").textContent = error.message;
  } finally { button.disabled = false; }
}

function setOnboardingStep(step) {
  state.onboardingStep = Math.max(0, Math.min(3, step));
  document.querySelectorAll(".onboard-step").forEach(node => node.classList.toggle("active", Number(node.dataset.step) === state.onboardingStep));
  element("onboardingProgress").querySelectorAll("li").forEach((node, index) => node.classList.toggle("active", index <= state.onboardingStep));
  if (state.onboardingStep === 2 && state.resumes.length) {
    const form = element("onboardResumeForm");
    form.querySelector('input[type="file"]').required = false;
    form.querySelector('button[type="submit"]').textContent = `Continue with ${state.resumes.length} saved resume${state.resumes.length === 1 ? "" : "s"}`;
    element("onboardResumeStatus").textContent = "A resume is already saved. Upload another or continue.";
  }
}

async function finishOnboarding() {
  const button = element("finishOnboarding");
  button.disabled = true;
  try {
    const profile = clone(state.config.profile);
    profile.personal = profile.personal || {};
    Object.assign(profile.personal, { full_name: value("onboardName"), email: value("onboardEmail"), phone: value("onboardPhone") });
    profile.education = profile.education || {};
    Object.assign(profile.education, { school: value("onboardSchool"), major: value("onboardMajor"), graduation_date: value("onboardGraduation") });
    profile.work_authorization = profile.work_authorization || {};
    Object.assign(profile.work_authorization, {
      legally_authorized_to_work_us: checked("onboardAuthorized"), requires_sponsorship: checked("onboardSponsorship"),
    });
    const settings = clone(state.config.settings);
    settings.service.enabled = true; settings.service.auto_prepare = true;
    delete settings.preparation.tailor_resumes;
    settings.automation.auto_apply_new = false;
    settings.automation.auto_apply_use_preferences = false;
    const config = await api("/api/config", {
      method: "PUT", body: JSON.stringify({ profile, settings, onboarding_complete: true }),
    });
    populateConfiguration(config);
    await refreshClaudeAuth();
    element("onboarding").classList.add("hidden");
    await api("/api/dashboard/visit", { method: "POST" });
    showToast("Setup complete. Browse Latest jobs whenever you are ready.");
    await refreshAll();
  } catch (error) { showToast(error.message, true); }
  finally { button.disabled = false; }
}

function closeWelcomeBack() {
  element("welcomeBack").classList.add("hidden");
}

function renderWelcomeBack(visit) {
  const applications = visit.applications || [];
  const failures = visit.failures || [];
  if (visit.first_visit) return;
  const activityCount = applications.length + failures.length;
  element("welcomeBackTitle").textContent = activityCount ? "The agent kept working." : "You are caught up.";
  const summary = [];
  if (applications.length) summary.push(`${applications.length} submitted`);
  if (failures.length) summary.push(`${failures.length} stopped with a recorded reason`);
  element("welcomeBackSummary").textContent = activityCount
    ? `${summary.join(" · ")} since your last dashboard visit.`
    : "No Auto mode results were recorded since your last dashboard visit.";
  const list = element("welcomeBackApplications");
  const submittedMarkup = applications.length ? `<p class="welcome-group-label">Submitted</p>${applications.map(job => `
    <button class="welcome-application" type="button" data-welcome-job="${job.id}" data-welcome-kind="applied">
      <span class="avatar">${escapeHtml(initials(job.company))}</span>
      <span><strong>${escapeHtml(job.company)}</strong><span>${escapeHtml(job.role)} · ${escapeHtml(job.submitted_resume_name || "Resume not recorded")}</span></span>
      <time>${escapeHtml(relativeTime(job.applied_at))}</time>
    </button>`).join("")}` : "";
  const failureMarkup = failures.length ? `<p class="welcome-group-label">Stopped safely</p>${failures.map(job => `
    <button class="welcome-application failed" type="button" data-welcome-job="${job.id}" data-welcome-kind="failed">
      <span class="avatar">!</span>
      <span><strong>${escapeHtml(job.company)}</strong><span>${escapeHtml(job.role)}</span><em>${escapeHtml(job.apply_error || "Auto mode could not complete this application.")}</em></span>
      <time>${escapeHtml(relativeTime(job.updated_at))}</time>
    </button>`).join("")}` : "";
  list.innerHTML = activityCount
    ? `${submittedMarkup}${failureMarkup}`
    : '<p class="welcome-empty">There is no new application activity to review.</p>';
  list.querySelectorAll("[data-welcome-job]").forEach(button => button.addEventListener("click", () => {
    closeWelcomeBack();
    setView(button.dataset.welcomeKind === "applied" ? "applications" : "latest");
    openJobDetail(button.dataset.welcomeJob);
  }));
  element("welcomeBack").classList.remove("hidden");
}

async function loadWelcomeBack() {
  renderWelcomeBack(await api("/api/dashboard/visit", { method: "POST" }));
}

async function refreshAll() {
  const [stats, analytics, sources, service] = await Promise.all([
    api("/api/stats"),
    api("/api/analytics"),
    api("/api/sources"),
    api("/api/service"),
    loadJobs(),
    loadLatestJobs(),
  ]);
  renderStats(stats); renderAnalytics(analytics); renderSources(sources.items); renderService(service);
  element("lastUpdated").textContent = `Local state · ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
  if (state.activeView === "live") await refreshLive();
}

async function initialize() {
  try {
    const [config, onboarding, resumes, claudeAuth] = await Promise.all([api("/api/config"), api("/api/onboarding"), api("/api/resumes"), api("/api/claude-auth")]);
    populateConfiguration(config);
    await refreshWebPushState();
    renderClaudeAuth(claudeAuth);
    state.onboarding = onboarding;
    renderResumes(resumes.items);
    if (!onboarding.complete) {
      element("onboarding").classList.remove("hidden");
      setOnboardingStep(0);
    }
    await refreshAll();
    if (onboarding.complete) await loadWelcomeBack();
    const requestedView = new URLSearchParams(window.location.search).get("view");
    if (requestedView && viewCopy[requestedView]) setView(requestedView);
  } catch (error) {
    showToast(error.message, true);
    element("serviceMessage").textContent = error.message;
  }
}

document.querySelectorAll("[data-view-target]").forEach(button => button.addEventListener("click", () => setView(button.dataset.viewTarget)));
document.querySelectorAll("[data-analytics-dimension]").forEach(button => button.addEventListener("click", () => {
  state.analyticsDimension = button.dataset.analyticsDimension;
  renderAnalyticsBreakdown();
}));
element("settingsForm").addEventListener("submit", saveConfiguration);
element("resumeForm").addEventListener("submit", event => { event.preventDefault(); uploadResume(event.currentTarget); });
element("onboardResumeForm").addEventListener("submit", event => { event.preventDefault(); uploadResume(event.currentTarget, true); });
document.querySelectorAll(".next-step").forEach(button => button.addEventListener("click", () => {
  if (state.onboardingStep === 1 && (!value("onboardName") || !value("onboardEmail"))) {
    showToast("Name and email are required", true); return;
  }
  setOnboardingStep(state.onboardingStep + 1);
}));
document.querySelectorAll(".prev-step").forEach(button => button.addEventListener("click", () => setOnboardingStep(state.onboardingStep - 1)));
element("finishOnboarding").addEventListener("click", finishOnboarding);
element("connectClaude").addEventListener("click", event => startClaudeLogin(event.currentTarget));
element("onboardConnectClaude").addEventListener("click", event => startClaudeLogin(event.currentTarget));
element("completeClaude").addEventListener("click", event => completeClaudeLogin("claudeLoginCode", event.currentTarget));
element("onboardCompleteClaude").addEventListener("click", event => completeClaudeLogin("onboardClaudeLoginCode", event.currentTarget));
element("disconnectClaude").addEventListener("click", async event => {
  if (!window.confirm("Disconnect this Claude account from TI-AAA?")) return;
  event.currentTarget.disabled = true;
  try { renderClaudeAuth(await api("/api/claude-auth", { method: "DELETE" })); showToast("Claude account disconnected"); }
  catch (error) { showToast(error.message, true); }
  finally { event.currentTarget.disabled = false; }
});
element("autoMode").addEventListener("change", handleAutoModeToggle);
element("autoModeMinimumFit").addEventListener("input", renderAutoMode);
element("webPushNotifications").addEventListener("change", handleWebPushToggle);
element("closeWelcomeBack").addEventListener("click", closeWelcomeBack);
element("welcomeBackDone").addEventListener("click", closeWelcomeBack);
element("welcomeBackTracker").addEventListener("click", () => { closeWelcomeBack(); setView("applications"); });
element("welcomeBack").addEventListener("click", event => { if (event.target === event.currentTarget) closeWelcomeBack(); });
element("statusFilter").addEventListener("change", () => loadJobs().catch(error => showToast(error.message, true)));
element("searchInput").addEventListener("input", () => {
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(() => loadJobs().catch(error => showToast(error.message, true)), 250);
});
element("latestEligibility").addEventListener("change", renderLatestJobs);
element("latestSearch").addEventListener("input", () => {
  clearTimeout(state.latestSearchTimer);
  state.latestSearchTimer = setTimeout(() => loadLatestJobs().catch(error => showToast(error.message, true)), 250);
});
element("closeJobDetail").addEventListener("click", closeJobDetail);
element("jobDetailScrim").addEventListener("click", event => {
  if (event.target === event.currentTarget) closeJobDetail();
});
element("applyJobButton").addEventListener("click", event => requestJobApplication(event.currentTarget.dataset.jobId, event.currentTarget));
const workerGrid = element("workerGrid");
const remoteBrowserKeys = new Set([
  "Backspace", "Tab", "Enter", "Escape", "PageUp", "PageDown", "End", "Home",
  "ArrowLeft", "ArrowUp", "ArrowRight", "ArrowDown", "Delete",
]);
workerGrid.addEventListener("pointerdown", event => {
  const canvas = event.target.closest("canvas[data-browser-control]");
  if (canvas) canvas.focus();
});
workerGrid.addEventListener("click", event => {
  const stopButton = event.target.closest(".stop-agent");
  if (stopButton) {
    stopAgentSession(stopButton.closest("[data-stop-job]").dataset.stopJob, stopButton);
    return;
  }
  const returnButton = event.target.closest("[data-return-browser-control]");
  if (returnButton) {
    returnBrowserControl(returnButton);
    return;
  }
  const canvas = event.target.closest("canvas[data-browser-control]");
  if (!canvas) return;
  event.preventDefault();
  const point = browserControlPoint(canvas, event);
  if (point) sendBrowserControl(canvas, { type: "click", ...point });
});
workerGrid.addEventListener("keydown", event => {
  const canvas = event.target.closest("canvas[data-browser-control]");
  if (!canvas) return;
  let action = null;
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
    action = { type: "key", key: "SelectAll" };
  } else if (!event.ctrlKey && !event.metaKey && !event.altKey && remoteBrowserKeys.has(event.key)) {
    action = { type: "key", key: event.key };
  } else if (!event.ctrlKey && !event.metaKey && !event.altKey && event.key.length === 1) {
    action = { type: "text", text: event.key };
  }
  if (!action) return;
  event.preventDefault();
  event.stopPropagation();
  sendBrowserControl(canvas, action);
});
workerGrid.addEventListener("paste", event => {
  const canvas = event.target.closest("canvas[data-browser-control]");
  const text = event.clipboardData?.getData("text");
  if (!canvas || !text) return;
  event.preventDefault();
  sendBrowserControl(canvas, { type: "text", text: text.slice(0, 2000) });
});
workerGrid.addEventListener("wheel", event => {
  const canvas = event.target.closest("canvas[data-browser-control]");
  if (!canvas) return;
  event.preventDefault();
  const point = browserControlPoint(canvas, event);
  if (!point) return;
  sendBrowserControl(canvas, {
    type: "scroll",
    ...point,
    delta_x: Math.max(-1200, Math.min(1200, event.deltaX)),
    delta_y: Math.max(-1200, Math.min(1200, event.deltaY)),
  });
}, { passive: false });
document.addEventListener("keydown", event => { if (event.key === "Escape") { closeJobDetail(); closeWelcomeBack(); } });
element("runButton").addEventListener("click", async () => {
  try { await api("/api/service/run", { method: "POST" }); showToast("Repository check scheduled"); }
  catch (error) { showToast(error.message, true); }
});
element("pauseButton").addEventListener("click", async () => {
  const paused = element("pauseButton").textContent === "Resume";
  try {
    await api(`/api/service/${paused ? "resume" : "pause"}`, { method: "POST" });
    showToast(paused ? "Background service resumed" : "Background service paused");
    renderService(await api("/api/service"));
  } catch (error) { showToast(error.message, true); }
});

initialize();
setInterval(() => refreshAll().catch(error => showToast(error.message, true)), 15000);
setInterval(() => {
  if (state.activeView === "live") refreshWorkers().catch(() => {});
}, 1000);
setInterval(() => {
  if (state.activeView === "live") refreshEvents().catch(() => {});
}, 2500);
