// RailSync — F-07 & F-08 Operations Cockpit JavaScript

let cockpitData = null;
let currentSelectedBlock = null;

document.addEventListener("DOMContentLoaded", () => {
  const blockModal = document.getElementById("blockModalBackdrop");
  if (blockModal) {
    blockModal.hidden = true;
    blockModal.classList.remove("is-open");
    blockModal.style.display = "none";
  }
  initEventListeners();
  loadCockpitIdentity();
  loadCockpitSummary();
});

function initEventListeners() {
  // Officer Role changes
  const roleSelect = document.getElementById("controllerRoleSelect");
  const roleDisplay = document.getElementById("headerRoleDisplay");
  if (roleSelect && roleDisplay) {
    roleSelect.addEventListener("change", (e) => {
      roleDisplay.textContent = e.target.value;
    });
  }

  // Filters and refresh
  const sectionFilter = document.getElementById("sectionFilter");
  if (sectionFilter) {
    sectionFilter.addEventListener("change", () => renderGanttTimeline());
  }

  const statusFilter = document.getElementById("statusFilter");
  if (statusFilter) {
    statusFilter.addEventListener("change", () => renderGanttTimeline());
  }

  const refreshBtn = document.getElementById("refreshCockpitBtn");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => loadCockpitSummary());
  }

  // Modal close buttons
  const closeModalBtn = document.getElementById("closeModalBtn");
  const closeModalFooterBtn = document.getElementById("closeModalFooterBtn");
  const modalBackdrop = document.getElementById("blockModalBackdrop");
  if (closeModalBtn) closeModalBtn.addEventListener("click", closeModal);
  if (closeModalFooterBtn) closeModalFooterBtn.addEventListener("click", closeModal);
  if (modalBackdrop) {
    modalBackdrop.addEventListener("click", (e) => {
      if (e.target === modalBackdrop) closeModal();
    });
  }

  // Action Radio Buttons toggle override fields
  const actionRadios = document.querySelectorAll('input[name="blockActionChoice"]');
  const overrideBox = document.getElementById("overrideInputsBox");
  actionRadios.forEach((radio) => {
    radio.addEventListener("change", (e) => {
      if (overrideBox) {
        overrideBox.hidden = !["OVERRIDE", "EXTEND"].includes(e.target.value);
      }
    });
  });

  // Justification character counter
  const justInput = document.getElementById("justificationNotesInput");
  const justCount = document.getElementById("justificationCharCount");
  if (justInput && justCount) {
    justInput.addEventListener("input", (e) => {
      const len = e.target.value.trim().length;
      justCount.textContent = `(${len} chars, min 5 required)`;
      justCount.style.color = len >= 5 ? "#34d399" : "#f87171";
    });
  }

  // Submit Action Button
  const submitActionBtn = document.getElementById("submitBlockActionBtn");
  if (submitActionBtn) {
    submitActionBtn.addEventListener("click", submitBlockAction);
  }

  // Export Buttons
  const exportPdfBtn = document.getElementById("exportPdfBtn");
  if (exportPdfBtn) {
    exportPdfBtn.addEventListener("click", () => {
      if (currentSelectedBlock) {
        window.open(`/api/v1/blocks/${encodeURIComponent(currentSelectedBlock.block_id)}/export-memo?format=pdf`, "_blank");
      }
    });
  }

  const exportHtmlBtn = document.getElementById("exportHtmlBtn");
  if (exportHtmlBtn) {
    exportHtmlBtn.addEventListener("click", () => {
      if (currentSelectedBlock) {
        window.open(`/api/v1/blocks/${encodeURIComponent(currentSelectedBlock.block_id)}/export-memo?format=html`, "_blank");
      }
    });
  }

  // Global Audit Drawer
  const openAuditBtn = document.getElementById("openAuditLedgerBtn");
  const closeAuditBtn = document.getElementById("closeAuditDrawerBtn");
  const auditDrawer = document.getElementById("auditDrawerBackdrop");
  if (openAuditBtn) {
    openAuditBtn.addEventListener("click", openAuditDrawer);
  }
  if (closeAuditBtn) {
    closeAuditBtn.addEventListener("click", () => {
      if (auditDrawer) auditDrawer.hidden = true;
    });
  }
  if (auditDrawer) {
    auditDrawer.addEventListener("click", (e) => {
      if (e.target === auditDrawer) auditDrawer.hidden = true;
    });
  }

  // What-If Drawer
  const openWhatIfBtn = document.getElementById("openWhatIfDrawerBtn");
  const closeWhatIfBtn = document.getElementById("closeWhatIfDrawerBtn");
  const whatIfDrawer = document.getElementById("whatIfDrawerBackdrop");
  if (openWhatIfBtn) {
    openWhatIfBtn.addEventListener("click", openWhatIfDrawer);
  }
  if (closeWhatIfBtn) {
    closeWhatIfBtn.addEventListener("click", () => {
      if (whatIfDrawer) whatIfDrawer.hidden = true;
    });
  }
  if (whatIfDrawer) {
    whatIfDrawer.addEventListener("click", (e) => {
      if (e.target === whatIfDrawer) whatIfDrawer.hidden = true;
    });
  }

  // What-If Form Submission
  const whatIfForm = document.getElementById("cockpitWhatIfForm");
  if (whatIfForm) {
    whatIfForm.addEventListener("submit", handleWhatIfSubmit);
  }
}

async function loadCockpitSummary() {
  try {
    const res = await fetch("/api/v1/cockpit/summary");
    if (!res.ok) {
      throw new Error(`Failed to load summary: ${res.statusText}`);
    }
    cockpitData = await res.json();
    renderKPIs(cockpitData.kpis);
    populateSectionFilter(cockpitData.sections);
    renderGanttTimeline();
    renderDeferredWork(cockpitData.unscheduled_tasks);
  } catch (err) {
    console.error("Cockpit summary error:", err);
  }
}

async function loadCockpitIdentity() {
  const actorInput = document.getElementById("controllerNameInput");
  const roleSelect = document.getElementById("controllerRoleSelect");
  const roleDisplay = document.getElementById("headerRoleDisplay");
  if (!actorInput || !roleSelect || !roleDisplay) return;

  try {
    const response = await fetch("/api/v1/cockpit/identity");
    if (!response.ok) throw new Error("Configured operator identity is unavailable.");
    const identity = await response.json();
    actorInput.value = identity.actor;
    actorInput.readOnly = true;
    roleSelect.value = identity.role;
    roleSelect.disabled = true;
    roleDisplay.textContent = identity.role;
  } catch (error) {
    actorInput.value = "";
    actorInput.placeholder = "Server identity unavailable";
    roleSelect.disabled = true;
    console.error("Cockpit identity error:", error);
  }
}

function renderKPIs(kpis) {
  if (!kpis) return;
  document.getElementById("kpiHoursSaved").innerHTML = `${kpis.total_block_hours_saved ?? "—"}<small> hrs</small>`;
  document.getElementById("kpiAvailability").innerHTML = `${kpis.planned_availability_pct ?? "—"}<small>%</small>`;
  document.getElementById("kpiHoursUsed").textContent = `${kpis.total_block_hours_used ?? "—"} hrs corridor maintenance`;
  document.getElementById("kpiBlockCount").textContent = kpis.total_scheduled_blocks || 0;
  document.getElementById("kpiTaskCount").textContent = `${kpis.total_scheduled_tasks || 0} maintenance tasks`;
  document.getElementById("kpiDeferredCount").textContent = kpis.total_unscheduled_tasks || 0;

  const counts = kpis.sanction_counts || {};
  document.getElementById("chipCountProposed").textContent = counts.PROPOSED || 0;
  document.getElementById("chipCountSanctioned").textContent = counts.SANCTIONED || 0;
  document.getElementById("chipCountOverridden").textContent = counts.OVERRIDDEN || 0;
}

function populateSectionFilter(sections) {
  const filter = document.getElementById("sectionFilter");
  if (!filter || !sections) return;
  const currentVal = filter.value;
  filter.innerHTML = '<option value="ALL">All Registered Sections</option>';
  sections.forEach((s) => {
    const opt = document.createElement("option");
    opt.value = s;
    opt.textContent = s;
    filter.appendChild(opt);
  });
  if (currentVal) filter.value = currentVal;
}

function renderGanttTimeline() {
  const emptyState = document.getElementById("timelineEmptyState");
  const ganttBoard = document.getElementById("ganttBoard");
  if (!emptyState || !ganttBoard) return;

  const blocks = cockpitData?.blocks || [];
  if (blocks.length === 0) {
    emptyState.hidden = false;
    ganttBoard.hidden = true;
    return;
  }

  emptyState.hidden = true;
  ganttBoard.hidden = false;

  const sectionFilter = document.getElementById("sectionFilter").value;
  const statusFilter = document.getElementById("statusFilter").value;

  const filtered = blocks.filter((b) => {
    if (sectionFilter !== "ALL" && b.section_id !== sectionFilter) return false;
    if (statusFilter !== "ALL" && b.state !== statusFilter) return false;
    return true;
  });

  if (filtered.length === 0) {
    ganttBoard.innerHTML = `
      <div class="timeline-empty-state">
        <div class="empty-icon">🔍</div>
        <h3>No matching blocks</h3>
        <p>No blocks matched the selected section or sanction state filter.</p>
      </div>
    `;
    return;
  }

  // Group by section
  const grouped = {};
  filtered.forEach((b) => {
    const sec = b.section_id || "UNASSIGNED";
    if (!grouped[sec]) grouped[sec] = [];
    grouped[sec].push(b);
  });

  ganttBoard.innerHTML = "";

  const timestamps = filtered.flatMap((block) => [Date.parse(block.effective_start || block.scheduled_start), Date.parse(block.effective_end || block.scheduled_end)]).filter(Number.isFinite);
  const domainStart = Math.min(...timestamps);
  const domainEnd = Math.max(...timestamps);
  const domainLength = Math.max(domainEnd - domainStart, 60 * 60 * 1000);
  const axis = document.createElement("div");
  axis.className = "gantt-time-axis";
  for (let index = 0; index <= 4; index += 1) {
    const marker = document.createElement("span");
    const timestamp = domainStart + (domainLength * index / 4);
    marker.style.left = `${index * 25}%`;
    marker.textContent = new Date(timestamp).toISOString().slice(0, 16).replace("T", " ") + " UTC";
    axis.appendChild(marker);
  }
  ganttBoard.appendChild(axis);

  Object.entries(grouped).forEach(([secId, secBlocks]) => {
    const rowEl = document.createElement("div");
    rowEl.className = "gantt-section-row";

    const headerEl = document.createElement("div");
    headerEl.className = "section-row-header";
    headerEl.innerHTML = `
      <span class="section-row-title">${escapeHtml(secId)}</span>
      <span class="badge badge-proposed">${secBlocks.length} Window${secBlocks.length > 1 ? "s" : ""}</span>
    `;

    const laneEl = document.createElement("div");
    laneEl.className = "section-row-blocks-lane";

    secBlocks.forEach((b) => {
      const pill = createBlockPill(b, domainStart, domainLength);
      laneEl.appendChild(pill);
    });

    rowEl.appendChild(headerEl);
    rowEl.appendChild(laneEl);
    ganttBoard.appendChild(rowEl);
  });
}

function createBlockPill(block, domainStart, domainLength) {
  const pill = document.createElement("div");
  const stateCls = `state-${(block.state || "proposed").toLowerCase()}`;
  pill.className = `block-pill ${stateCls}`;
  const blockStart = Date.parse(block.effective_start || block.scheduled_start);
  const blockEnd = Date.parse(block.effective_end || block.scheduled_end);
  const left = Number.isFinite(blockStart) ? Math.max(0, ((blockStart - domainStart) / domainLength) * 100) : 0;
  const width = Number.isFinite(blockStart) && Number.isFinite(blockEnd) ? Math.max(4, ((blockEnd - blockStart) / domainLength) * 100) : 8;
  pill.style.left = `${left}%`;
  pill.style.width = `${width}%`;
  pill.setAttribute("role", "button");
  pill.setAttribute("tabindex", "0");

  const depts = block.consolidated_departments || [];
  let segmentsHtml = "";
  if (depts.includes("ENGINEERING")) segmentsHtml += '<div class="seg-eng" title="Engineering"></div>';
  if (depts.includes("SIGNAL_TELECOM")) segmentsHtml += '<div class="seg-st" title="Signal & Telecom"></div>';
  if (depts.includes("TRACTION")) segmentsHtml += '<div class="seg-trd" title="Traction"></div>';
  if (!segmentsHtml) segmentsHtml = '<div class="seg-eng" style="background:#475569;"></div>';

  const badgeCls = `badge-${(block.state || "proposed").toLowerCase()}`;
  const startStr = block.effective_start ? formatIsoTime(block.effective_start) : "Not recorded";
  const endStr = block.effective_end ? formatIsoTime(block.effective_end) : "Not recorded";

  pill.innerHTML = `
    <div class="block-pill-top">
      <span class="pill-id">${escapeHtml(block.block_id)}</span>
      <span class="pill-state-badge ${badgeCls}">${escapeHtml(block.state || "PROPOSED")}</span>
    </div>
    <div class="pill-dept-segments">${segmentsHtml}</div>
    <div class="pill-time-slot">
      ${startStr} – ${endStr} UTC (${block.duration_minutes || 0}m)
    </div>
    <div class="pill-meta-foot">
      <span>${(block.assigned_tasks || []).length} tasks</span>
      <div class="precautions-icons">
        ${block.requires_traffic_block ? '<span title="Traffic Block Required">🛑</span>' : ""}
        ${block.requires_traction_disconnection ? '<span title="Traction Disconnection Required">⚡</span>' : ""}
      </div>
    </div>
  `;

  pill.addEventListener("click", () => openBlockModal(block));
  pill.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openBlockModal(block);
    }
  });

  return pill;
}

function openBlockModal(block) {
  currentSelectedBlock = block;
  const modal = document.getElementById("blockModalBackdrop");
  if (!modal) return;

  document.getElementById("modalBlockTitle").textContent = `Block ${block.block_id}`;
  document.getElementById("mSection").textContent = block.section_id;

  const stateBadge = document.getElementById("mStateBadge");
  stateBadge.className = `status-badge badge-${(block.state || "proposed").toLowerCase()}`;
  stateBadge.textContent = block.state || "PROPOSED";

  document.getElementById("mScheduledWindow").textContent = `${formatIsoDateTime(block.scheduled_start)} to ${formatIsoDateTime(block.scheduled_end)}`;
  document.getElementById("mEffectiveWindow").textContent = `${formatIsoDateTime(block.effective_start)} to ${formatIsoDateTime(block.effective_end)}`;
  document.getElementById("mDuration").textContent = `${block.duration_minutes} mins (${(block.duration_minutes / 60).toFixed(2)} hrs)`;
  document.getElementById("mTrafficBlock").textContent = block.requires_traffic_block ? "YES (Absolute Traffic Protection)" : "NO";
  document.getElementById("mTractionDisconnection").textContent = block.requires_traction_disconnection ? "YES (25kV Power Disconnection)" : "NO";
  document.getElementById("mTrainImpact").textContent = `${block.passenger_trains_affected || 0} passenger · ${block.goods_trains_affected || 0} goods affected`;

  // Render Tasks
  const tbody = document.getElementById("mTasksTableBody");
  tbody.innerHTML = "";
  (block.assigned_tasks || []).forEach((t) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><strong>${escapeHtml(t.task_id || "")}</strong></td>
      <td><span class="dept-pill dept-${escapeHtml(String(t.department || "").toLowerCase())}">${escapeHtml(t.department || "")}</span></td>
      <td>${escapeHtml(t.maintenance_type || "Not recorded")}</td>
      <td>${escapeHtml(t.km_span || "Not recorded")}</td>
      <td><strong>${t.priority_score != null ? t.priority_score : "N/A"}</strong></td>
      <td>${t.scheduled_start ? formatIsoTime(t.scheduled_start) : "Not recorded"}–${t.scheduled_end ? formatIsoTime(t.scheduled_end) : "Not recorded"}</td>
      <td><strong>${escapeHtml(t.feasibility?.status || "NOT_ASSESSED")}</strong><br/><small>${escapeHtml((t.feasibility?.warning_reasons || []).join(" ") || `Data quality: ${t.data_quality_status || "UNKNOWN"}`)}</small></td>
    `;
    tbody.appendChild(tr);
  });

  // Action Panel reset
  document.querySelector('input[name="blockActionChoice"][value="APPROVE"]').checked = true;
  document.getElementById("overrideInputsBox").hidden = true;
  document.getElementById("actionStatusMsg").textContent = "";

  // Pre-fill override date inputs
  const startIso = block.effective_start ? block.effective_start.substring(0, 16) : "";
  const endIso = block.effective_end ? block.effective_end.substring(0, 16) : "";
  document.getElementById("overrideStartInput").value = startIso;
  document.getElementById("overrideEndInput").value = endIso;

  const justInput = document.getElementById("justificationNotesInput");
  justInput.value = "";
  document.getElementById("justificationCharCount").textContent = "(0 chars, min 5 required)";
  document.getElementById("justificationCharCount").style.color = "#f87171";

  // Load audit history for this block
  loadBlockAuditHistory(block.block_id);

  modal.hidden = false;
  modal.classList.add("is-open");
  modal.style.display = "grid";
}

function closeModal() {
  const modal = document.getElementById("blockModalBackdrop");
  if (modal) {
    modal.hidden = true;
    modal.classList.remove("is-open");
    modal.style.display = "none";
  }
  currentSelectedBlock = null;
}

async function submitBlockAction() {
  if (!currentSelectedBlock) return;

  const actor = document.getElementById("controllerNameInput").value.trim();
  const role = document.getElementById("controllerRoleSelect").value;
  const actionChoice = document.querySelector('input[name="blockActionChoice"]:checked')?.value || "APPROVE";
  const reasonCode = document.getElementById("reasonCodeSelect").value;
  const notes = document.getElementById("justificationNotesInput").value.trim();
  const statusMsg = document.getElementById("actionStatusMsg");

  if (!actor) {
    statusMsg.className = "status-msg error";
    statusMsg.textContent = "Please enter officer/controller name.";
    return;
  }

  if (notes.length < 5) {
    statusMsg.className = "status-msg error";
    statusMsg.textContent = "Justification must be at least 5 characters.";
    return;
  }

  const payload = {
    plan_id: currentSelectedBlock.plan_id,
    actor: actor,
    role: role,
    action: actionChoice,
    reason_code: reasonCode,
    justification_notes: notes,
  };

  if (["OVERRIDE", "EXTEND"].includes(actionChoice)) {
    const modStart = document.getElementById("overrideStartInput").value;
    const modEnd = document.getElementById("overrideEndInput").value;
    if (modStart) payload.modified_start = new Date(modStart).toISOString();
    if (modEnd) payload.modified_end = new Date(modEnd).toISOString();
  }

  statusMsg.className = "status-msg";
  statusMsg.textContent = "Submitting decision to regulatory ledger...";

  try {
    const res = await fetch(`/api/v1/blocks/${encodeURIComponent(currentSelectedBlock.block_id)}/action`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || res.statusText);
    }

    const data = await res.json();
    statusMsg.className = "status-msg success";
    statusMsg.textContent = `Decision logged! State is now ${data.state}. Audit reference: ${data.audit_id.substring(0, 8)}...`;

    // Refresh block data
    currentSelectedBlock.state = data.state;
    if (data.effective_start) currentSelectedBlock.effective_start = data.effective_start;
    if (data.effective_end) currentSelectedBlock.effective_end = data.effective_end;

    // Update modal badge
    const stateBadge = document.getElementById("mStateBadge");
    stateBadge.className = `status-badge badge-${data.state.toLowerCase()}`;
    stateBadge.textContent = data.state;
    document.getElementById("mEffectiveWindow").textContent = `${formatIsoDateTime(data.effective_start)} to ${formatIsoDateTime(data.effective_end)}`;

    // Refresh audit history in modal
    loadBlockAuditHistory(currentSelectedBlock.block_id);

    // Refresh cockpit summary in background
    loadCockpitSummary();
  } catch (err) {
    statusMsg.className = "status-msg error";
    statusMsg.textContent = `Error: ${err.message}`;
  }
}

async function loadBlockAuditHistory(blockId) {
  const container = document.getElementById("blockAuditTimeline");
  if (!container) return;
  container.innerHTML = '<div class="audit-loading">Fetching audit history...</div>';

  try {
    const res = await fetch(`/api/v1/blocks/${encodeURIComponent(blockId)}/audit-logs`);
    if (!res.ok) throw new Error("Failed to load audit logs.");
    const logs = await res.json();
    if (logs.length === 0) {
      container.innerHTML = '<div class="audit-loading">No audit decisions recorded yet.</div>';
      return;
    }
    container.innerHTML = "";
    logs.forEach((log) => {
      const item = document.createElement("div");
      item.className = "audit-timeline-item";
      item.innerHTML = `
        <div class="audit-item-top">
          <span>${escapeHtml(log.action)} by ${escapeHtml(log.actor)} (${escapeHtml(log.role)})</span>
          <span>${formatIsoDateTime(log.created_at)}</span>
        </div>
        <div class="audit-item-notes">
          <strong>[${escapeHtml(log.reason_code)}]</strong> ${escapeHtml(log.justification_notes)}
        </div>
      `;
      container.appendChild(item);
    });
  } catch (err) {
    container.innerHTML = `<div class="audit-loading" style="color:#f87171;">Audit log unavailable: ${escapeHtml(err.message)}</div>`;
  }
}

async function openAuditDrawer() {
  const drawer = document.getElementById("auditDrawerBackdrop");
  const tbody = document.getElementById("globalAuditTableBody");
  if (!drawer || !tbody) return;

  drawer.hidden = false;
  tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;">Loading audit entries...</td></tr>';

  try {
    const res = await fetch("/api/v1/audit/logs?limit=50");
    if (!res.ok) throw new Error("Failed to load audit ledger.");
    const logs = await res.json();
    if (logs.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#94a3b8;">No regulatory entries logged yet.</td></tr>';
      return;
    }
    tbody.innerHTML = "";
    logs.forEach((l) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="mono-txt">${formatIsoDateTime(l.created_at)}</td>
        <td><strong>${escapeHtml(l.block_id)}</strong></td>
        <td>${escapeHtml(l.actor)}<br/><small style="color:#94a3b8;">${escapeHtml(l.role)}</small></td>
        <td><span class="status-chip chip-${escapeHtml(String(l.action).toLowerCase())}">${escapeHtml(l.action)}</span></td>
        <td><code>${escapeHtml(l.reason_code)}</code></td>
        <td>${escapeHtml(l.justification_notes)}</td>
      `;
      tbody.appendChild(tr);
    });
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="6" style="color:#f87171;text-align:center;">Error: ${escapeHtml(err.message)}</td></tr>`;
  }
}

function openWhatIfDrawer() {
  const drawer = document.getElementById("whatIfDrawerBackdrop");
  if (!drawer) return;
  drawer.hidden = false;

}

async function handleWhatIfSubmit(e) {
  e.preventDefault();
  const btn = document.getElementById("runWiSimBtn");
  const resultsArea = document.getElementById("wiResultsArea");
  btn.disabled = true;
  btn.textContent = "Simulating delay propagation...";

  const payload = {
    section_from: document.getElementById("wiSectionFrom").value.trim(),
    section_to: document.getElementById("wiSectionTo").value.trim(),
    block_start_time: new Date(document.getElementById("wiStartTime").value).toISOString(),
    block_end_time: new Date(document.getElementById("wiEndTime").value).toISOString(),
  };

  try {
    const res = await fetch("/api/v1/simulate/what-if", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || res.statusText);
    }

    const report = await res.json();
    document.getElementById("wiPassDelay").innerHTML = `${report.total_passenger_delay_minutes || 0}<small> mins</small>`;
    document.getElementById("wiRegTrains").textContent = (report.regulated_freight_trains || []).length;
    document.getElementById("wiPunctuality").textContent = `${report.punctuality_impact_pct || 0}%`;

    const warnBox = document.getElementById("wiWarningsBox");
    if (report.conflict_warnings && report.conflict_warnings.length > 0) {
      warnBox.innerHTML = report.conflict_warnings.map((w) => `<div>⚠ ${escapeHtml(w)}</div>`).join("");
      warnBox.hidden = false;
    } else {
      warnBox.innerHTML = "<div>✓ No safety headway conflicts identified on this simulated run.</div>";
      warnBox.hidden = false;
    }

    resultsArea.hidden = false;
  } catch (err) {
    alert(`What-If simulation failed: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "Run Operational Delay Simulation";
  }
}

function renderDeferredWork(tasks) {
  const section = document.getElementById("deferredSection");
  const tbody = document.getElementById("deferredTableBody");
  const badge = document.getElementById("deferredCountBadge");
  if (!section || !tbody || !badge) return;

  if (!tasks || tasks.length === 0) {
    section.hidden = true;
    return;
  }

  section.hidden = false;
  badge.textContent = `${tasks.length} Task${tasks.length > 1 ? "s" : ""}`;
  tbody.innerHTML = "";

  tasks.forEach((t) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><strong>${escapeHtml(t.task_id || "")}</strong></td>
      <td class="mono-txt">${escapeHtml(t.plan_id ? t.plan_id.substring(0, 8) + "..." : "—")}</td>
      <td style="color:#f87171;">${escapeHtml(t.reason || "Constraint unfulfilled")}</td>
    `;
    tbody.appendChild(tr);
  });
}

function formatIsoTime(isoStr) {
  if (!isoStr) return "";
  try {
    const parts = isoStr.split("T");
    if (parts.length > 1) {
      return parts[1].substring(0, 5);
    }
    const d = new Date(isoStr);
    return d.toISOString().substring(11, 16);
  } catch {
    return isoStr;
  }
}

function formatIsoDateTime(isoStr) {
  if (!isoStr) return "N/A";
  try {
    return isoStr.replace("T", " ").substring(0, 19);
  } catch {
    return isoStr;
  }
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
