let incidents = [];
let selectedIndex = null;

const el = id => document.getElementById(id);

function labelsOf(record) {
  return record.alert?.labels || record.alert || record.labels || {};
}

function analysisOf(record) {
  return record.root_cause_analysis || record.rca || "No analysis available.";
}

function evidenceOf(record) {
  const evidence = record.evidence ?? record.collected_evidence;
  return typeof evidence === "string"
    ? evidence
    : JSON.stringify(evidence ?? "No evidence available.", null, 2);
}

function displayDate(value) {
  if (!value) return "Unknown";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function showDetails(index) {
  const record = incidents[index];
  if (!record) return;

  selectedIndex = index;
  const labels = labelsOf(record);

  el("detail-placeholder").hidden = true;
  el("detail-content").hidden = false;
  el("detail-title").textContent = labels.alertname || "Unnamed alert";
  el("detail-meta").textContent =
    `${displayDate(record.timestamp)} · ${labels.namespace || "No namespace"} · ${record.status || "unknown"}`;
  el("detail-analysis").textContent = analysisOf(record);
  el("detail-evidence").textContent = evidenceOf(record);

  renderRows();
}

function renderRows() {
  const query = el("search").value.toLowerCase();
  const tbody = el("incident-rows");
  tbody.replaceChildren();

  const matching = incidents
    .map((record, index) => ({ record, index }))
    .filter(({ record }) => {
      const labels = labelsOf(record);
      return [
        labels.alertname, labels.namespace,
        labels.severity, record.status
      ].some(value => String(value || "").toLowerCase().includes(query));
    });

  if (!matching.length) {
    const row = tbody.insertRow();
    const cell = row.insertCell();
    cell.colSpan = 5;
    cell.textContent = "No matching incidents.";
    return;
  }

  for (const { record, index } of matching) {
    const labels = labelsOf(record);
    const row = tbody.insertRow();
    if (index === selectedIndex) row.className = "selected";

    const values = [
      displayDate(record.timestamp),
      labels.alertname || "Unknown",
      labels.severity || "unknown",
      labels.namespace || "—",
      record.status || "unknown"
    ];

    values.forEach((value, position) => {
      const cell = row.insertCell();
      cell.textContent = value;
      if (position === 2 || position === 4) {
        cell.className = String(value).toLowerCase();
      }
    });

    row.addEventListener("click", () => showDetails(index));
  }
}

async function loadIncidents() {
  try {
    const response = await fetch("/incidents?n=100", { cache: "no-store" });
    if (!response.ok) throw new Error(`API returned ${response.status}`);

    const data = await response.json();
    if (!Array.isArray(data)) throw new Error("Unexpected API response");

    incidents = data.slice().reverse();

    el("total").textContent = incidents.length;
    el("completed").textContent =
      incidents.filter(x => x.status === "completed").length;
    el("failed").textContent =
      incidents.filter(x => x.status === "failed").length;
    el("critical").textContent =
      incidents.filter(x => labelsOf(x).severity === "critical").length;

    el("error").hidden = true;
    renderRows();

    if (selectedIndex !== null && incidents[selectedIndex]) {
      showDetails(selectedIndex);
    }
  } catch (error) {
    el("error").textContent = `Unable to load incidents: ${error.message}`;
    el("error").hidden = false;
  }
}

el("search").addEventListener("input", renderRows);
loadIncidents();
setInterval(loadIncidents, 15000);
