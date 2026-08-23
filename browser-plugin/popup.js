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
    selectedResolution: ""
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
    const language = (el("language").value || "").toLowerCase();
    const actress = el("actress").value.trim();
    const url = state.pageUrl || "";

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
    el("media-type").value = prefs.mediaType || "song";
    el("media-type").dispatchEvent(new Event("change"));
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

    // Set background image with overlay layer
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
    titleEl.title = displayTitle;
    body.appendChild(titleEl);

    const fileName = item.file_name || "";
    const fileNameEl = text("div", "result-meta", fileName);
    fileNameEl.title = fileName;
    body.appendChild(fileNameEl);

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
async function getNetscapeCookies(url) {
    if (typeof chrome === "undefined" || !chrome.cookies) return null;
    try {
        const targetUrl = new URL(url);
        const domainUrl = `${targetUrl.protocol}//.youtube.com`;

        const [urlCookies, domainCookies] = await Promise.all([
            chrome.cookies.getAll({ url }),
            chrome.cookies.getAll({ url: domainUrl })
        ]);

        const cookieMap = new Map();
        [...urlCookies, ...domainCookies].forEach((c) => {
            const key = `${c.domain}:${c.path}:${c.name}`;
            if (!cookieMap.has(key)) {
                cookieMap.set(key, c);
            }
        });

        const cookies = Array.from(cookieMap.values());
        if (!cookies.length) return null;

        const lines = ["# Netscape HTTP Cookie File"];
        const now = Math.floor(Date.now() / 1000);
        const defaultExpiration = now + 31536000;

        for (const c of cookies) {
            const domain = c.domain;
            const flag = domain.startsWith(".") ? "TRUE" : "FALSE";
            const path = c.path || "/";
            const secure = c.secure ? "TRUE" : "FALSE";
            const expiration = Math.floor(c.expirationDate || defaultExpiration);
            const name = c.name;
            const value = c.value;
            lines.push(`${domain}\t${flag}\t${path}\t${secure}\t${expiration}\t${name}\t${value}`);
        }
        return lines.join("\n");
    } catch (err) {
        return null;
    }
}

function cookieEntryCount(cookieFileContent) {
    if (!cookieFileContent) return 0;
    return cookieFileContent
        .split("\n")
        .filter((line) => line.trim() && !line.trim().startsWith("#")).length;
}

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
        const cookies = await getNetscapeCookies(state.pageUrl);
        const cookieCount = cookieEntryCount(cookies);
        if (cookieCount === 0) {
            toast("No browser cookies found for this page - log into YouTube in this tab, or downloads may get HTTP 403.", "warn");
        }

        state.formats = await api("POST", "/api/ytdlp/formats", {
            url: state.pageUrl,
            cookies: cookies,
            verbose: false
        });

        if (cookieCount > 0 && state.formats.cookies_received === false) {
            toast("Cookies were sent but the server did not accept them - check the backend logs.", "warn");
        }

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
            await api("POST", "/api/ytdlp/download-entry", { entry: downloadEntry, title: state.pageTitle || "" });
            toast(`Added download entry ${downloadEntry} for ${state.pageTitle || "Unknown Title"}`, "success");
        } catch (err) {
            toast(`Failed to save entry: ${err.message}`, "error");
        }
        return;
    }

    const cookies = await getNetscapeCookies(state.pageUrl);
    const cookieCount = cookieEntryCount(cookies);
    if (cookieCount === 0) {
        toast("Starting download with no cookies attached - expect possible HTTP 403.", "warn");
    }

    const payload = {
        ...targetPayload(),
        url: state.pageUrl,
        video_format: videoFormat,
        audio_format: videoFormat.has_audio ? null : audioFormat,
        cookies: cookies
    };

    if (!videoFormat.has_audio && !payload.audio_format) {
        toast("No separate audio stream found for this video", "error");
        return;
    }

    persistPrefs();
    setJobStatus(`Queuing download: Video [${videoFormat.height}p] + Audio [${audioFormat?.format_id || 'muxed'}]...`, false);
    document.querySelectorAll(".fmt-btn").forEach((b) => { b.disabled = true; });

    try {
        const job = await api("POST", "/api/ytdlp/download", payload);
        if (cookieCount > 0 && job.cookies_received === false) {
            toast("Cookies were sent but the server did not report using them - check the backend logs.", "warn");
        }
        connectJobSSE(job.id);
    } catch (err) {
        setJobStatus(`Download failed: ${err.message}`, true);
        document.querySelectorAll(".fmt-btn").forEach((b) => { b.disabled = false; });
    }
}

function connectJobSSE(jobId) {
    const sseUrl = `${state.serverUrl}/api/ytdlp/stream/${jobId}`;
    const evtSource = new EventSource(sseUrl);

    evtSource.onmessage = (event) => {
        const job = JSON.parse(event.data);
        const isFailed = job.status === "failed";

        setJobStatus(job.message || `${job.status}...`, isFailed);

        if (job.status === "success" || job.status === "completed" || isFailed) {
            evtSource.close();
            document.querySelectorAll(".fmt-btn").forEach((b) => { b.disabled = false; });
            toast(job.message, isFailed ? "error" : "success");
        }
    };

    evtSource.onerror = () => {
        evtSource.close();
        document.querySelectorAll(".fmt-btn").forEach((b) => { b.disabled = false; });
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
        note.textContent = `Audio track: ${audio.ext || "?"} · ${audio.filesize_human}`;
        note.style.color = "var(--fmt-audio)";
        container.appendChild(note);
    }
}

/* ----------------------------------------------------------- download */

function targetPayload() {
    const mediaType = el("media-type").value;
    return {
        media_type: mediaType,
        title: (state.formats?.suggested_filename || state.pageTitle || "").trim(),
        language: el("language").value,
        quality: el("quality").value,
        actress: el("actress").value.trim(),
        industry: el("industry").value,
        movie_name: el("movie-name").value.trim()
    };
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

function setJobStatus(message, failed) {
    const box = el("job-status");
    box.classList.remove("hidden");
    box.classList.toggle("failed", Boolean(failed));
    box.textContent = message;
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