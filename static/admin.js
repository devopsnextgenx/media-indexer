const REFRESH_MS = 5000;

function dotClass(healthy) {
    if (healthy === true) return "ok";
    if (healthy === false) return "bad";
    return "unknown";
}

function serviceCard(name, ok, detail) {
    return `<div class="status-card">
        <div class="status-title"><span class="status-dot ${dotClass(ok)}"></span>${name}</div>
        <div class="status-detail">${detail || ""}</div>
    </div>`;
}

function ollamaCard(entry) {
    const detail = entry.healthy === false
        ? (entry.last_error || "unreachable")
        : "reachable";
    return `<div class="status-card">
        <div class="status-title"><span class="status-dot ${dotClass(entry.healthy)}"></span>${entry.endpoint}</div>
        <div class="status-detail">${detail}</div>
    </div>`;
}

async function populateMountSelects(mounts) {
    for (const id of ["llm-mount-select", "dup-mount-select"]) {
        const sel = document.getElementById(id);
        const current = sel.value;
        sel.querySelectorAll("option:not(:first-child)").forEach(o => o.remove());
        for (const m of mounts) {
            const opt = document.createElement("option");
            opt.value = m; opt.textContent = m;
            sel.appendChild(opt);
        }
        if (mounts.includes(current)) sel.value = current;
    }
}

function renderJobsTable(jobs) {
    const tbody = document.getElementById("jobs-tbody");
    tbody.innerHTML = "";
    for (const job of jobs) {
        const tr = document.createElement("tr");
        const progress = job.total_items
            ? `${job.processed_items || 0} / ${job.total_items}`
            : `${job.processed_items || 0}`;
        const canPause = job.status === "RUNNING" || job.status === "PENDING";
        const canResume = job.status === "PAUSED" || job.status === "FAILED";
        const canCancel = job.status === "RUNNING" || job.status === "PENDING" || job.status === "PAUSED";
        const eta = job.eta_seconds != null ? formatDuration(job.eta_seconds) : "—";
        tr.innerHTML = `
            <td>${job.job_id}</td>
            <td>${job.job_type}</td>
            <td>${job.mount_name || "all"}</td>
            <td><span class="job-status-pill ${job.status}">${job.status}</span></td>
            <td>${eta}</td>
            <td>${progress}</td>
            <td>${job.failed_items || 0}</td>
            <td>${job.updated_at || ""}</td>
            <td>
                ${canPause ? `<button class="btn btn-secondary btn-sm" data-action="pause" data-job="${job.job_id}">Pause</button>` : ""}
                ${canResume ? `<button class="btn btn-primary btn-sm" data-action="resume" data-job="${job.job_id}">Resume</button>` : ""}
                ${canCancel ? `<button class="btn btn-danger btn-sm" data-action="cancel" data-job="${job.job_id}">Cancel</button>` : ""}
            </td>`;
        tbody.appendChild(tr);
    }
    tbody.querySelectorAll("button[data-action]").forEach(btn => {
        btn.addEventListener("click", async () => {
            const action = btn.dataset.action;
            const jobId = btn.dataset.job;
            await fetch(`/api/admin/jobs/${encodeURIComponent(jobId)}/${action}`, { method: "POST" });
            refresh();
        });
    });
}

function renderBacklog(backlog) {
    const el = document.getElementById("llm-backlog-list");
    el.innerHTML = Object.entries(backlog || {})
        .map(([mount, count]) => `<div class="backlog-pill">${mount}: ${count} unparsed</div>`)
        .join("") || `<span class="admin-hint">No backlog data (MySQL disabled?)</span>`;
}

function formatDuration(seconds) {
    if (seconds == null || seconds < 0) return "—";
    if (seconds < 60) return `< 1m`;
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    if (m < 60) return `≈ ${m}m ${s}s`;
    const h = Math.floor(m / 60);
    const rm = m % 60;
    return `≈ ${h}h ${rm}m`;
}

// ---- Mount Actions Table & Console ----
const MOUNT_ACTION_CONSOLE = document.getElementById("mount-action-console");
const MOUNT_ACTION_LOG = document.getElementById("mount-action-log");
const MOUNT_ACTION_STATUS = document.getElementById("mount-action-status");
const MOUNT_ACTION_CLEAR = document.getElementById("mount-action-clear");
const MOUNT_ACTION_HIDE = document.getElementById("mount-action-hide");
const MOUNT_ACTIONS_TBODY = document.getElementById("mount-actions-tbody");

let mountActionStream = null;
let mountActionPollInterval = null;
let mountActionJobId = null;
let mountActionType = null;
let mountActionMount = null;

function escapeHtml(str) {
    if (!str) return "";
    return String(str).replace(/[&<>"']/g, function(m) {
        if (m === '&') return '&amp;';
        if (m === '<') return '&lt;';
        if (m === '>') return '&gt;';
        if (m === '"') return '&quot;';
        if (m === "'") return '&#39;';
        return m;
    });
}

function formatDate(iso) {
    if (!iso) return "—";
    try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

function actionStatusBadge(status) {
    const cls = (status || "unknown").toLowerCase();
    return `<span class="job-status-pill ${cls}">${status || "—"}</span>`;
}

function renderMountActionsTable(statuses) {
    const mounts = statuses.mounts || [];
    const data = statuses.statuses || {};

    let html = "";
    for (const mount of mounts) {
        const row = data[mount] || {};
        const idx = row.indexing || null;
        const llm = row.llm_parse || null;
        const dup = row.duplicate_detect || null;

        const idxRunning = idx && ["RUNNING", "PENDING"].includes(idx.status?.toUpperCase());
        const llmRunning = llm && ["RUNNING", "PENDING"].includes(llm.status?.toUpperCase());
        const dupRunning = dup && ["RUNNING", "PENDING"].includes(dup.status?.toUpperCase());

        html += `<tr>
            <td><strong>${escapeHtml(mount)}</strong></td>
            <td>
                <div class="action-cell">
                    <div class="action-info">
                        ${actionStatusBadge(idx?.status)}
                        <span class="action-date">${formatDate(idx?.updated_at)}</span>
                    </div>
                    <button class="btn-icon-action" data-mount="${escapeHtml(mount)}" data-action="indexing" title="Start indexing" ${idxRunning ? 'disabled' : ''}>
                        ${idxRunning ? '⏳' : '▶'}
                    </button>
                </div>
            </td>
            <td>
                <div class="action-cell">
                    <div class="action-info">
                        ${actionStatusBadge(llm?.status)}
                        <span class="action-date">${formatDate(llm?.updated_at)}</span>
                    </div>
                    <button class="btn-icon-action" data-mount="${escapeHtml(mount)}" data-action="llm_parse" title="Start LLM parse" ${llmRunning ? 'disabled' : ''}>
                        ${llmRunning ? '⏳' : '🧠'}
                    </button>
                </div>
            </td>
            <td>
                <div class="action-cell">
                    <div class="action-info">
                        ${actionStatusBadge(dup?.status)}
                        <span class="action-date">${formatDate(dup?.updated_at)}</span>
                    </div>
                    <button class="btn-icon-action" data-mount="${escapeHtml(mount)}" data-action="duplicate_detect" title="Start duplicate detection" ${dupRunning ? 'disabled' : ''}>
                        ${dupRunning ? '⏳' : '⊞'}
                    </button>
                </div>
            </td>
        </tr>`;
    }
    MOUNT_ACTIONS_TBODY.innerHTML = html || `<tr><td colspan="4" class="empty-state">No mounts configured.</td></tr>`;

    // Attach event listeners to all action buttons
    MOUNT_ACTIONS_TBODY.querySelectorAll(".btn-icon-action").forEach(btn => {
        btn.addEventListener("click", async (e) => {
            const mount = btn.dataset.mount;
            const action = btn.dataset.action;
            await triggerMountAction(mount, action);
        });
    });
}

function formatEta(seconds) {
    if (seconds == null || seconds < 0) return "--";
    const total = Math.round(seconds);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    if (h > 0) return `${h}h${String(m).padStart(2, "0")}m`;
    if (m > 0) return `${m}m${String(s).padStart(2, "0")}s`;
    return `${s}s`;
}

async function triggerMountAction(mount, action) {
    // Close any existing stream/poll
    cleanupMountAction();

    // Show console
    MOUNT_ACTION_CONSOLE.classList.remove("hidden");
    MOUNT_ACTION_LOG.innerHTML = "";
    MOUNT_ACTION_STATUS.textContent = `Starting ${action} for ${mount}...`;
    mountActionMount = mount;
    mountActionType = action;

    try {
        if (action === "indexing") {
            const res = await fetch(`/api/scan/start?mount_name=${encodeURIComponent(mount)}&rescan_disk=false&incremental_scan=true`, { method: "POST" });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || "Failed to start indexing");
            MOUNT_ACTION_STATUS.textContent = `Indexing started for ${mount}. Waiting for progress...`;
            mountActionStream = new EventSource(`/api/scan/stream?mount_name=${encodeURIComponent(mount)}`);
            mountActionStream.onmessage = (event) => {
                let data;
                try { data = JSON.parse(event.data); } catch { return; }
                appendMountLog(data.logs || []);
                const status = data.status || "";
                if (status === "COMPLETED") {
                    MOUNT_ACTION_STATUS.textContent = `Indexing completed for ${mount}.`;
                    cleanupMountAction();
                    refreshMountStatuses();
                } else if (status === "FAILED") {
                    MOUNT_ACTION_STATUS.textContent = `Indexing failed for ${mount}: ${data.error || "unknown error"}`;
                    cleanupMountAction();
                    refreshMountStatuses();
                } else {
                    const pct = data.progress_percentage ?? 0;
                    const eta = data.eta_seconds ? formatEta(data.eta_seconds) : "--";
                    MOUNT_ACTION_STATUS.textContent = `Indexing ${mount}: ${data.processed_files || 0}/${data.total_files || 0} (${pct}%) ETA ${eta}`;
                }
            };
            mountActionStream.onerror = () => {
                MOUNT_ACTION_STATUS.textContent = `Indexing stream disconnected for ${mount}.`;
                cleanupMountAction();
                refreshMountStatuses();
            };
        } else {
            let url;
            if (action === "llm_parse") {
                url = `/api/admin/llm-parse/run?mount=${encodeURIComponent(mount)}`;
            } else {
                url = `/api/admin/duplicates/detect-job?mount=${encodeURIComponent(mount)}`;
            }
            const res = await fetch(url, { method: "POST" });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || `Failed to start ${action}`);
            const jobId = data.job_id;
            mountActionJobId = jobId;
            MOUNT_ACTION_STATUS.textContent = `Job ${jobId} started. Polling status...`;
            mountActionPollInterval = setInterval(async () => {
                try {
                    const jobRes = await fetch(`/api/admin/jobs/${jobId}`);
                    if (!jobRes.ok) throw new Error("Job not found");
                    const job = await jobRes.json();
                    const status = job.status || "";
                    const processed = job.processed_items || 0;
                    const total = job.total_items || 0;
                    const error = job.last_error || "";
                    if (status === "COMPLETED") {
                        MOUNT_ACTION_STATUS.textContent = `${action} completed for ${mount} (${processed} processed).`;
                        cleanupMountAction();
                        refreshMountStatuses();
                    } else if (status === "FAILED" || status === "CANCELLED") {
                        MOUNT_ACTION_STATUS.textContent = `${action} ${status.toLowerCase()} for ${mount}: ${error || "no error"}`;
                        cleanupMountAction();
                        refreshMountStatuses();
                    } else {
                        const pct = total ? Math.round((processed/total)*100) : 0;
                        MOUNT_ACTION_STATUS.textContent = `${action} ${mount}: ${processed}/${total} (${pct}%) - ${status}`;
                    }
                    if (job.updated_at) {
                        appendMountLog([{ mount, level: "info", ts: job.updated_at, message: `${action} status: ${status}` }]);
                    }
                } catch (err) {
                    // ignore polling errors
                }
            }, 2000);
        }
    } catch (err) {
        MOUNT_ACTION_STATUS.textContent = `Error: ${err.message}`;
        appendMountLog([{ mount, level: "error", message: err.message }]);
        cleanupMountAction();
    }
}

function appendMountLog(entries) {
    if (!entries) return;
    for (const entry of entries) {
        const node = document.createElement("div");
        node.className = `scan-log-line level-${entry.level || "info"}`;
        const ts = entry.ts ? new Date(entry.ts).toLocaleTimeString() : "--:--:--";
        node.innerHTML = `<span class="log-ts">${escapeHtml(ts)}</span><span class="log-mount">[${escapeHtml(entry.mount || "-")}]</span> ${escapeHtml(entry.message || "")}`;
        MOUNT_ACTION_LOG.appendChild(node);
    }
    MOUNT_ACTION_LOG.scrollTop = MOUNT_ACTION_LOG.scrollHeight;
}

function cleanupMountAction() {
    if (mountActionStream) {
        mountActionStream.close();
        mountActionStream = null;
    }
    if (mountActionPollInterval) {
        clearInterval(mountActionPollInterval);
        mountActionPollInterval = null;
    }
    mountActionJobId = null;
    mountActionType = null;
    mountActionMount = null;
    refreshMountStatuses();
}

MOUNT_ACTION_CLEAR.addEventListener("click", () => {
    MOUNT_ACTION_LOG.innerHTML = "";
});

MOUNT_ACTION_HIDE.addEventListener("click", () => {
    MOUNT_ACTION_CONSOLE.classList.add("hidden");
    cleanupMountAction();
});

// ---- Fetch mount statuses periodically ----
async function refreshMountStatuses() {
    try {
        const res = await fetch("/api/admin/mounts/status");
        const data = await res.json();
        renderMountActionsTable(data);
    } catch (e) {
        console.error("Failed to fetch mount statuses", e);
    }
}

document.getElementById("btn-refresh-mounts").addEventListener("click", refreshMountStatuses);

// ---- Main refresh function ----
async function refresh() {
    try {
        const res = await fetch("/api/admin/status");
        const data = await res.json();

        document.getElementById("services-grid").innerHTML = [
            serviceCard("MySQL", data.mysql.connected, data.mysql.database || data.mysql.error || (data.mysql.enabled ? "" : "disabled")),
            serviceCard("Redis", data.redis.connected, data.redis.error || (data.redis.enabled ? "" : "disabled")),
            serviceCard("Qdrant", data.qdrant.connected, (data.qdrant.collections || []).join(", ") || data.qdrant.error),
            serviceCard("Resource gate", data.resource_gate.free, data.resource_gate.reason),
        ].join("");

        document.getElementById("ollama-llm-grid").innerHTML =
            (data.ollama.llm || []).map(ollamaCard).join("") || `<span class="admin-hint">No endpoints configured</span>`;
        document.getElementById("ollama-embed-grid").innerHTML =
            (data.ollama.embedding || []).map(ollamaCard).join("") || `<span class="admin-hint">Single endpoint (no pool)</span>`;

        await populateMountSelects(data.mounts || []);
        renderBacklog(data.llm_parse_backlog);
        renderJobsTable(data.jobs || []);
    } catch (e) {
        console.error("Failed to refresh admin status", e);
    }

    await refreshMountStatuses();
}

// ---- Event listeners ----
document.getElementById("btn-refresh").addEventListener("click", refresh);

document.getElementById("btn-refresh-ollama").addEventListener("click", async () => {
    await fetch("/api/admin/ollama/refresh", { method: "POST" });
    refresh();
});

document.getElementById("btn-start-llm-job").addEventListener("click", async () => {
    const mount = document.getElementById("llm-mount-select").value;
    const qs = mount ? `?mount=${encodeURIComponent(mount)}` : "";
    await fetch(`/api/admin/llm-parse/run${qs}`, { method: "POST" });
    refresh();
});

document.getElementById("btn-start-dup-job").addEventListener("click", async () => {
    const mount = document.getElementById("dup-mount-select").value;
    const qs = mount ? `?mount=${encodeURIComponent(mount)}` : "";
    await fetch(`/api/admin/duplicates/detect-job${qs}`, { method: "POST" });
    refresh();
});

// ---- Initial load and periodic refresh ----
refresh();
setInterval(refresh, REFRESH_MS);