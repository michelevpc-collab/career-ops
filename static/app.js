const listNode = document.querySelector("#company-list");
const detailNode = document.querySelector("#detail-pane");
const searchNode = document.querySelector("#search");
const countNode = document.querySelector("#result-count");
const moreButton = document.querySelector("#load-more");
const toastNode = document.querySelector("#toast");

let selectedId = null;
let selectedRecord = null;
let offset = 0;
let lastQuery = "";
let debounceTimer;

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function safeLink(value) {
  const text = esc(value);
  if (/^https?:\/\//i.test(String(value || ""))) {
    return `<a href="${text}" target="_blank" rel="noopener noreferrer">${text}</a>`;
  }
  return text || '<span class="empty">Not recorded</span>';
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

function notify(message) {
  toastNode.textContent = message;
  toastNode.classList.add("show");
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => toastNode.classList.remove("show"), 2600);
}

function companySummary(company) {
  const meta = [company.sector, company.hq_city, company.hq_country].filter(Boolean).join(" · ") || "No sector or location recorded";
  const status = company.research_status
    ? `<span class="mini-flag">${esc(company.research_status.replaceAll("_", " "))}</span>`
    : "";
  const missing = company.has_missing_research_info
    ? `<span class="mini-flag missing">${company.missing_fields.length} gaps</span>`
    : '<span class="mini-flag">Core fields present</span>';
  return `<li class="company-item"><button class="company-button ${Number(company.id) === selectedId ? "active" : ""}" data-company-id="${Number(company.id)}">
    <span class="company-name">${esc(company.name)}</span><span class="company-meta">${esc(meta)}</span><span class="mini-flags">${missing}${status}</span>
  </button></li>`;
}

async function loadCompanies({ append = false } = {}) {
  const query = searchNode.value.trim();
  if (!append) {
    offset = 0;
    lastQuery = query;
    listNode.innerHTML = "";
    countNode.textContent = "Searching…";
  }
  const data = await api(`/api/companies?q=${encodeURIComponent(query)}&limit=80&offset=${offset}`);
  listNode.insertAdjacentHTML("beforeend", data.companies.map(companySummary).join(""));
  offset += data.companies.length;
  countNode.textContent = append ? `${offset} loaded` : `${data.companies.length}${data.companies.length === 80 ? "+" : ""} results`;
  moreButton.hidden = data.companies.length < 80;
}

function statusPill(status) {
  return `<span class="status-pill ${esc(status || "queued")}">${esc((status || "queued").replaceAll("_", " "))}</span>`;
}

function researchEditor(company) {
  const record = selectedRecord;
  const editable = Boolean(record);
  const sources = editable ? (record.sources || []).map((source) => source.url).join("\n") : "";
  return `<section class="research-card">
    <div class="detail-head"><div><h3>Research record</h3><p class="research-note">${editable ? `Record #${record.id} · updated ${esc(record.updated_at)}` : "Select this company to create a research record."} ${editable ? statusPill(record.status) : ""}</p></div>
      <button id="start-research" class="secondary">${editable ? "Resume research" : "Select for research"}</button></div>
    <p class="research-note">Manual workspace only: no live web research or AI fact-finding runs here. Add findings and source links yourself; nothing is inferred from missing fields.</p>
    ${editable ? `<form id="research-form">
      <div class="form-field"><label for="findings">Findings</label><textarea id="findings" name="findings" placeholder="Record sourced findings. Keep uncertain claims clearly marked.">${esc(record.findings || "")}</textarea></div>
      <div class="form-row">
        <div class="form-field"><label for="source-urls">Source URLs (one per line)</label><textarea id="source-urls" name="source_urls" placeholder="https://…">${esc(sources)}</textarea></div>
        <div class="form-field"><label for="status">Research status</label><select id="status" name="status">
          ${["in_progress", "needs_review", "researched", "queued"].map((status) => `<option value="${status}" ${record.status === status ? "selected" : ""}>${status.replaceAll("_", " ")}</option>`).join("")}
        </select><p class="research-note">Status is a workflow state, not an accuracy guarantee.</p></div>
      </div>
      <label class="check-row"><input id="verified" type="checkbox" ${record.verified_at ? "checked" : ""}><span>Mark this record verified against its cited sources. This stores the current timestamp; the app does not validate source contents.</span></label>
      <div class="buttons"><button class="primary" type="submit">Save research</button><span class="research-note">${record.verified_at ? `Last verified ${esc(record.verified_at)}` : "Not verified"}</span></div>
      <p id="form-error" class="inline-error" hidden></p>
      ${record.sources?.length ? `<h4 class="section-title">Saved source links</h4><ul class="source-list">${record.sources.map((source) => `<li>${safeLink(source.url)}${source.verified_at ? ` <small>· verified ${esc(source.verified_at)}</small>` : ""}</li>`).join("")}</ul>` : ""}
    </form>` : ""}
  </section>`;
}

function renderCompany(company) {
  selectedId = Number(company.id);
  selectedRecord = company.research_records?.[0] || null;
  const fields = [
    ["Sector", company.sector], ["Other categories", company.categories],
    ["Headquarters city", company.hq_city], ["Region / state", company.hq_region],
    ["Country", company.hq_country], ["Website", company.website, true],
    ["Careers URL", company.careers_url, true], ["Company LinkedIn", company.company_linkedin_url, true],
    ["Company size", company.employee_count], ["Size band", company.size_band],
    ["Company stage", company.stage], ["Other locations", company.operating_locations],
    ["Verification status", company.verification_status], ["Confidence", company.confidence],
    ["Source URL", company.source_url, true],
  ];
  const missing = company.missing_fields?.length
    ? `<div class="missing-box"><strong>${company.missing_fields.length} research fields missing</strong><p>${company.missing_fields.map(esc).join(" · ")}</p></div>`
    : '<div class="complete-box">All tracked research fields are populated.</div>';
  const provenance = company.sources?.length ? `<ul class="source-list">${company.sources.slice(0, 6).map((source) => `<li>${esc(source.worksheet)} · row ${Number(source.worksheet_row)}${source.source_url ? ` · ${safeLink(source.source_url)}` : ""}</li>`).join("")}</ul>` : '<p class="research-note">No workbook source rows recorded.</p>';
  detailNode.innerHTML = `<div class="detail-head"><div><h2>${esc(company.name)}</h2><p class="detail-subtitle">${esc([company.sector, company.hq_city, company.hq_country].filter(Boolean).join(" · ") || "Company profile from imported master")}</p></div><div>${statusPill(company.research_status)}</div></div>
    <h3 class="section-title">Available company information</h3>${missing}
    <dl class="field-grid">${fields.map(([label, value, link]) => `<div class="field"><dt>${esc(label)}</dt><dd class="${value ? "" : "empty"}">${value ? (link ? safeLink(value) : esc(value)) : "Not recorded"}</dd></div>`).join("")}</dl>
    <h3 class="section-title">Workbook provenance</h3>${provenance}
    ${researchEditor(company)}`;
  listNode.querySelectorAll(".company-button").forEach((button) => {
    button.classList.toggle("active", Number(button.dataset.companyId) === selectedId);
  });
  document.querySelector("#start-research")?.addEventListener("click", beginResearch);
  document.querySelector("#research-form")?.addEventListener("submit", saveResearch);
}

async function selectCompany(id) {
  try {
    const data = await api(`/api/companies/${id}`);
    renderCompany(data.company);
  } catch (error) { notify(error.message); }
}

async function beginResearch() {
  try {
    const result = await api("/api/research/start", { method: "POST", body: JSON.stringify({ company_id: selectedId }) });
    notify("Research record opened. Add sourced findings when ready.");
    await selectCompany(selectedId);
  } catch (error) { notify(error.message); }
}

async function saveResearch(event) {
  event.preventDefault();
  const errorNode = document.querySelector("#form-error");
  errorNode.hidden = true;
  const payload = {
    company_id: selectedId,
    record_id: selectedRecord?.id,
    findings: document.querySelector("#findings").value,
    source_urls: document.querySelector("#source-urls").value.split(/\r?\n/).map((url) => url.trim()).filter(Boolean),
    status: document.querySelector("#status").value,
    verified: document.querySelector("#verified").checked,
  };
  try {
    await api("/api/research", { method: "POST", body: JSON.stringify(payload) });
    notify("Research record saved.");
    await selectCompany(selectedId);
    await loadCompanies();
  } catch (error) {
    errorNode.textContent = error.message;
    errorNode.hidden = false;
  }
}

listNode.addEventListener("click", (event) => {
  const button = event.target.closest("[data-company-id]");
  if (button) selectCompany(Number(button.dataset.companyId));
});
searchNode.addEventListener("input", () => {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => loadCompanies().catch((error) => { countNode.textContent = error.message; }), 180);
});
moreButton.addEventListener("click", () => loadCompanies({ append: true }).catch((error) => notify(error.message)));

loadCompanies().catch((error) => { countNode.textContent = `Could not load companies: ${error.message}`; });
