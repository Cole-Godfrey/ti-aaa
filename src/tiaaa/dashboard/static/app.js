const state = {
  config: null,
  onboarding: null,
  resumes: [],
  jobs: [],
  activeView: "overview",
  searchTimer: null,
  onboardingStep: 0,
};

const viewCopy = {
  overview: ["THE DAILY BRIEF", "Your internship desk", "A quiet view of what the agent found, prepared, and submitted."],
  applications: ["APPLICATION LEDGER", "Your opportunity record", "Update outcomes and see exactly which resume accompanied each application."],
  live: ["AGENT OBSERVATORY", "The work in progress", "Follow browser workers without keeping the dashboard open."],
  resumes: ["FACT LIBRARY", "Resumes for each kind of role", "Keep multiple truthful versions; TI-AAA selects the closest match."],
  settings: ["CONTROL ROOM", "Your rules, your data", "Configure the entire local service without editing a file."],
};
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

function showToast(message, error = false) {
  const toast = element("toast");
  toast.textContent = message;
  toast.className = `toast show${error ? " error" : ""}`;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.className = "toast"; }, 3200);
}

function setView(view) {
  if (!viewCopy[view]) return;
  state.activeView = view;
  document.querySelectorAll(".view").forEach(node => node.classList.toggle("active", node.dataset.view === view));
  document.querySelectorAll(".nav-item").forEach(node => node.classList.toggle("active", node.dataset.viewTarget === view));
  const [kicker, title, subtitle] = viewCopy[view];
  element("viewKicker").textContent = kicker;
  element("viewTitle").textContent = title;
  element("viewSubtitle").textContent = subtitle;
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (view === "live") refreshLive().catch(error => showToast(error.message, true));
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
      <time>${escapeHtml(relativeTime(job.applied_at))}</time></div>`).join("") : '<div class="empty">No applications yet. New repository additions will appear here.</div>';
}

function optionsMarkup(options, current, disabled = []) {
  return options.map(([key, label]) => `<option value="${key}"${key === current ? " selected" : ""}${disabled.includes(key) ? " disabled" : ""}>${label}</option>`).join("");
}

function renderJobs(jobs) {
  state.jobs = jobs;
  const body = element("jobsTableBody");
  if (!jobs.length) {
    body.innerHTML = '<tr><td colspan="6" class="loading">No internships match this view.</td></tr>';
    element("tableSummary").textContent = "0 internships shown";
    return;
  }
  body.innerHTML = jobs.map(job => {
    const resumeName = job.submitted_resume_name || job.base_resume_name;
    const resumeLabel = job.submitted_resume_name ? "submitted" : "selected";
    const protectedStates = job.discovered_as_new ? [] : ["queued", "ready", "applying", "failed"];
    return `<tr data-job-id="${job.id}">
      <td class="role-cell"><strong>${escapeHtml(job.company)}</strong><span>${escapeHtml(job.role)} · ${escapeHtml(job.location || "location not listed")}</span></td>
      <td><select class="status-select pipeline-select" aria-label="Pipeline status for ${escapeHtml(job.company)}" title="${job.discovered_as_new ? "" : "Protected first-sync baseline"}">${optionsMarkup(pipelineOptions, job.pipeline_status, protectedStates)}</select></td>
      <td class="resume-cell">${resumeName ? `<a href="/api/jobs/${job.id}/resume" target="_blank">${escapeHtml(resumeName)}</a><span>${resumeLabel}</span>` : "—"}</td>
      <td><select class="status-select outcome-select" aria-label="Outcome for ${escapeHtml(job.company)}">${optionsMarkup(outcomeOptions, job.outcome_status)}</select></td>
      <td>${escapeHtml(shortDate(job.first_seen_at))}</td>
      <td><a class="open-link" href="${safeExternalUrl(job.application_url)}" target="_blank" rel="noopener noreferrer" title="Open application">↗</a></td>
    </tr>`;
  }).join("");
  element("tableSummary").textContent = `${jobs.length} internship${jobs.length === 1 ? "" : "s"} shown · tracker changes save immediately`;
  body.querySelectorAll(".pipeline-select").forEach(select => select.addEventListener("change", event => {
    updateJob(event.target.closest("tr").dataset.jobId, { pipeline_status: event.target.value });
  }));
  body.querySelectorAll(".outcome-select").forEach(select => select.addEventListener("change", event => {
    updateJob(event.target.closest("tr").dataset.jobId, { outcome_status: event.target.value });
  }));
}

async function updateJob(jobId, payload) {
  try {
    await api(`/api/jobs/${jobId}`, { method: "PATCH", body: JSON.stringify(payload) });
    showToast("Application tracker updated");
    renderStats(await api("/api/stats"));
  } catch (error) {
    showToast(error.message, true);
    await loadJobs();
  }
}

async function loadJobs() {
  const parameters = new URLSearchParams({ limit: "250" });
  const search = value("searchInput");
  const status = element("statusFilter").value;
  if (search) parameters.set("search", search);
  if (status !== "all") parameters.set("status", status);
  const response = await api(`/api/jobs?${parameters}`);
  renderJobs(response.items);
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
      <div class="source-meta"><span>${documents.length} document${documents.length === 1 ? "" : "s"}</span><span>${escapeHtml(latest ? relativeTime(latest) : "awaiting baseline")}</span></div></article>`;
  }).join("") : '<div class="empty">The first background poll will initialize all three repositories.</div>';
}

function renderService(service) {
  const status = String(service.process_running ? (service.service_status || "starting") : "dashboard_only");
  const banner = element("serviceBanner");
  const good = ["waiting", "syncing", "preparing", "applying", "starting"].includes(status);
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

function renderWorkers(items) {
  element("workerGrid").innerHTML = items.length ? items.map(worker => `
    <article class="worker-card"><div class="worker-head"><div class="worker-title"><strong>${escapeHtml(worker.company || worker.worker_id)}</strong><span>${escapeHtml(worker.role || "Waiting for a prepared application")}</span></div>
      <span class="worker-state ${escapeHtml(worker.status)}">${escapeHtml(worker.status)}</span></div>
      <div class="preview-frame">${worker.preview_available ? `<img src="${escapeHtml(worker.preview_url)}?t=${Date.now()}" alt="Current browser snapshot for ${escapeHtml(worker.worker_id)}">` : '<p class="preview-empty">A browser snapshot appears here when a worker starts.</p>'}</div>
      <p class="worker-message">${escapeHtml(worker.message || "Waiting for the next cycle")}</p></article>`).join("") : '<div class="empty tall">No browser worker has run yet. Enable browser automation in Settings, or keep watch-and-prepare mode.</div>';
}

function renderEvents(items) {
  element("eventList").innerHTML = items.length ? items.map(item => `
    <div class="event-item"><i></i><p>${escapeHtml(item.company || "System")} <span>· ${escapeHtml(item.role || item.event_type)} · ${escapeHtml(item.event_type.replaceAll("_", " "))}${item.detail ? ` · ${escapeHtml(item.detail)}` : ""}</span></p><time>${escapeHtml(relativeTime(item.created_at))}</time></div>`).join("") : '<div class="empty">No activity recorded yet.</div>';
}

async function refreshLive() {
  const [workers, events] = await Promise.all([api("/api/workers"), api("/api/events?limit=18")]);
  renderWorkers(workers.items);
  renderEvents(events.items);
}

function secretLabel(status) {
  return status?.configured ? `saved${status.suffix ? ` ···${status.suffix}` : ""}` : "not set";
}

function populateConfiguration(config) {
  state.config = config;
  const profile = config.profile;
  const personal = profile.personal || {};
  const education = profile.education || {};
  const authorization = profile.work_authorization || {};
  const preferences = profile.preferences || {};
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
  setValue("address", personal.address); setValue("postalCode", personal.postal_code);
  setValue("linkedin", personal.linkedin_url); setValue("github", personal.github_url); setValue("portfolio", personal.portfolio_url);
  setValue("school", education.school); setValue("degree", education.degree); setValue("major", education.major);
  setValue("graduation", education.graduation_date); setValue("currentYear", education.current_year); setValue("gpa", education.gpa);
  setValue("workPermitType", authorization.work_permit_type);
  setChecked("authorized", authorization.legally_authorized_to_work_us); setChecked("sponsorship", authorization.requires_sponsorship);
  setChecked("citizen", authorization.us_citizen); setChecked("relocate", preferences.willing_to_relocate);
  setValue("roles", joinList(preferences.roles)); setValue("locations", joinList(preferences.locations)); setValue("terms", joinList(preferences.terms));
  const allSkills = Object.values(profile.skills || {}).flatMap(item => Array.isArray(item) ? item : []);
  setValue("skills", [...new Set(allSkills)].join(", "));
  setValue("includeKeywords", joinList(filters.include_role_keywords)); setValue("excludeKeywords", joinList(filters.exclude_keywords));
  setValue("allowedLocations", joinList(filters.allowed_locations)); setChecked("remoteOnly", filters.remote_only);
  setValue("pollInterval", settings.poll_interval_seconds); setValue("minimumFit", settings.minimum_fit_score);
  setValue("workerCount", automation.workers); setValue("dayCap", automation.max_applications_per_day);
  setValue("cycleCap", automation.max_applications_per_cycle); setValue("claudeModel", automation.claude_model);
  setValue("maxAttempts", automation.max_attempts); setValue("workerTimeout", automation.timeout_seconds);
  setChecked("serviceEnabled", service.enabled); setChecked("autoPrepare", service.auto_prepare);
  setChecked("tailorResumes", preparation.tailor_resumes); setChecked("useLlm", preparation.use_llm);
  setChecked("generateCoverLetters", preparation.generate_cover_letters);
  setChecked("automationEnabled", automation.enabled); setChecked("allowSubmission", automation.allow_submission);
  setChecked("headless", automation.headless);
  setValue("availableStartDate", answers.available_start_date); setValue("howHeard", answers.how_heard);
  setChecked("age18", answers.age_18_or_older); setChecked("previouslyWorked", answers.previously_worked_here);
  setValue("eeoGender", eeo.gender); setValue("eeoRace", eeo.race_ethnicity);
  setValue("eeoVeteran", eeo.veteran_status); setValue("eeoDisability", eeo.disability_status);
  element("anthropicState").textContent = secretLabel(config.secrets.ANTHROPIC_API_KEY);
  element("githubState").textContent = secretLabel(config.secrets.GITHUB_TOKEN);
  element("openaiState").textContent = secretLabel(config.secrets.OPENAI_API_KEY);
  element("geminiState").textContent = secretLabel(config.secrets.GEMINI_API_KEY);
  element("applicationPasswordState").textContent = secretLabel(config.secrets.TIAAA_APPLICATION_PASSWORD);

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
    address: value("address"), postal_code: value("postalCode"), linkedin_url: value("linkedin"),
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
  settings.minimum_fit_score = Number(value("minimumFit")) || 5;
  settings.filters = settings.filters || {};
  settings.filters.include_role_keywords = splitList(value("includeKeywords"));
  settings.filters.exclude_keywords = splitList(value("excludeKeywords"));
  settings.filters.allowed_locations = splitList(value("allowedLocations"));
  settings.filters.remote_only = checked("remoteOnly");
  settings.service = settings.service || {};
  settings.service.enabled = checked("serviceEnabled"); settings.service.auto_prepare = checked("autoPrepare");
  settings.preparation = settings.preparation || {};
  settings.preparation.tailor_resumes = checked("tailorResumes"); settings.preparation.use_llm = checked("useLlm");
  settings.preparation.generate_cover_letters = checked("generateCoverLetters");
  settings.automation = settings.automation || {};
  Object.assign(settings.automation, {
    enabled: checked("automationEnabled"), allow_submission: checked("allowSubmission"), headless: checked("headless"),
    workers: Number(value("workerCount")) || 1, max_applications_per_day: Number(value("dayCap")) || 25,
    max_applications_per_cycle: Number(value("cycleCap")) || 5, max_attempts: Number(value("maxAttempts")) || 3,
    timeout_seconds: Number(value("workerTimeout")) || 600, claude_model: value("claudeModel") || "sonnet",
  });
  const secrets = {};
  if (value("anthropicKey")) secrets.ANTHROPIC_API_KEY = value("anthropicKey");
  if (value("githubToken")) secrets.GITHUB_TOKEN = value("githubToken");
  if (value("openaiKey")) secrets.OPENAI_API_KEY = value("openaiKey");
  if (value("geminiKey")) secrets.GEMINI_API_KEY = value("geminiKey");
  if (value("applicationPassword")) secrets.TIAAA_APPLICATION_PASSWORD = value("applicationPassword");
  return { profile, settings, secrets };
}

async function saveConfiguration(event) {
  event.preventDefault();
  const button = event.submitter;
  if (button) button.disabled = true;
  try {
    const config = await api("/api/config", { method: "PUT", body: JSON.stringify(configurationPayload()) });
    populateConfiguration(config);
    ["anthropicKey", "githubToken", "openaiKey", "geminiKey", "applicationPassword"].forEach(id => setValue(id, ""));
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
    const mode = document.querySelector('input[name="onboardMode"]:checked').value;
    settings.service.enabled = true; settings.service.auto_prepare = true; settings.preparation.tailor_resumes = true;
    settings.automation.enabled = mode !== "watch"; settings.automation.allow_submission = mode === "submit";
    const secrets = {};
    if (value("onboardAnthropic")) secrets.ANTHROPIC_API_KEY = value("onboardAnthropic");
    if (value("onboardApplicationPassword")) secrets.TIAAA_APPLICATION_PASSWORD = value("onboardApplicationPassword");
    const config = await api("/api/config", {
      method: "PUT", body: JSON.stringify({ profile, settings, secrets, onboarding_complete: true }),
    });
    populateConfiguration(config);
    element("onboarding").classList.add("hidden");
    showToast("Setup complete. TI-AAA is watching for new listings.");
    await refreshAll();
  } catch (error) { showToast(error.message, true); }
  finally { button.disabled = false; }
}

async function refreshAll() {
  const [stats, sources, service] = await Promise.all([api("/api/stats"), api("/api/sources"), api("/api/service"), loadJobs()]);
  renderStats(stats); renderSources(sources.items); renderService(service);
  element("lastUpdated").textContent = `Local state · ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
  if (state.activeView === "live") await refreshLive();
}

async function initialize() {
  try {
    const [config, onboarding, resumes] = await Promise.all([api("/api/config"), api("/api/onboarding"), api("/api/resumes")]);
    populateConfiguration(config);
    state.onboarding = onboarding;
    renderResumes(resumes.items);
    if (!onboarding.complete) {
      element("onboarding").classList.remove("hidden");
      setOnboardingStep(0);
    }
    await refreshAll();
  } catch (error) {
    showToast(error.message, true);
    element("serviceMessage").textContent = error.message;
  }
}

document.querySelectorAll("[data-view-target]").forEach(button => button.addEventListener("click", () => setView(button.dataset.viewTarget)));
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
document.querySelectorAll(".clear-secret").forEach(button => button.addEventListener("click", async () => {
  if (!window.confirm("Forget this saved value?")) return;
  try {
    const config = await api("/api/config", {
      method: "PUT", body: JSON.stringify({ clear_secrets: [button.dataset.secret] }),
    });
    populateConfiguration(config);
    showToast("Saved value removed");
  } catch (error) { showToast(error.message, true); }
}));
element("statusFilter").addEventListener("change", () => loadJobs().catch(error => showToast(error.message, true)));
element("searchInput").addEventListener("input", () => {
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(() => loadJobs().catch(error => showToast(error.message, true)), 250);
});
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
  if (state.activeView === "live") refreshLive().catch(() => {});
}, 2500);
