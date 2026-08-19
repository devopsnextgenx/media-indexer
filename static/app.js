document.addEventListener("DOMContentLoaded", () => {
    const searchInput = document.getElementById("search-input");
    const searchBtn = document.getElementById("search-btn");
    const resultsBody = document.getElementById("results-body");

    const btnScan = document.getElementById("btn-scan");
    const btnCleanIndex = document.getElementById("btn-clean-index");
    const btnBulk = document.getElementById("btn-bulk");
    const btnYt = document.getElementById("btn-yt");

    const mountSelect = document.getElementById("mount-select");
    const rescanDiskCheckbox = document.getElementById("rescan-disk");
    const scanConsole = document.getElementById("scan-console");
    const scanStatusText = document.getElementById("scan-status-text");
    const scanLog = document.getElementById("scan-log");
    const scanLogFilter = document.getElementById("scan-log-filter");
    const scanLogCount = document.getElementById("scan-log-count");
    const scanLogAutoscroll = document.getElementById("scan-log-autoscroll");
    const scanLogClear = document.getElementById("scan-log-clear");
    const scanLogHide = document.getElementById("scan-log-hide");
    const scanProgressFill = document.getElementById("scan-progress-fill");

    const modalYt = document.getElementById("modal-yt");
    const ytSubmit = document.getElementById("yt-submit");
    const ytClose = document.getElementById("yt-close");
    const ytCookies = document.getElementById("yt-cookies");
    const ytCookieStatus = document.getElementById("yt-cookie-status");

    const resultsFilterInput = document.getElementById("results-filter");
    const resultsSortSelect = document.getElementById("results-sort");
    const resultsSortDirBtn = document.getElementById("results-sort-dir");
    const resultsCount = document.getElementById("results-count");

    const MAX_LOG_LINES = 5000;
    const activeStreams = new Map();
    const mountStatus = new Map();
    let logFilter = "";
    let visibleLines = 0;
    let totalLines = 0;
    let allResults = [];
    let currentResults = [];
    let availableMounts = [];
    let resultsFilterText = "";
    let sortField = "score";
    let sortDir = "desc";

    loadMounts();

    async function loadMounts() {
        try {
            const res = await fetch("/api/scan/mounts");
            const mounts = await res.json();
            availableMounts = mounts.map((m) => m.mount_name);

            mountSelect.innerHTML = `<option value="all">All Mounts</option>` +
                mounts.map((m) => {
                    const libs = m.folders.flatMap((f) => f.libraries).join(", ");
                    return `<option value="${escapeHtml(m.mount_name)}" title="${escapeHtml(libs)}">${escapeHtml(m.mount_name)}</option>`;
                }).join("");
        } catch (err) {
            console.error("Failed to load mounts:", err);
        }
    }

    searchBtn.addEventListener("click", performSearch);
    searchInput.addEventListener("keypress", (e) => {
        if (e.key === "Enter") performSearch();
    });

    async function performSearch() {
        const q = searchInput.value.trim();
        if (!q) return;

        resultsBody.innerHTML = `<tr><td colspan="8" class="empty-state">Searching vector embeddings...</td></tr>`;

        try {
            const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
            const data = await res.json();
            allResults = data || [];
            applyFilterAndSort();
        } catch (err) {
            resultsBody.innerHTML = `<tr><td colspan="8" class="empty-state" style="color:var(--danger-red)">Error executing query</td></tr>`;
        }
    }

    function applyFilterAndSort() {
        const needle = resultsFilterText.trim().toLowerCase();
        let filtered = !needle
            ? [...allResults]
            : allResults.filter((item) => {
                const haystack = `${item.normalized_title || ""} ${item.file_name || ""} ${item.mount || ""}`.toLowerCase();
                return haystack.includes(needle);
            });

        filtered.sort((a, b) => {
            let va = a[sortField];
            let vb = b[sortField];
            if (typeof va === "string" || typeof vb === "string") {
                va = (va || "").toString().toLowerCase();
                vb = (vb || "").toString().toLowerCase();
                const cmp = va.localeCompare(vb);
                return sortDir === "asc" ? cmp : -cmp;
            }
            va = va ?? 0;
            vb = vb ?? 0;
            return sortDir === "asc" ? va - vb : vb - va;
        });

        renderResults(filtered);
        resultsCount.textContent = allResults.length
            ? `${filtered.length} / ${allResults.length} result${allResults.length === 1 ? "" : "s"}`
            : "";
    }

    resultsFilterInput.addEventListener("input", () => {
        resultsFilterText = resultsFilterInput.value;
        applyFilterAndSort();
    });

    resultsSortSelect.addEventListener("change", () => {
        sortField = resultsSortSelect.value;
        applyFilterAndSort();
    });

    resultsSortDirBtn.addEventListener("click", () => {
        sortDir = sortDir === "asc" ? "desc" : "asc";
        resultsSortDirBtn.dataset.dir = sortDir;
        resultsSortDirBtn.innerHTML = sortDir === "asc" ? "&#8593; Asc" : "&#8595; Desc";
        applyFilterAndSort();
    });

    // Jellyfin poster aspect (2:3)
    const THUMB_WIDTH = 251;
    const THUMB_HEIGHT = 377;
    const THUMB_PLACEHOLDER_SVG = `<svg xmlns='http://www.w3.org/2000/svg' width='${THUMB_WIDTH}' height='${THUMB_HEIGHT}'><rect width='100%' height='100%' fill='%23333'/></svg>`;
    const THUMB_PLACEHOLDER = `data:image/svg+xml,${encodeURIComponent(THUMB_PLACEHOLDER_SVG)}`;

    function thumbnailUrl(item) {
        const jellyfinId = item.jellyfin?.jellyfin_id || item.jellyfin?.jf_id;
        if (!jellyfinId) return THUMB_PLACEHOLDER;

        const params = new URLSearchParams({
            jellyfin_id: jellyfinId,
            width: THUMB_WIDTH,
            height: THUMB_HEIGHT
        });
        const tag = item.primary_image_tag || item.jellyfin?.primary_image_tag;
        if (tag) params.set("tag", tag);

        return `/api/media/thumbnail?${params.toString()}`;
    }

    function escapeHtml(value) {
        return String(value ?? "").replace(/[&<>"']/g, (c) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
        }[c]));
    }

    // Parent -> grandparent -> great-grandparent
    const FOLDER_TAG_COLORS = ["#2ecc71", "#3b82f6", "#e74c3c"];

    function folderTagsHtml(item) {
        const tags = item.folder_tags || [];
        if (!tags.length) return "";
        return `<div class="folder-tags">` + tags.map((tag, i) =>
            `<span class="folder-tag" style="border-color:${FOLDER_TAG_COLORS[i]};color:${FOLDER_TAG_COLORS[i]};">${escapeHtml(tag)}</span>`
        ).join("") + `</div>`;
    }

    function renderResults(items) {
        currentResults = items || [];

        if (currentResults.length === 0) {
            resultsBody.innerHTML = `<tr><td colspan="8" class="empty-state">No semantic matches found.</td></tr>`;
            return;
        }

        resultsBody.innerHTML = currentResults.map((item, idx) => `
            <tr>
                <td class="col-thumb">
                    <img class="thumb-img" src="${thumbnailUrl(item)}" alt="thumb" loading="lazy" onerror="this.src='${THUMB_PLACEHOLDER}'"/>
                </td>
                <td class="col-details">
                    <div class="file-title" title="${escapeHtml(item.normalized_title)}">${escapeHtml(item.normalized_title)}</div>
                    <small class="file-name" title="${escapeHtml(item.file_name)}">${escapeHtml(item.file_name)}</small>
                    ${folderTagsHtml(item)}
                </td>
                <td class="col-mount">${escapeHtml(item.mount)}</td>
                <td class="col-resolution">${escapeHtml(item.resolution || item.metadata?.resolution || 'N/A')}<br/>
                    <small style="color:#888;">${escapeHtml(item.quality || item.metadata?.quality || '')}</small></td>
                <td class="col-duration">${escapeHtml(item.duration_formatted || item.metadata?.duration_formatted || 'N/A')}</td>
                <td class="col-size">${escapeHtml(item.size_human || item.metadata?.file_size_human || 'N/A')}</td>
                <td class="col-score"><span style="color:var(--success-green);">${escapeHtml(item.score)}</span></td>
                <td class="col-actions">
                    <div class="row-actions">
                        <button class="btn btn-secondary" onclick="playMedia(${idx})">Play</button>
                        <button class="btn btn-secondary" onclick="renameMedia(${idx})">Rename</button>
                        <button class="btn btn-danger" onclick="deleteMedia(${idx})">Delete</button>
                    </div>
                </td>
            </tr>
        `).join("");
    }

    // ---------- Scan console ----------

    function formatLogTime(iso) {
        const date = iso ? new Date(iso) : new Date();
        if (Number.isNaN(date.getTime())) return "--:--:--";
        return date.toLocaleTimeString([], { hour12: false });
    }

    function highlightMatches(text) {
        const escaped = escapeHtml(text);
        if (!logFilter) return escaped;
        const needle = escapeHtml(logFilter).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        if (!needle) return escaped;
        return escaped.replace(new RegExp(needle, "gi"), (match) => `<mark>${match}</mark>`);
    }

    function renderLogLine(node) {
        const entry = node.logEntry;
        node.innerHTML =
            `<span class="log-ts">${escapeHtml(formatLogTime(entry.ts))}</span>` +
            `<span class="log-mount">[${escapeHtml(entry.mount || "-")}]</span>` +
            highlightMatches(entry.message || "");
    }

    function lineMatchesFilter(node) {
        return !logFilter || node.dataset.text.includes(logFilter.toLowerCase());
    }

    function updateLogCount() {
        scanLogCount.textContent = logFilter
            ? `${visibleLines} / ${totalLines} match`
            : `${totalLines} lines`;
    }

    function appendLog(entry) {
        const node = document.createElement("div");
        node.className = `scan-log-line level-${entry.level || "info"}`;
        node.logEntry = entry;
        node.dataset.text = `${entry.mount || ""} ${entry.message || ""}`.toLowerCase();
        renderLogLine(node);

        const visible = lineMatchesFilter(node);
        node.style.display = visible ? "" : "none";
        scanLog.appendChild(node);
        totalLines++;
        if (visible) visibleLines++;

        while (scanLog.childElementCount > MAX_LOG_LINES) {
            const dropped = scanLog.firstElementChild;
            if (dropped.style.display !== "none") visibleLines--;
            totalLines--;
            dropped.remove();
        }
    }

    function flushLogs(entries) {
        if (!entries || !entries.length) return;
        const stick = scanLogAutoscroll.checked;
        entries.forEach(appendLog);
        updateLogCount();
        if (stick) scanLog.scrollTop = scanLog.scrollHeight;
    }

    function applyLogFilter() {
        logFilter = scanLogFilter.value.trim();
        visibleLines = 0;
        for (const node of scanLog.children) {
            const visible = lineMatchesFilter(node);
            node.style.display = visible ? "" : "none";
            renderLogLine(node);
            if (visible) visibleLines++;
        }
        updateLogCount();
    }

    scanLogFilter.addEventListener("input", applyLogFilter);
    scanLogClear.addEventListener("click", () => {
        scanLog.innerHTML = "";
        totalLines = 0;
        visibleLines = 0;
        updateLogCount();
    });
    scanLogHide.addEventListener("click", () => scanConsole.classList.add("hidden"));

    function renderStatusHeader() {
        scanStatusText.textContent =
            [...mountStatus.values()].map((s) => s.text).join("\n") || "Ready...";

        const totals = [...mountStatus.values()].reduce(
            (acc, s) => ({ done: acc.done + (s.processed || 0), all: acc.all + (s.total || 0) }),
            { done: 0, all: 0 }
        );
        const pct = totals.all ? Math.min(100, (totals.done / totals.all) * 100) : 0;
        scanProgressFill.style.width = `${pct}%`;
    }

    function setMountStatus(mount, text, processed, total) {
        mountStatus.set(mount, { text: `[${mount}] ${text}`, processed, total });
        renderStatusHeader();
    }

    // Mount-Aware Scanning Function
    btnScan.addEventListener("click", async () => {
        const selectedMount = mountSelect.value;
        const rescanDisk = rescanDiskCheckbox.checked;

        const targetMounts = selectedMount === "all" ? [...availableMounts] : [selectedMount];

        scanConsole.classList.remove("hidden");

        if (targetMounts.length === 0) {
            setMountStatus("config", "No mounts are enabled in config.yml", 0, 0);
            return;
        }

        btnScan.disabled = true;

        for (const mount of targetMounts) {
            setMountStatus(mount, `Requesting scan start (rescan_disk=${rescanDisk})...`, 0, 0);

            try {
                const response = await fetch(
                    `/api/scan/start?mount_name=${encodeURIComponent(mount)}&rescan_disk=${rescanDisk}`,
                    { method: "POST" }
                );
                const data = await response.json();

                if (!response.ok) {
                    setMountStatus(mount, `Error: ${data.detail || data.error || "Failed to start scan"}`, 0, 0);
                    flushLogs([{ mount, level: "error", message: data.detail || "Failed to start scan" }]);
                    continue;
                }

                setMountStatus(mount, data.message || "Scan initiated, connecting to progress stream...", 0, 0);
                listenToStream(mount);
            } catch (err) {
                console.error(`Failed to trigger scan for ${mount}:`, err);
                setMountStatus(mount, "Connection error. Check server logs.", 0, 0);
                flushLogs([{ mount, level: "error", message: `Connection error: ${err.message}` }]);
            }
        }

        btnScan.disabled = false;
    });

    // Formats remaining seconds as e.g. 3m45s / 1h04m / 12s
    function formatEta(seconds) {
        const total = Math.max(0, Math.round(Number(seconds) || 0));
        if (!total) return "--";
        const h = Math.floor(total / 3600);
        const m = Math.floor((total % 3600) / 60);
        const s = total % 60;
        if (h > 0) return `${h}h${String(m).padStart(2, "0")}m`;
        if (m > 0) return `${m}m${String(s).padStart(2, "0")}s`;
        return `${s}s`;
    }

    function closeStream(mountName) {
        const source = activeStreams.get(mountName);
        if (source) {
            source.close();
            activeStreams.delete(mountName);
        }
    }

    // Stream SSE Progress Updates (one independent stream per mount)
    function listenToStream(mountName) {
        closeStream(mountName);

        const source = new EventSource(`/api/scan/stream?mount_name=${encodeURIComponent(mountName)}`);
        activeStreams.set(mountName, source);

        source.onmessage = (event) => {
            let data;
            try {
                data = JSON.parse(event.data);
            } catch (e) {
                flushLogs([{ mount: mountName, level: "warn", message: String(event.data) }]);
                return;
            }

            flushLogs(data.logs);

            const status = (data.status || "").toUpperCase();
            const total = data.total_files ?? data.total ?? 0;
            const processed = data.current_index ?? data.processed_files ?? 0;

            if (status === "FAILED" && data.error) {
                setMountStatus(mountName, `Scan aborted: ${data.error} (processed ${processed}/${total}, failed: ${data.failed_files || 0})`, processed, total);
                closeStream(mountName);
            } else if (status === "COMPLETED" || status === "FAILED") {
                setMountStatus(mountName, `Scan finished! Total processed: ${processed}/${total} (failed: ${data.failed_files || 0})`, processed, total);
                closeStream(mountName);
            } else if (status === "MANIFEST_NOT_FOUND") {
                setMountStatus(mountName, "Waiting for scan manifest...", 0, 0);
            } else if (status === "LOADING_LIBRARIES") {
                const lib = data.current_library || "Jellyfin libraries";
                const libTotal = data.libraries_total || 0;
                const libDone = data.libraries_loaded || 0;
                const items = data.library_items_loaded || 0;
                const itemsTotal = data.library_items_total;
                const itemText = itemsTotal ? `${items}/${itemsTotal}` : `${items}`;
                setMountStatus(mountName, `Loading library ${libDone + 1}/${libTotal || 1}: ${lib} (${itemText} items)`, 0, 0);
            } else {
                const pct = data.progress_percentage ?? 0;
                setMountStatus(mountName, `[eta ${formatEta(data.eta_seconds)}] Progress: ${processed}/${total} (${pct}%) | File: ${data.current_file || "Processing..."}`, processed, total);
            }
        };

        source.onerror = () => {
            const previous = mountStatus.get(mountName);
            setMountStatus(
                mountName,
                "Progress stream disconnected.",
                previous?.processed,
                previous?.total
            );
            flushLogs([{ mount: mountName, level: "warn", message: "Progress stream disconnected." }]);
            closeStream(mountName);
        };
    }

    btnBulk.addEventListener("click", async () => {
        const ok = await askConfirm({
            title: "Bulk rename files on disk",
            message: "Replace all underscores '_' with spaces in mounted file names? This renames files on disk.",
            okLabel: "Rename files"
        });
        if (!ok) return;

        try {
            const res = await fetch("/api/actions/bulk-normalize-underscores", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({})
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || "Bulk rename failed");
            showToast(`Bulk rename complete. Updated ${data.count} files.`, "success");
        } catch (err) {
            showToast(`Bulk rename failed: ${err.message}`, "error", 8000);
        }
    });

    function updateCookieStatus() {
        const hasCookies = ytCookies.value.trim().length > 0;
        ytCookieStatus.textContent = hasCookies
            ? "Cookies provided — will be sent with the format probe and download."
            : "No cookies provided — download may fail with HTTP 403.";
        ytCookieStatus.classList.toggle("ok", hasCookies);
    }

    btnYt.addEventListener("click", () => {
        modalYt.classList.remove("hidden");
        updateCookieStatus();
    });
    ytClose.addEventListener("click", () => modalYt.classList.add("hidden"));
    ytCookies.addEventListener("input", updateCookieStatus);

    btnCleanIndex.addEventListener("click", async () => {
        const ok = await askConfirm({
            title: "Clean vector database",
            message: "Wipe all vectors from Qdrant and reset scan manifests?<br/>The next scan will re-index everything from scratch.",
            okLabel: "Clean index"
        });
        if (!ok) return;

        btnCleanIndex.disabled = true;
        const original = btnCleanIndex.innerText;
        btnCleanIndex.innerText = "Cleaning...";

        try {
            const res = await fetch("/api/admin/index/clean?mode=recreate&clear_manifests=true", { method: "POST" });
            const data = await res.json();

            if (!res.ok) throw new Error(data.detail || "Clean failed");

            scanConsole.classList.remove("hidden");
            mountStatus.clear();
            setMountStatus(
                "admin",
                `Vector DB cleaned: removed ${data.deleted_points} points from '${data.collection}', ` +
                `cleared ${data.cleared_manifests.length} manifest(s). Ready for a fresh scan.`,
                0,
                0
            );
            resultsBody.innerHTML = `<tr><td colspan="8" class="empty-state">Index cleared. Run "Scan & Index" to rebuild.</td></tr>`;
            currentResults = [];
        } catch (err) {
            showToast(`Failed to clean vector DB: ${err.message}`, "error", 8000);
        } finally {
            btnCleanIndex.innerText = original;
            btnCleanIndex.disabled = false;
        }
    });

    ytSubmit.addEventListener("click", async () => {
        const url = document.getElementById("yt-url").value.trim();
        const mount = document.getElementById("yt-mount").value.trim();
        const cookies = ytCookies.value.trim() || null;
        if (!url) return;

        ytSubmit.disabled = true;
        ytSubmit.innerText = "Downloading...";
        try {
            const res = await fetch("/api/actions/download", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ url, mount, cookies })
            });
            const data = await res.json().catch(() => ({}));

            if (!res.ok) {
                showToast(`Download failed: ${data.detail || res.statusText}`, "error", 8000);
                return;
            }

            modalYt.classList.add("hidden");

            // Surface the server's own check of whether cookies were actually received/used.
            if (!data.cookies_received && cookies) {
                showToast("Warning: server did not report receiving cookies — check the backend route.", "warn", 8000);
            } else if (!cookies) {
                showToast("Download started without cookies — expect possible HTTP 403 errors.", "warn", 8000);
            } else {
                showToast("Download started with cookies applied.", "success");
            }
        } catch (err) {
            showToast(`Download failed: ${err.message}`, "error", 8000);
        } finally {
            ytSubmit.disabled = false;
            ytSubmit.innerText = "Download";
        }
    });

    // ---- Overlay Video Player ----
    const playerOverlay = document.getElementById("player-overlay");
    const playerShell = playerOverlay.querySelector(".player-shell");
    const playerVideo = document.getElementById("player-video");
    const playerTitle = document.getElementById("player-title");
    const playerMeta = document.getElementById("player-meta");
    const playerClose = document.getElementById("player-close");
    const playerPlay = document.getElementById("player-play");
    const playerMute = document.getElementById("player-mute");
    const playerFullscreen = document.getElementById("player-fullscreen");
    const playerProgress = document.getElementById("player-progress");
    const playerVolume = document.getElementById("player-volume");
    const playerCurrent = document.getElementById("player-current");
    const playerDuration = document.getElementById("player-duration");

    let seeking = false;

    function formatClock(seconds) {
        if (!isFinite(seconds)) return "00:00";
        const total = Math.max(0, Math.floor(seconds));
        const h = Math.floor(total / 3600);
        const m = Math.floor((total % 3600) / 60);
        const s = total % 60;
        const mm = String(m).padStart(2, "0");
        const ss = String(s).padStart(2, "0");
        return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
    }

    function streamUrl(item) {
        const jellyfinId = item.jellyfin?.jellyfin_id || item.jellyfin?.jf_id;
        if (jellyfinId) {
            return `/api/media/jellyfin/stream?jellyfin_id=${encodeURIComponent(jellyfinId)}`;
        }
        return `/api/media/stream?path=${encodeURIComponent(item.file_path)}`;
    }

    function openPlayer(item) {
        playerTitle.innerText = item.normalized_title || item.file_name;
        const resolution = item.resolution || item.metadata?.resolution || "";
        const sizeHuman = item.size_human || item.metadata?.file_size_human || "";
        playerMeta.innerText = [resolution, sizeHuman].filter(Boolean).join(" \u2022 ");
        playerVideo.src = streamUrl(item);
        playerOverlay.classList.remove("hidden");
        playerVideo.volume = Number(playerVolume.value);
        playerVideo.play().catch(() => { /* autoplay blocked; user presses play */ });
    }

    function closePlayer() {
        if (document.fullscreenElement) document.exitFullscreen?.();
        playerVideo.pause();
        playerVideo.removeAttribute("src");
        playerVideo.load();
        playerOverlay.classList.add("hidden");
    }

    playerClose.addEventListener("click", closePlayer);
    playerOverlay.addEventListener("click", (e) => {
        if (e.target === playerOverlay) closePlayer();
    });
    document.addEventListener("keydown", (e) => {
        if (playerOverlay.classList.contains("hidden")) return;
        if (e.key === "Escape" && !document.fullscreenElement) closePlayer();
        if (e.key === " " && e.target === document.body) {
            e.preventDefault();
            togglePlay();
        }
        if (e.key === "f" || e.key === "F") toggleFullscreen();
    });

    function togglePlay() {
        if (playerVideo.paused) playerVideo.play(); else playerVideo.pause();
    }

    function toggleFullscreen() {
        if (document.fullscreenElement) {
            document.exitFullscreen?.();
        } else {
            (playerShell.requestFullscreen || playerShell.webkitRequestFullscreen)?.call(playerShell);
        }
    }

    playerPlay.addEventListener("click", togglePlay);

    // Delay single-click play/pause so a double-click only toggles fullscreen
    let clickTimer = null;
    playerVideo.addEventListener("click", () => {
        if (clickTimer) return;
        clickTimer = setTimeout(() => {
            clickTimer = null;
            togglePlay();
        }, 220);
    });
    playerVideo.addEventListener("dblclick", (e) => {
        e.preventDefault();
        clearTimeout(clickTimer);
        clickTimer = null;
        toggleFullscreen();
    });
    playerVideo.addEventListener("play", () => { playerPlay.innerHTML = "&#10074;&#10074;"; });
    playerVideo.addEventListener("pause", () => { playerPlay.innerHTML = "&#9654;"; });

    playerVideo.addEventListener("loadedmetadata", () => {
        playerProgress.max = isFinite(playerVideo.duration) ? playerVideo.duration : 0;
        playerDuration.innerText = formatClock(playerVideo.duration);
    });

    playerVideo.addEventListener("timeupdate", () => {
        if (seeking) return;
        playerProgress.value = playerVideo.currentTime;
        playerCurrent.innerText = formatClock(playerVideo.currentTime);
    });

    playerProgress.addEventListener("input", () => {
        seeking = true;
        playerCurrent.innerText = formatClock(Number(playerProgress.value));
    });
    playerProgress.addEventListener("change", () => {
        playerVideo.currentTime = Number(playerProgress.value);
        seeking = false;
    });

    playerVolume.addEventListener("input", () => {
        playerVideo.volume = Number(playerVolume.value);
        playerVideo.muted = playerVideo.volume === 0;
    });

    playerMute.addEventListener("click", () => {
        playerVideo.muted = !playerVideo.muted;
    });

    playerVideo.addEventListener("volumechange", () => {
        playerMute.innerHTML = playerVideo.muted || playerVideo.volume === 0 ? "&#128263;" : "&#128266;";
        if (!playerVideo.muted) playerVolume.value = playerVideo.volume;
    });

    playerFullscreen.addEventListener("click", toggleFullscreen);

    let chromeTimer = null;

    function showChrome() {
        playerShell.classList.remove("chrome-hidden");
        clearTimeout(chromeTimer);
        if (document.fullscreenElement) {
            chromeTimer = setTimeout(() => playerShell.classList.add("chrome-hidden"), 3000);
        }
    }

    // Listen on document so movement over the video/controls always re-shows the chrome
    ["pointermove", "mousemove", "pointerdown", "keydown", "wheel"].forEach((evt) => {
        document.addEventListener(evt, () => {
            if (playerOverlay.classList.contains("hidden")) return;
            showChrome();
        }, true);
    });
    playerShell.addEventListener("mouseenter", showChrome, true);

    document.addEventListener("fullscreenchange", () => {
        playerFullscreen.title = document.fullscreenElement ? "Exit fullscreen" : "Fullscreen";
        if (document.fullscreenElement) {
            showChrome();
        } else {
            clearTimeout(chromeTimer);
            playerShell.classList.remove("chrome-hidden");
        }
    });

    playerVideo.addEventListener("error", () => {
        if (playerVideo.getAttribute("src")) {
            playerTitle.innerText += " — stream unavailable (unsupported codec or offline source)";
        }
    });

    window.playMedia = (index) => {
        const item = currentResults[index];
        if (item) openPlayer(item);
    };

    // ---- Toast Notifications ----
    const toastContainer = document.getElementById("toast-container");
    const TOAST_ICONS = { info: "&#8505;", success: "&#10004;", warn: "&#9888;", error: "&#9888;" };

    function showToast(message, type = "info", timeout = 5000) {
        const toast = document.createElement("div");
        toast.className = `toast toast-${type}`;
        toast.innerHTML = `
            <span class="toast-icon">${TOAST_ICONS[type] || TOAST_ICONS.info}</span>
            <span class="toast-body">${escapeHtml(message)}</span>
            <button class="toast-close" title="Dismiss">&times;</button>
        `;

        const dismiss = () => {
            toast.classList.add("toast-out");
            setTimeout(() => toast.remove(), 200);
        };
        toast.querySelector(".toast-close").addEventListener("click", dismiss);
        toastContainer.appendChild(toast);
        if (timeout) setTimeout(dismiss, timeout);
    }

    // ---- Confirm Modal ----
    const modalConfirm = document.getElementById("modal-confirm");
    const confirmTitle = document.getElementById("confirm-title");
    const confirmMessage = document.getElementById("confirm-message");
    const confirmOk = document.getElementById("confirm-ok");
    const confirmCancel = document.getElementById("confirm-cancel");
    let confirmResolver = null;

    function askConfirm({ title, message, okLabel = "Confirm" }) {
        confirmTitle.innerText = title;
        confirmMessage.innerHTML = message;
        confirmOk.innerText = okLabel;
        modalConfirm.classList.remove("hidden");
        return new Promise((resolve) => { confirmResolver = resolve; });
    }

    function closeConfirm(result) {
        modalConfirm.classList.add("hidden");
        confirmResolver?.(result);
        confirmResolver = null;
    }

    confirmOk.addEventListener("click", () => closeConfirm(true));
    confirmCancel.addEventListener("click", () => closeConfirm(false));
    modalConfirm.addEventListener("click", (e) => {
        if (e.target === modalConfirm) closeConfirm(false);
    });

    // ---- Rename Modal ----
    const modalRename = document.getElementById("modal-rename");
    const renameCurrent = document.getElementById("rename-current");
    const renameSuggestion = document.getElementById("rename-suggestion");
    const renameUseSuggestion = document.getElementById("rename-use-suggestion");
    const renameInput = document.getElementById("rename-input");
    const renameApply = document.getElementById("rename-apply");
    const renameClose = document.getElementById("rename-close");
    let renameTarget = null;

    function splitExtension(fileName) {
        const dot = fileName.lastIndexOf(".");
        return dot > 0 ? [fileName.slice(0, dot), fileName.slice(dot)] : [fileName, ""];
    }

    function parentFolderName(filePath) {
        const parts = String(filePath || "").split(/[\\/]+/).filter(Boolean);
        return parts.length > 1 ? parts[parts.length - 2] : "";
    }

    function tidy(name) {
        return name.replace(/_+/g, " ").replace(/\s+/g, " ").trim();
    }

    // Movies get the folder name; everything else keeps its name with underscores removed.
    function suggestName(item) {
        const [base, ext] = splitExtension(item.file_name || "");
        if ((item.mount || "").toLowerCase() === "movies") {
            const folder = tidy(parentFolderName(item.file_path));
            if (folder) return `${folder}${ext}`;
        }
        return `${tidy(base)}${ext}`;
    }

    function openRenameModal(item) {
        renameTarget = item;
        renameCurrent.innerText = item.file_name;
        renameSuggestion.innerText = suggestName(item);
        renameInput.value = item.file_name;
        modalRename.classList.remove("hidden");
        renameInput.focus();
    }

    function closeRenameModal() {
        modalRename.classList.add("hidden");
        renameTarget = null;
    }

    renameClose.addEventListener("click", closeRenameModal);
    modalRename.addEventListener("click", (e) => {
        if (e.target === modalRename) closeRenameModal();
    });
    renameUseSuggestion.addEventListener("click", () => applyRename(renameSuggestion.innerText));
    renameApply.addEventListener("click", () => applyRename(renameInput.value));
    renameInput.addEventListener("keypress", (e) => {
        if (e.key === "Enter") applyRename(renameInput.value);
    });

    async function applyRename(rawName) {
        if (!renameTarget) return;
        const item = renameTarget;
        const newName = (rawName || "").trim();

        if (!newName) {
            showToast("Please provide a file name.", "warn");
            return;
        }
        if (/[\\/]/.test(newName)) {
            showToast("File name cannot contain path separators.", "error");
            return;
        }
        if (newName === item.file_name) {
            showToast("New name is identical to the current name.", "warn");
            return;
        }

        try {
            const res = await fetch("/api/actions/rename", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ old_path: item.file_path, new_name: newName })
            });
            const data = await res.json();

            if (!res.ok) {
                showToast(`Rename failed: ${data.detail || "unknown error"}`, "error", 8000);
                return;
            }
            closeRenameModal();
            if (data.index_updated) {
                showToast(`Renamed to ${newName} (index updated)`, "success");
            } else {
                showToast(`Renamed to ${newName}, but no index entry was found to update. Re-scan to sync.`, "warn", 8000);
            }
            performSearch();
        } catch (err) {
            showToast(`Rename failed: ${err.message}`, "error", 8000);
        }
    }

    window.renameMedia = (index) => {
        const item = currentResults[index];
        if (item) openRenameModal(item);
    };

    window.deleteMedia = async (index) => {
        const item = currentResults[index];
        if (!item) return;

        const ok = await askConfirm({
            title: "Delete file from disk",
            message: `This permanently deletes the file from disk and cannot be undone.<br/><br/>
                      <strong>${escapeHtml(item.file_name)}</strong><br/>
                      <span style="color:#888">${escapeHtml(item.file_path)}</span>`,
            okLabel: "Delete from disk"
        });
        if (!ok) return;

        try {
            const res = await fetch(`/api/actions/file?path=${encodeURIComponent(item.file_path)}`, { method: "DELETE" });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                showToast(`Delete failed: ${data.detail || res.statusText}`, "error", 8000);
                return;
            }
            if (data.index_removed) {
                showToast(`Deleted ${item.file_name} from disk and removed it from the index.`, "success");
            } else {
                showToast(`Deleted ${item.file_name} from disk, but no index entry was found to remove.`, "warn", 8000);
            }
            performSearch();
        } catch (err) {
            showToast(`Delete failed: ${err.message}`, "error", 8000);
        }
    };
});