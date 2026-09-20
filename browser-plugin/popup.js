const DEFAULT_SERVER = "http://192.168.12.199:2345";
const SEARCH_LIMIT = 5;
const THUMB_W = 251;
const THUMB_H = 377;
const THUMB_PLACEHOLDER =
    "data:image/svg+xml," +
    encodeURIComponent(
        `<svg xmlns='http://www.w3.org/2000/svg' width='${THUMB_W}' height='${THUMB_H}'><rect width='100%' height='100%' fill='#333'/></svg>`
    );

const FOLDER_TAG_COLORS = ["#2ecc71", "#3b82f6", "#e74c3c"];

const state = {
    serverUrl: DEFAULT_SERVER,
    options: null,
    pageUrl: "",
    pageTitle: "",
    pageStrings: [],
    formats: null,
    results: [],
    pollTimer: null,
    hideLabels: false,
    buildEntryOnly: false,
    selectedResolution: "",
    jobStatusEvents: [],
    jobStatusExpanded: false
};

const el = (id) => document.getElementById(id);

document.addEventListener("DOMContentLoaded", init);

/* ------------------------------------------------------------------ init */

async function init() {
    const prefs = await chrome.storage.local.get([
        "serverUrl", "mediaType", "language", "quality", "actress", "industry",
        "hideLabels", "buildEntryOnly"
    ]);
    state.serverUrl = (prefs.serverUrl || DEFAULT_SERVER).replace(/\/+$/, "");
    el("server-url").value = state.serverUrl;

    state.hideLabels = Boolean(prefs.hideLabels);
    state.buildEntryOnly = Boolean(prefs.buildEntryOnly);
    el("toggle-hide-labels").checked = state.hideLabels;
    el("toggle-build-entry-only").checked = state.buildEntryOnly;
    applyHideLabelsState();

    buildMediaTypeToggle();
    bindEvents();
    await loadOptions(prefs);
    await readActiveTab();

    if (state.pageUrl) {
        searchLibrary();
        fetchFormats();
    }
}

function bindEvents() {
    el("btn-settings").addEventListener("click", () => el("settings-panel").classList.toggle("hidden"));
    el("settings-save").addEventListener("click", saveServerUrl);
    el("search-btn").addEventListener("click", searchLibrary);
    el("search-input").addEventListener("keypress", (e) => { if (e.key === "Enter") searchLibrary(); });
    el("formats-btn").addEventListener("click", fetchFormats);
    el("job-status").addEventListener("click", toggleJobStatusConsole);

    el("toggle-hide-labels").addEventListener("change", (e) => {
        state.hideLabels = e.target.checked;
        chrome.storage.local.set({ hideLabels: state.hideLabels });
        applyHideLabelsState();
    });

    el("toggle-build-entry-only").addEventListener("change", (e) => {
        state.buildEntryOnly = e.target.checked;
        chrome.storage.local.set({ buildEntryOnly: state.buildEntryOnly });
    });

    el("download-entry-preview").addEventListener("click", copyDownloadEntry);

    el("media-type").addEventListener("change", () => {
        const isMovie = el("media-type").value === "movie";
        el("song-fields").classList.toggle("hidden", isMovie);
        el("movie-fields").classList.toggle("hidden", !isMovie);
        persistPrefs();
        updateTargetPreview();
        updateDownloadEntry();
        if (state.results.length) renderResults();
    });

    ["language", "quality", "actress", "industry", "movie-name"].forEach((id) => {
        el(id).addEventListener("change", () => {
            persistPrefs();
            updateTargetPreview();
            updateDownloadEntry();
        });
        el(id).addEventListener("input", () => {
            updateTargetPreview();
            updateDownloadEntry();
        });
    });
}

function applyHideLabelsState() {
    document.body.classList.toggle("hide-labels", state.hideLabels);
}

function buildDownloadEntry(res) {
    const resolution = res || state.selectedResolution || "";
    const url = state.pageUrl || "";

    if (isMovieMode()) {
        // Same 4-field shape as songs: url|resolution|lang|name
        // lang is "english" (hollywood) or a non-English value (bollywood).
        return `${url}|${resolution}|${movieLanguage()}|${el("movie-name").value.trim()}`;
    }

    const language = (el("language").value || "").toLowerCase();
    const actress = el("actress").value.trim();

    return `${url}|${resolution}|${language}|${actress}`;
}

function updateDownloadEntry(res) {
    if (res) state.selectedResolution = res;
    const entry = buildDownloadEntry();
    el("download-entry-preview").textContent = entry || "Select resolution / metadata";
}

async function copyDownloadEntry() {
    const textToCopy = buildDownloadEntry();
    if (!textToCopy) {
        toast("Download entry is empty", "warn");
        return;
    }

    try {
        await navigator.clipboard.writeText(textToCopy);
        toast("Download entry copied to clipboard!", "success");
    } catch (err) {
        toast("Failed to copy entry", "error");
    }
}

async function loadOptions(prefs) {
    try {
        state.options = await api("GET", "/api/ytdlp/options");
    } catch (err) {
        state.options = {
            languages: ["Hindi", "South", "Marathi", "English", "Bhojpuri"],
            qualities: ["xhd", "hd", "sd"],
            industries: ["bollywood", "hollywood"],
            songs_root: "/media/storage/songs",
            movies_root: "/media/storage/movies"
        };
        toast(`Could not reach ${state.serverUrl}: ${err.message}`, "error");
    }

    fillSelect(el("language"), state.options.languages, prefs.language);
    fillSelect(el("quality"), state.options.qualities, prefs.quality);
    fillSelect(el("industry"), state.options.industries, prefs.industry);
    el("actress").value = prefs.actress || "";
    setMediaType(prefs.mediaType);
}

function fillSelect(select, values, selected) {
    select.textContent = "";
    (values || []).forEach((value) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        if (value === selected) option.selected = true;
        select.appendChild(option);
    });
}

function persistPrefs() {
    chrome.storage.local.set({
        mediaType: el("media-type").value,
        language: el("language").value,
        quality: el("quality").value,
        actress: el("actress").value.trim(),
        industry: el("industry").value
    });
}

function saveServerUrl() {
    const url = el("server-url").value.trim().replace(/\/+$/, "");
    if (!/^https?:\/\//.test(url)) {
        toast("Server URL must start with http:// or https://", "error");
        return;
    }
    state.serverUrl = url;
    chrome.storage.local.set({ serverUrl: url });
    el("settings-panel").classList.add("hidden");
    toast("Server URL saved", "success");
}

/* ------------------------------------------------------- page scraping */

async function readActiveTab() {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.url || !/^https?:/.test(tab.url)) {
        el("page-url").textContent = "No supported page in the active tab.";
        updateDownloadEntry();
        return;
    }

    state.pageUrl = tab.url;
    el("page-url").textContent = tab.url;

    updateDownloadEntry();

    try {
        const [injected] = await chrome.scripting.executeScript({
            target: { tabId: tab.id },
            func: scrapeFormattedStrings
        });
        const data = injected?.result || {};
        state.pageTitle = data.title || tab.title || "";
        state.pageStrings = data.strings || [];
    } catch (err) {
        state.pageTitle = tab.title || "";
    }

    el("search-input").value = state.pageTitle;
    if (!el("movie-name").value) el("movie-name").value = state.pageTitle;
    updateTargetPreview();
}

function scrapeFormattedStrings() {
    const heading = document.querySelector(
        "ytd-watch-metadata h1 yt-formatted-string, h1.title yt-formatted-string, h1 yt-formatted-string"
    );
    const texts = Array.from(document.querySelectorAll("yt-formatted-string"))
        .map((node) => (node.textContent || "").trim())
        .filter((text) => text.length > 2 && text.length < 180);

    return {
        title: (heading?.textContent || document.title || "").trim(),
        strings: Array.from(new Set(texts)).slice(0, 8)
    };
}

/* ------------------------------------------------------------- search */

async function searchLibrary() {
    const query = el("search-input").value.trim();
    const container = el("search-results");
    if (!query) {
        toast("Nothing to search for", "warn");
        return;
    }

    container.textContent = "";
    container.appendChild(emptyState("Searching vector embeddings..."));

    try {
        state.results = await api("GET", `/api/search?q=${encodeURIComponent(query)}&limit=${SEARCH_LIMIT}`);
        renderResults();
    } catch (err) {
        container.textContent = "";
        container.appendChild(emptyState(`Search failed: ${err.message}`));
    }
}

function renderResults() {
    const container = el("search-results");
    container.textContent = "";

    if (!state.results.length) {
        container.appendChild(emptyState("No matches in the library."));
        return;
    }

    state.results.slice(0, SEARCH_LIMIT).forEach((item) => container.appendChild(resultRow(item)));
}

function resultRow(item) {
    const row = document.createElement("div");
    row.className = "result-row";

    const bgUrl = thumbnailUrl(item);
    if (bgUrl && bgUrl !== THUMB_PLACEHOLDER) {
        row.style.backgroundImage = `url("${bgUrl}")`;
    }

    const overlay = document.createElement("div");
    overlay.className = "result-row-overlay";
    row.appendChild(overlay);

    const body = document.createElement("div");
    body.className = "result-body";

    const displayTitle = item.normalized_title || item.file_name || "Untitled";
    const titleEl = text("div", "result-title", displayTitle);
    titleEl.title = displayTitle + " (Ctrl+Click to open in full player tab)";
    titleEl.style.cursor = "pointer";
    titleEl.addEventListener("click", (e) => {
        if (e.ctrlKey || e.metaKey) {
            openInFullAppTab(item);
        } else {
            openPlayer(item);
        }
    });
    body.appendChild(titleEl);

    body.appendChild(
        text(
            "div",
            "result-meta",
            [
                item.mount || "N/A",
                item.resolution || item.metadata?.resolution || "N/A",
                item.quality || item.metadata?.quality || "N/A",
                item.duration_formatted || item.metadata?.duration_formatted || "N/A",
                item.size_human || item.metadata?.file_size_human || "N/A"
            ].join(" · ")
        )
    );

    const vectorId = String(item.id || item.vector_id || "N/A");
    const mysqlId = String(item.mysql_id || item.db_id || item.file_id || "N/A");

    const dbRow = document.createElement("div");
    dbRow.className = "db-identifiers";
    dbRow.innerHTML = `
        <span class="result-score">score ${item.score}</span>
        <span><code class="clickable-id" title="Click to copy Vector ID">${escapeHtml(vectorId)}</code></span>
    `;
    // <span><code class="clickable-id" title="Click to copy MySQL ID">${escapeHtml(mysqlId)}</code></span>

    const codes = dbRow.querySelectorAll(".clickable-id");
    if (codes[0]) codes[0].addEventListener("click", () => copyToClipboard(vectorId, "Vector ID"));
    if (codes[1]) codes[1].addEventListener("click", () => copyToClipboard(mysqlId, "MySQL ID"));

    const cleanBtn = document.createElement("button");
    cleanBtn.className = "btn-icon-clean";
    cleanBtn.title = "Clean index (Remove from DBs, keep disk file)";
    cleanBtn.innerHTML = `
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="3 6 5 6 21 6"></polyline>
            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
            <line x1="10" y1="11" x2="10" y2="17"></line>
            <line x1="14" y1="11" x2="14" y2="17"></line>
        </svg>
    `;
    cleanBtn.addEventListener("click", () => cleanRecord(item, cleanBtn));
    dbRow.appendChild(cleanBtn);
    body.appendChild(dbRow);

    const tags = item.folder_tags || [];
    if (tags.length) {
        const tagRow = document.createElement("div");
        tagRow.className = "folder-tags";

        tags.forEach((tag, i) => {
            const chip = text("span", "folder-tag", tag);
            chip.style.color = FOLDER_TAG_COLORS[i] || "#cccccc";
            chip.style.borderColor = FOLDER_TAG_COLORS[i] || "#cccccc";
            const targetField = folderTagTarget(i);
            if (targetField) {
                chip.classList.add("folder-tag-clickable");
                chip.setAttribute("role", "button");
                chip.tabIndex = 0;
                chip.title = `Use as ${targetField}`;
                chip.addEventListener("click", () => populateFromFolderTag(targetField, tag));
                chip.addEventListener("keydown", (event) => {
                    if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        populateFromFolderTag(targetField, tag);
                    }
                });
            }
            tagRow.appendChild(chip);
        });

        const allChip = text("span", "folder-tag folder-tag-clickable", "All");
        allChip.style.color = "#ffffff";
        allChip.style.borderColor = "#ffffff";
        allChip.setAttribute("role", "button");
        allChip.tabIndex = 0;
        allChip.title = "Apply all folder tags to inputs";

        const applyAllTags = () => {
            tags.forEach((tag, i) => {
                const targetField = folderTagTarget(i);
                if (targetField) {
                    populateFromFolderTag(targetField, tag);
                }
            });
        };

        allChip.addEventListener("click", applyAllTags);
        allChip.addEventListener("keydown", (event) => {
            if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                applyAllTags();
            }
        });

        tagRow.appendChild(allChip);
        body.appendChild(tagRow);
    }

    const actions = document.createElement("div");
    actions.className = "result-actions";

    const renameBtn = button("Rename", "btn btn-secondary btn-tiny");
    const deleteBtn = button("Delete", "btn btn-danger btn-tiny");
    actions.append(renameBtn, deleteBtn);
    body.appendChild(actions);

    const renameRow = document.createElement("div");
    renameRow.className = "rename-row hidden";
    const renameInput = document.createElement("input");
    renameInput.type = "text";
    renameInput.value = item.file_name || "";
    const saveBtn = button("Save", "btn btn-primary btn-tiny");
    const cancelBtn = button("Cancel", "btn btn-secondary btn-tiny");
    renameRow.append(renameInput, saveBtn, cancelBtn);
    body.appendChild(renameRow);

    renameBtn.addEventListener("click", () => renameRow.classList.toggle("hidden"));
    cancelBtn.addEventListener("click", () => renameRow.classList.add("hidden"));

    saveBtn.addEventListener("click", async () => {
        const newName = renameInput.value.trim();
        if (!newName || newName.includes("/") || newName.includes("\\")) {
            toast("Enter a file name without path separators", "error");
            return;
        }
        saveBtn.disabled = true;
        try {
            await api("POST", "/api/actions/rename", { old_path: item.file_path, new_name: newName });
            item.file_name = newName;
            item.file_path = item.file_path.replace(/[^/\\]+$/, newName);
            toast(`Renamed to ${newName}`, "success");
            renderResults();
        } catch (err) {
            toast(`Rename failed: ${err.message}`, "error");
            saveBtn.disabled = false;
        }
    });

    deleteBtn.addEventListener("click", async () => {
        if (deleteBtn.dataset.armed !== "1") {
            deleteBtn.dataset.armed = "1";
            deleteBtn.textContent = "Confirm delete?";
            return;
        }
        deleteBtn.disabled = true;
        try {
            await api("DELETE", `/api/actions/file?path=${encodeURIComponent(item.file_path)}`);
            state.results = state.results.filter((r) => r !== item);
            toast("File deleted", "success");
            renderResults();
        } catch (err) {
            toast(`Delete failed: ${err.message}`, "error");
            deleteBtn.disabled = false;
        }
    });

    row.appendChild(body);
    return row;
}

async function copyToClipboard(value, label) {
    if (!value || value === "N/A") {
        toast(`${label} unavailable`, "warn");
        return;
    }
    try {
        await navigator.clipboard.writeText(value);
        toast(`${label} copied!`, "success");
    } catch {
        toast(`Failed to copy ${label}`, "error");
    }
}

function folderTagTarget(index) {
    if (el("media-type").value === "movie") return index === 1 ? "industry" : null;
    return ["actress", "quality", "language"][index] || null;
}

function populateFromFolderTag(targetField, value) {
    el(targetField).value = value;
    persistPrefs();
    updateTargetPreview();
    updateDownloadEntry();
}

function thumbnailUrl(item) {
    const jellyfinId = item.jellyfin?.jellyfin_id || item.jellyfin?.jf_id;
    if (!jellyfinId) return THUMB_PLACEHOLDER;

    const params = new URLSearchParams({ jellyfin_id: jellyfinId, width: THUMB_W, height: THUMB_H });
    const tag = item.primary_image_tag || item.jellyfin?.primary_image_tag;
    if (tag) params.set("tag", tag);
    return `${state.serverUrl}/api/media/thumbnail?${params.toString()}`;
}

/* ------------------------------------------------------------ formats */

async function fetchFormats() {
    if (!state.pageUrl) {
        toast("No page URL to download from", "warn");
        return;
    }

    const container = el("format-buttons");
    container.textContent = "";
    container.appendChild(emptyState("Probing available formats..."));
    el("formats-btn").disabled = true;

    try {

        state.formats = await api("POST", "/api/ytdlp/formats", {
            url: state.pageUrl,
            verbose: false,
            media_type: el("media-type").value
        });

        if (state.formats.title) {
            if (!el("search-input").value) el("search-input").value = state.formats.title;
            if (!el("movie-name").value) el("movie-name").value = state.formats.title;
        }
        suggestQuality();
        renderFormats();
        updateTargetPreview();
    } catch (err) {
        container.textContent = "";
        container.appendChild(emptyState(`Format lookup failed: ${err.message}`));
    } finally {
        el("formats-btn").disabled = false;
    }
}

async function startDownload(videoFormat, audioFormat) {
    const resString = videoFormat.height ? String(videoFormat.height) : "";
    updateDownloadEntry(resString);

    const downloadEntry = buildDownloadEntry();

    if (state.buildEntryOnly) {
        if (!downloadEntry) {
            toast("Download entry is empty", "warn");
            return;
        }

        try {
            const title = targetPayload().title;
            await api("POST", "/api/ytdlp/download-entry", { entry: downloadEntry, title });
            toast(`Added download entry ${downloadEntry} for ${title || "Unknown Title"}`, "success");
        } catch (err) {
            toast(`Failed to save entry: ${err.message}`, "error");
        }
        return;
    }

    const payload = {
        ...targetPayload(),
        url: state.pageUrl,
        video_format: videoFormat,
        audio_format: videoFormat.has_audio ? null : audioFormat,
    };

    if (!videoFormat.has_audio && !payload.audio_format) {
        toast("No separate audio stream found for this video", "error");
        return;
    }

    persistPrefs();
    resetJobStatusConsole();
    setJobStatus(`Queuing download: Video [${videoFormat.height}p] + Audio [${audioFormat?.format_id || 'muxed'}]...`, false);
    document.querySelectorAll(".fmt-btn").forEach((b) => { b.disabled = true; });

    try {
        const job = await api("POST", "/api/ytdlp/download", payload);

        connectJobSSE(job.id);
    } catch (err) {
        setJobStatus(`Download failed: ${err.message}`, true);
        document.querySelectorAll(".fmt-btn").forEach((b) => { b.disabled = false; });
    }
}

function formatBytes(n) {
    n = Number(n);
    if (!isFinite(n) || n <= 0) return "";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1);
    return `${(n / Math.pow(1024, i)).toFixed(i ? 1 : 0)} ${units[i]}`;
}

function describeJobEvent(job) {
    const status = job.status || "update";
    let head = status;
    if (job.phase) head += ` [${job.phase}]`;
    if (job.progress !== undefined && job.progress !== null && job.progress !== "") {
        head += ` ${job.progress}%`;
    }

    const parts = [head];
    if (job.message) parts.push(job.message);
    if (job.error) parts.push(job.error);
    if (job.file) {
        const size = formatBytes(job.size);
        parts.push(size ? `${job.file} (${size})` : job.file);
    }
    const host = job.hostname || job.host;
    if (host) parts.push(`@${host}`);
    return parts.join(" · ");
}

function connectJobSSE(jobId) {
    const evtSource = new EventSource(`${state.serverUrl}/api/ytdlp/stream/${jobId}`);
    const enableButtons = () =>
        document.querySelectorAll(".fmt-btn").forEach((b) => { b.disabled = false; });

    evtSource.onmessage = (event) => {
        let job;
        try {
            job = JSON.parse(event.data);
        } catch {
            setJobStatus(event.data, false);   // non-JSON line: show it as-is
            return;
        }

        const failed = job.status === "failed" || job.status === "error" || Boolean(job.error);
        const done = failed || job.status === "success" || job.status === "completed";
        const line = describeJobEvent(job);

        setJobStatus(line, failed);

        if (done) {
            evtSource.close();
            enableButtons();
            toast(line, failed ? "error" : "success");
        }
    };

    evtSource.onerror = () => {
        evtSource.close();
        enableButtons();
        setJobStatus("Lost connection to the job stream", true);
    };
}

function qualityForHeight(height) {
    if (!height) return "sd";
    if (height > 1080) return "xhd";
    if (height >= 720) return "hd";
    return "sd";
}

function fmtBucket(height) {
    if (!height || height < 720) return "sd";
    if (height < 1080) return "720";
    if (height < 1440) return "1080";
    if (height < 2160) return "1440";
    return "2160";
}

function suggestQuality() {
    const heights = (state.formats?.video_formats || []).map((f) => f.height).filter(Boolean);
    if (!heights.length) return;
    el("quality").value = qualityForHeight(Math.max(...heights));
    persistPrefs();
}

function renderFormats() {
    const container = el("format-buttons");
    container.textContent = "";

    const processor = state.formats?.processor || "legacy";
    const serviceHost = state.formats?.service_host || state.formats?.nats_host || "unknown host";

    const videos = state.formats?.video_formats || [];
    if (!videos.length) {
        container.appendChild(emptyState("No 720/1080/1440/2160 streams available."));
        return;
    }

    const audio = state.formats.audio_format;

    // Sort descending by resolution (height) and slice the top 4
    const topVideos = [...videos]
        .sort((a, b) => (b.height || 0) - (a.height || 0))
        .slice(0, 4);

    topVideos.forEach((fmt) => {
        const btn = document.createElement("button");
        btn.className = `fmt-btn fmt-${fmtBucket(fmt.height)}`;
        btn.appendChild(text("span", "", `${fmt.height}p ${fmt.ext || ""}`.trim()));
        btn.appendChild(text("small", "", `${fmt.filesize_human} · ${qualityForHeight(fmt.height)}`));
        btn.addEventListener("click", () => startDownload(fmt, audio));
        container.appendChild(btn);
    });

    if (audio) {
        const note = document.createElement("div");
        note.className = "empty-state fmt-audio-note";
        const audioText = text("span", "", `Audio track: ${audio.ext || "?"} · ${audio.filesize_human || "Unknown size"}`);
        const serviceTag = text("span", `format-tag format-tag-${processor}`, processor.toUpperCase());
        const hostTag = text("span", "format-tag format-tag-host", serviceHost);
        note.append(audioText, serviceTag, hostTag);
        container.appendChild(note);
    }
}

/* ------------------------------------------------- song / movie toggle */

const MEDIA_TOGGLE_CSS = `
.media-toggle { display: inline-flex; align-items: center; gap: 10px; user-select: none; }
.media-toggle-side { cursor: pointer; opacity: .55; font-weight: 500; transition: opacity .15s; }
.media-toggle-side.active { opacity: 1; }
.media-switch { position: relative; display: inline-block; flex: none; width: 40px; height: 22px; }
.media-switch input { position: absolute; inset: 0; width: 100%; height: 100%; margin: 0; opacity: 0; cursor: pointer; z-index: 1; }
.media-switch-track { position: absolute; inset: 0; border-radius: 11px; background: #3b82f6; }
.media-switch-thumb { position: absolute; top: 3px; left: 3px; width: 16px; height: 16px; border-radius: 50%; background: #fff; transition: transform .15s; }
.media-switch input:checked ~ .media-switch-track .media-switch-thumb { transform: translateX(18px); }
.media-switch input:focus-visible ~ .media-switch-track { outline: 2px solid #93c5fd; outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) {
    .media-toggle-side, .media-switch-thumb { transition: none; }
}
`;

// Replaces the #media-type <select> with a Song/Movie switch. A hidden input
// keeps the id "media-type", so every existing `el("media-type").value` read
// and the "change" listener in bindEvents() keep working unchanged.
function buildMediaTypeToggle() {
    const select = el("media-type");
    if (!select || select.tagName !== "SELECT") return;

    if (!document.getElementById("media-toggle-style")) {
        const style = document.createElement("style");
        style.id = "media-toggle-style";
        style.textContent = MEDIA_TOGGLE_CSS;
        document.head.appendChild(style);
    }

    const hidden = document.createElement("input");
    hidden.type = "hidden";
    hidden.id = "media-type";
    hidden.value = "song";

    const wrap = document.createElement("div");
    wrap.className = "media-toggle";
    wrap.setAttribute("role", "group");
    wrap.setAttribute("aria-label", "Media type");

    const songSide = text("span", "media-toggle-side", "Song");
    songSide.dataset.side = "song";
    const movieSide = text("span", "media-toggle-side", "Movie");
    movieSide.dataset.side = "movie";

    const switchLabel = document.createElement("label");
    switchLabel.className = "media-switch";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.id = "media-type-toggle";
    checkbox.setAttribute("role", "switch");
    checkbox.setAttribute("aria-label", "Movie mode");
    const track = document.createElement("span");
    track.className = "media-switch-track";
    track.appendChild(document.createElement("span")).className = "media-switch-thumb";
    switchLabel.append(checkbox, track);

    wrap.append(songSide, switchLabel, movieSide);

    checkbox.addEventListener("change", () => setMediaType(checkbox.checked ? "movie" : "song"));
    songSide.addEventListener("click", () => setMediaType("song"));
    movieSide.addEventListener("click", () => setMediaType("movie"));

    select.replaceWith(wrap, hidden);
}

function setMediaType(type) {
    const next = type === "movie" ? "movie" : "song";
    const hidden = el("media-type");
    hidden.value = next;

    const checkbox = el("media-type-toggle");
    if (checkbox) checkbox.checked = next === "movie";
    document.querySelectorAll(".media-toggle-side").forEach((node) => {
        node.classList.toggle("active", node.dataset.side === next);
    });

    hidden.dispatchEvent(new Event("change"));
}

function isMovieMode() {
    return el("media-type").value === "movie";
}

// Movie folder rule: English -> hollywood, everything else -> bollywood.
// Mirrors ytdlp.resolve_movie_target() on the server.
function movieIndustry() {
    return el("industry").value === "hollywood" ? "hollywood" : "bollywood";
}

function movieLanguage() {
    return movieIndustry() === "hollywood" ? "english" : "hindi";
}

/* ----------------------------------------------------------- download */

function targetPayload() {
    const mediaType = el("media-type").value;
    const title = mediaType === "movie"
        ? (el("movie-name").value.trim() || state.formats?.title || state.pageTitle || "")
        : (state.formats?.title || state.pageTitle || "");
    const payload = {
        media_type: mediaType,
        title: title.trim(),
        language: el("language").value,
        quality: el("quality").value,
        actress: el("actress").value.trim(),
        industry: el("industry").value,
        movie_name: el("movie-name").value.trim()
    };

    if (mediaType === "movie") {
        // The song-only fields are hidden in movie mode; don't let their stale
        // values leak into a movie request.
        payload.language = movieLanguage();
        payload.industry = movieIndustry();
        payload.actress = "";
    }
    return payload;
}

function sanitizeComponent(value) {
    return String(value || "").replace(/[<>:"/\\|?*\x00-\x1f]/g, " ").replace(/\s+/g, " ").trim().replace(/^\.+|\.+$/g, "");
}

function updateTargetPreview() {
    const preview = el("target-preview");
    const payload = targetPayload();
    const opts = state.options || {};

    if (payload.media_type === "movie") {
        const movie = sanitizeComponent(payload.movie_name);
        preview.textContent = movie
            ? `${opts.movies_root}/${payload.industry}/${movie}/${movie}.mp4`
            : "Enter a movie name";
        return;
    }

    const actress = sanitizeComponent(payload.actress);
    const stem = sanitizeComponent(payload.title) || "download";
    preview.textContent = actress
        ? `${opts.songs_root}/${payload.language}/${payload.quality}/${actress}/${stem}.mp4`
        : "Enter an actress name";
}

function pollJob(jobId) {
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(async () => {
        try {
            const job = await api("GET", `/api/ytdlp/jobs/${jobId}`);
            if (job.status === "queued" || job.status === "running") {
                setJobStatus(`${job.status}: ${job.message}`, false);
                pollJob(jobId);
                return;
            }
            const failed = job.status === "failed";
            setJobStatus(`${job.status}: ${job.message}`, failed);
            toast(job.message, failed ? "error" : "success");
            document.querySelectorAll(".fmt-btn").forEach((b) => { b.disabled = false; });
        } catch (err) {
            setJobStatus(`Lost track of the job: ${err.message}`, true);
        }
    }, 2000);
}

function resetJobStatusConsole() {
    state.jobStatusEvents = [];
    state.jobStatusExpanded = false;
    const box = el("job-status");
    box.classList.remove("expanded", "failed");
    box.setAttribute("aria-expanded", "false");
}

function toggleJobStatusConsole() {
    if (!state.jobStatusEvents.length) return;
    state.jobStatusExpanded = !state.jobStatusExpanded;
    renderJobStatus();
}

function renderJobStatus() {
    const box = el("job-status");
    box.classList.toggle("expanded", state.jobStatusExpanded);
    box.setAttribute("aria-expanded", String(state.jobStatusExpanded));
    box.textContent = state.jobStatusExpanded
        ? state.jobStatusEvents.join("\n")
        : state.jobStatusEvents[state.jobStatusEvents.length - 1];
}

function setJobStatus(message, failed) {
    const box = el("job-status");
    box.classList.remove("hidden");
    box.classList.toggle("failed", Boolean(failed));
    state.jobStatusEvents.push(message);
    renderJobStatus();
}

/* ------------------------------------------------------------ helpers */

async function api(method, path, body) {
    const res = await fetch(`${state.serverUrl}${path}`, {
        method,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined
    });

    const raw = await res.text();
    let data = null;
    try { data = raw ? JSON.parse(raw) : null; } catch { /* non-JSON error body */ }

    if (!res.ok) {
        throw new Error(data?.detail || raw.slice(0, 160) || `HTTP ${res.status}`);
    }
    return data;
}

function text(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = content;
    return node;
}

function button(label, className) {
    const btn = document.createElement("button");
    btn.className = className;
    btn.textContent = label;
    return btn;
}

function emptyState(message) {
    return text("div", "empty-state", message);
}

function toast(message, type = "info") {
    const node = text("div", `toast toast-${type}`, message);
    el("toast-container").appendChild(node);
    setTimeout(() => node.remove(), 5000);
}

function escapeHtml(str) {
    return String(str || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

async function cleanRecord(item, buttonEl) {
    if (buttonEl.dataset.armed !== "1") {
        buttonEl.dataset.armed = "1";
        buttonEl.style.borderColor = "var(--danger-red)";
        buttonEl.style.color = "var(--danger-red)";
        toast("Click trash icon again to confirm cleaning DB index entries", "warn");
        setTimeout(() => {
            buttonEl.dataset.armed = "0";
            buttonEl.style.borderColor = "";
            buttonEl.style.color = "";
        }, 4000);
        return;
    }

    buttonEl.disabled = true;
    try {
        await api("DELETE", `/api/actions/clean-record?path=${encodeURIComponent(item.file_path)}`);
        state.results = state.results.filter((r) => r !== item);
        toast(`Cleaned DB entries for ${item.file_name}. File kept on disk.`, "success");
        renderResults();
    } catch (err) {
        toast(`Cleaning failed: ${err.message}`, "error");
        buttonEl.disabled = false;
    }
}


// ------------------------------------------------------------------ Player state
let currentPlayerItem = null;
let playerSeekTimer = null;
let arrowSeekIndex = 0;
let arrowSeekSteps = [3, 5, 7, 10];

// ------------------------------------------------------------------ Full app deep link
const FULL_APP_URL = "http://minis.local:2345/";

function openInFullAppTab(item) {
    if (!item) return;
    const jellyfinId = item.jellyfin?.jellyfin_id || item.jellyfin?.jf_id || item.jellyfin_id;
    const title = item.normalized_title || item.file_name || item.name || "";
    const resolution = item.resolution || item.metadata?.resolution || "";
    const sizeHuman = item.size_human || item.metadata?.file_size_human || "";

    const params = new URLSearchParams({ autoplay: "1" });
    if (jellyfinId) {
        params.set("jellyfin_id", jellyfinId);
    } else {
        params.set("file_path", item.file_path || item.path || "");
    }
    if (title) params.set("title", title);
    if (resolution) params.set("resolution", resolution);
    if (sizeHuman) params.set("size", sizeHuman);

    const url = `${FULL_APP_URL}?${params.toString()}`;
    if (typeof chrome !== "undefined" && chrome.tabs?.create) {
        chrome.tabs.create({ url });
    } else {
        window.open(url, "_blank");
    }
}

// ------------------------------------------------------------------ Stream URL
function streamUrl(item) {
    const jellyfinId = item.jellyfin?.jellyfin_id || item.jellyfin?.jf_id || item.jellyfin_id;
    if (jellyfinId) {
        return `${state.serverUrl}/api/media/jellyfin/stream?jellyfin_id=${encodeURIComponent(jellyfinId)}`;
    }
    return `${state.serverUrl}/api/media/stream?path=${encodeURIComponent(item.file_path || item.path || '')}`;
}

// ------------------------------------------------------------------ Player DOM refs
const playerOverlay = document.getElementById('player-overlay');
const playerShell = document.querySelector('.player-shell');
const playerVideo = document.getElementById('player-video');
const playerTitle = document.getElementById('player-title');
const playerMeta = document.getElementById('player-meta');
const playerClose = document.getElementById('player-close');
const playerPlay = document.getElementById('player-play');
const playerMute = document.getElementById('player-mute');
const playerFullscreen = document.getElementById('player-fullscreen');
const playerProgress = document.getElementById('player-progress');
const playerVolume = document.getElementById('player-volume');
const playerCurrent = document.getElementById('player-current');
const playerDuration = document.getElementById('player-duration');
const playerSeekBadge = document.getElementById('player-seek-badge');

// Shuffle/loop/prev/next are present but not used for single-file playback; we keep them for style consistency.
// We'll wire them with no-ops or hide them optionally.
const playerShuffle = document.getElementById('player-shuffle');
const playerPrev = document.getElementById('player-prev');
const playerNext = document.getElementById('player-next');
const playerLoop = document.getElementById('player-loop');

// ------------------------------------------------------------------ Player functions
function openPlayer(item) {
    if (!item) return;
    currentPlayerItem = item;
    const title = item.normalized_title || item.file_name || item.name || 'Untitled';
    playerTitle.textContent = title;
    const resolution = item.resolution || item.metadata?.resolution || '';
    const sizeHuman = item.size_human || item.metadata?.file_size_human || '';
    playerMeta.textContent = [resolution, sizeHuman].filter(Boolean).join(' \u2022 ');
    playerVideo.src = streamUrl(item);
    playerOverlay.classList.remove('hidden');
    playerVideo.volume = parseFloat(playerVolume.value);
    playerVideo.play().catch(() => { });
}

function closePlayer() {
    if (document.fullscreenElement) document.exitFullscreen?.();
    playerVideo.pause();
    playerVideo.removeAttribute('src');
    playerVideo.load();
    playerOverlay.classList.add('hidden');
    currentPlayerItem = null;
    clearTimeout(playerSeekTimer);
    playerSeekTimer = null;
    arrowSeekIndex = 0;
    playerSeekBadge.classList.add('hidden');
}

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

function getSeekIncrement() {
    const step = arrowSeekSteps[Math.min(arrowSeekIndex, arrowSeekSteps.length - 1)];
    arrowSeekIndex++;
    clearTimeout(playerSeekTimer);
    playerSeekTimer = setTimeout(() => { arrowSeekIndex = 0; }, 2000);
    return step;
}

function showSeekBadge(direction, step) {
    if (!playerSeekBadge) return;
    playerSeekBadge.classList.remove('hidden', 'left', 'right');
    if (direction === 'left') {
        playerSeekBadge.classList.add('left');
        playerSeekBadge.innerHTML = `&#171; -${step}s`;
    } else {
        playerSeekBadge.classList.add('right');
        playerSeekBadge.innerHTML = `+${step}s &#187;`;
    }
    clearTimeout(playerSeekTimer);
    playerSeekTimer = setTimeout(() => {
        playerSeekBadge.classList.add('hidden');
    }, 800);
}

function formatClock(seconds) {
    if (!isFinite(seconds)) return '00:00';
    const total = Math.max(0, Math.floor(seconds));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    const mm = String(m).padStart(2, '0');
    const ss = String(s).padStart(2, '0');
    return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

// ------------------------------------------------------------------ Player event listeners
playerClose.addEventListener('click', closePlayer);
playerOverlay.addEventListener('click', (e) => {
    if (e.target === playerOverlay) closePlayer();
});
playerPlay.addEventListener('click', togglePlay);
playerVideo.addEventListener('play', () => { playerPlay.innerHTML = '&#10074;&#10074;'; });
playerVideo.addEventListener('pause', () => { playerPlay.innerHTML = '&#9654;'; });

// Duration label click-to-cycle: total duration -> remaining
let durationDisplayMode = 'duration'; // 'duration' | 'remaining'

function updateDurationLabel() {
    const duration = isFinite(playerVideo.duration) ? playerVideo.duration : 0;
    const current = playerVideo.currentTime || 0;
    if (durationDisplayMode === 'remaining') {
        playerDuration.textContent = `-${formatClock(Math.max(0, duration - current))}`;
    } else {
        playerDuration.textContent = formatClock(duration);
    }
}

playerDuration.style.cursor = 'pointer';
playerDuration.title = 'Click to toggle: duration / remaining time';
playerDuration.addEventListener('click', () => {
    durationDisplayMode = durationDisplayMode === 'duration' ? 'remaining'
        : 'duration';
    updateDurationLabel();
});

playerVideo.addEventListener('loadedmetadata', () => {
    const dur = isFinite(playerVideo.duration) ? playerVideo.duration : 0;
    playerProgress.max = dur;
    updateDurationLabel();
});

playerVideo.addEventListener('timeupdate', () => {
    if (playerProgress === document.activeElement) return;
    playerProgress.value = playerVideo.currentTime;
    playerCurrent.textContent = formatClock(playerVideo.currentTime);
    updateDurationLabel();
});

playerProgress.addEventListener('input', () => {
    playerCurrent.textContent = formatClock(parseFloat(playerProgress.value));
});
playerProgress.addEventListener('change', () => {
    playerVideo.currentTime = parseFloat(playerProgress.value);
});

playerVolume.addEventListener('input', () => {
    playerVideo.volume = parseFloat(playerVolume.value);
    playerVideo.muted = playerVolume.value === 0;
});
playerMute.addEventListener('click', () => {
    playerVideo.muted = !playerVideo.muted;
});
playerVideo.addEventListener('volumechange', () => {
    playerMute.innerHTML = (playerVideo.muted || playerVideo.volume === 0) ? '&#128263;' : '&#128266;';
    if (!playerVideo.muted) playerVolume.value = playerVideo.volume;
});

playerFullscreen.addEventListener('click', toggleFullscreen);
document.addEventListener('fullscreenchange', () => {
    playerFullscreen.title = document.fullscreenElement ? 'Exit fullscreen' : 'Fullscreen';
});

// Keyboard shortcuts when player is open
document.addEventListener('keydown', (e) => {
    if (playerOverlay.classList.contains('hidden')) return;
    if (e.key === 'Escape' && !document.fullscreenElement) closePlayer();
    if (e.key === ' ' && e.target === document.body) {
        e.preventDefault();
        togglePlay();
    }
    if (e.key === 'f' || e.key === 'F') toggleFullscreen();
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
        const isInputTarget = ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target?.tagName);
        if (!isInputTarget) {
            e.preventDefault();
            const step = getSeekIncrement();
            if (e.key === 'ArrowLeft') {
                playerVideo.currentTime = Math.max(0, playerVideo.currentTime - step);
                showSeekBadge('left', step);
            } else {
                const maxTime = isFinite(playerVideo.duration) ? playerVideo.duration : playerVideo.currentTime + step;
                playerVideo.currentTime = Math.min(maxTime, playerVideo.currentTime + step);
                showSeekBadge('right', step);
            }
        }
    }
});

// Double-click video to fullscreen
let playerClickTimer = null;
playerVideo.addEventListener('click', () => {
    if (playerClickTimer) return;
    playerClickTimer = setTimeout(() => {
        playerClickTimer = null;
        togglePlay();
    }, 220);
});
playerVideo.addEventListener('dblclick', (e) => {
    e.preventDefault();
    clearTimeout(playerClickTimer);
    playerClickTimer = null;
    toggleFullscreen();
});

// Autohide chrome when fullscreen (simple version)
let chromeTimer = null;
function showChrome() {
    playerShell.classList.remove('chrome-hidden');
    clearTimeout(chromeTimer);
    if (document.fullscreenElement) {
        chromeTimer = setTimeout(() => playerShell.classList.add('chrome-hidden'), 3000);
    }
}
document.addEventListener('pointermove', () => {
    if (playerOverlay.classList.contains('hidden')) return;
    showChrome();
}, true);
playerShell.addEventListener('mouseenter', showChrome, true);

// Shuffle/loop/prev/next are no-ops in single-file mode – keep them but disable
playerShuffle.style.opacity = '0.4';
playerLoop.style.opacity = '0.4';
playerPrev.style.opacity = '0.4';
playerNext.style.opacity = '0.4';
playerShuffle.style.pointerEvents = 'none';
playerLoop.style.pointerEvents = 'none';
playerPrev.style.pointerEvents = 'none';
playerNext.style.pointerEvents = 'none';