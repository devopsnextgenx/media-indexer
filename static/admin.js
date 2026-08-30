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
}

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

refresh();
setInterval(refresh, REFRESH_MS);