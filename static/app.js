document.addEventListener("DOMContentLoaded", () => {
    const searchInput = document.getElementById("search-input");
    const searchBtn = document.getElementById("search-btn");
    const resultsBody = document.getElementById("results-body");

    const searchHistoryBtn = document.getElementById("search-history-btn");
    const searchHistoryMenu = document.getElementById("search-history-menu");

    const btnScan = document.getElementById("btn-scan");
    const btnCleanIndex = document.getElementById("btn-clean-index");
    const btnBulk = document.getElementById("btn-bulk");
    const btnYt = document.getElementById("btn-yt");

    const mountSelect = document.getElementById("mount-select");
    const scanModeToggle = document.getElementById("scan-mode-toggle");
    const scanModeLabel = document.getElementById("scan-mode-label");
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

    // Interactive Token Filter & History Elements
    const filterContainer = document.getElementById("filter-tokens-container");
    const filterTokensList = document.getElementById("filter-tokens-list");
    const filterInputField = document.getElementById("results-filter-input");
    const filterHistoryBtn = document.getElementById("filter-history-btn");
    const filterHistoryMenu = document.getElementById("filter-history-menu");

    const resultsSortSelect = document.getElementById("results-sort");
    const resultsSortDirBtn = document.getElementById("results-sort-dir");
    const resultsCount = document.getElementById("results-count");

    // Library tab elements
    const libraryBreadcrumb = document.getElementById("library-breadcrumb");
    const libraryGrid = document.getElementById("library-grid");
    const librarySentinel = document.getElementById("library-sentinel");
    const libraryLoadingEl = document.getElementById("library-loading");
    const libraryCountEl = document.getElementById("library-count");
    const librarySortSelect = document.getElementById("library-sort");
    const librarySortDirBtn = document.getElementById("library-sort-dir");
    const cardSizeGroup = document.getElementById("card-size-group");
    const btnLibraryRefresh = document.getElementById("btn-library-refresh");

    const MAX_LOG_LINES = 5000;
    const MAX_HISTORY = 15;

    const activeStreams = new Map();
    const mountStatus = new Map();
    let logFilter = "";
    let visibleLines = 0;
    let totalLines = 0;
    let allResults = [];
    let currentResults = [];
    let availableMounts = [];
    let activeFilterTokens = [];
    let sortField = "score";
    let sortDir = "desc";
    let downloadsList = [];
    let downloadStatusFilter = "all";
    let downloadSortField = "created_at";
    let downloadSortDir = "desc";

    // Library tab state
    let libraryMount = "all";
    let libraryPath = "";
    let libraryOffset = 0;
    const LIBRARY_PAGE_SIZE = 150;
    let libraryHasMore = false;
    let libraryLoading = false;
    let libraryItems = [];          // accumulated items for the current folder (across pages)
    let librarySort = "name";
    let librarySortDir = "asc";
    const CARD_SIZE_PX = { xs: 96, s: 126, m: 150, l: 270, xl: 420 };
    const SAVED_CARD_SIZE = localStorage.getItem("library_card_size");
    let cardSize = (SAVED_CARD_SIZE && CARD_SIZE_PX.hasOwnProperty(SAVED_CARD_SIZE)) ? SAVED_CARD_SIZE : "m";
    let libraryRequestToken = 0;    // guards against out-of-order responses when navigating fast
    // Short-lived client cache so breadcrumb "back" navigation doesn't re-hit the server
    const libraryPageCache = new Map(); // key: `${mount}::${path}::${sort}::${sortDir}` -> {items, hasMore, total}
    const LIBRARY_CLIENT_CACHE_TTL = 20000;

    // ==========================================
    // IndexedDB cache for Library browse responses
    // (faster repeat lookups across page reloads; server stays source of truth)
    // ==========================================
    const LIBRARY_IDB_NAME = "media_indexer_library";
    const LIBRARY_IDB_STORE = "browse_cache";
    let libraryIdbPromise = null;

    function openLibraryIdb() {
        if (!("indexedDB" in window)) return Promise.resolve(null);
        if (libraryIdbPromise) return libraryIdbPromise;
        libraryIdbPromise = new Promise((resolve) => {
            try {
                const req = indexedDB.open(LIBRARY_IDB_NAME, 1);
                req.onupgradeneeded = () => {
                    const db = req.result;
                    if (!db.objectStoreNames.contains(LIBRARY_IDB_STORE)) {
                        db.createObjectStore(LIBRARY_IDB_STORE, { keyPath: "cache_key" });
                    }
                };
                req.onsuccess = () => resolve(req.result);
                req.onerror = () => resolve(null);
            } catch {
                resolve(null);
            }
        });
        return libraryIdbPromise;
    }

    async function idbGetLibraryCache(cacheKey) {
        try {
            const db = await openLibraryIdb();
            if (!db) return null;
            return await new Promise((resolve) => {
                const tx = db.transaction(LIBRARY_IDB_STORE, "readonly");
                const req = tx.objectStore(LIBRARY_IDB_STORE).get(cacheKey);
                req.onsuccess = () => resolve(req.result || null);
                req.onerror = () => resolve(null);
            });
        } catch {
            return null;
        }
    }

    async function idbSetLibraryCache(cacheKey, entry) {
        try {
            const db = await openLibraryIdb();
            if (!db) return;
            const tx = db.transaction(LIBRARY_IDB_STORE, "readwrite");
            tx.objectStore(LIBRARY_IDB_STORE).put({ cache_key: cacheKey, ...entry });
        } catch {
            // best-effort only; IndexedDB being unavailable shouldn't break browsing
        }
    }

    async function idbClearLibraryCache() {
        try {
            const db = await openLibraryIdb();
            if (!db) return;
            const tx = db.transaction(LIBRARY_IDB_STORE, "readwrite");
            tx.objectStore(LIBRARY_IDB_STORE).clear();
        } catch {
            // best-effort only
        }
    }

    // ==========================================
    // LocalStorage History Controllers
    // ==========================================

    function getLocalStorageArray(key) {
        try {
            return JSON.parse(localStorage.getItem(key)) || [];
        } catch {
            return [];
        }
    }

    function saveLocalStorageArray(key, arr) {
        try {
            localStorage.setItem(key, JSON.stringify(arr));
        } catch (e) {
            console.error("Failed to save history to localStorage:", e);
        }
    }

    // Search History
    function saveSearchHistory(query) {
        if (!query.trim()) return;
        let history = getLocalStorageArray("app_search_history");
        history = history.filter(q => q.toLowerCase() !== query.toLowerCase());
        history.unshift(query);
        if (history.length > MAX_HISTORY) history = history.slice(0, MAX_HISTORY);
        saveLocalStorageArray("app_search_history", history);
    }

    function renderSearchHistory() {
        const history = getLocalStorageArray("app_search_history");
        if (!history.length) {
            searchHistoryMenu.innerHTML = `<div class="history-empty">No search history</div>`;
            return;
        }

        searchHistoryMenu.innerHTML = history.map((q, idx) => `
            <div class="history-item" data-idx="${idx}">
                <span class="history-item-text">${escapeHtml(q)}</span>
                <span class="history-item-remove" data-remove="${idx}">&times;</span>
            </div>
        `).join("");

        searchHistoryMenu.querySelectorAll(".history-item").forEach(el => {
            el.addEventListener("click", (e) => {
                const rmIdx = e.target.dataset.remove;
                if (rmIdx !== undefined) {
                    e.stopPropagation();
                    let hist = getLocalStorageArray("app_search_history");
                    hist.splice(rmIdx, 1);
                    saveLocalStorageArray("app_search_history", hist);
                    renderSearchHistory();
                    return;
                }
                const q = history[el.dataset.idx];
                searchInput.value = q;
                searchHistoryMenu.classList.add("hidden");
                performSearch();
            });
        });
    }

    searchHistoryBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        filterHistoryMenu.classList.add("hidden");
        renderSearchHistory();
        searchHistoryMenu.classList.toggle("hidden");
    });

    // Filter History
    function saveFilterHistory(tokens) {
        if (!tokens || !tokens.length) return;
        const filterString = tokens.join(" ");
        let history = getLocalStorageArray("app_filter_history");
        history = history.filter(f => f !== filterString);
        history.unshift(filterString);
        if (history.length > MAX_HISTORY) history = history.slice(0, MAX_HISTORY);
        saveLocalStorageArray("app_filter_history", history);
    }

    function renderFilterHistory() {
        const history = getLocalStorageArray("app_filter_history");
        if (!history.length) {
            filterHistoryMenu.innerHTML = `<div class="history-empty">No filter history</div>`;
            return;
        }

        filterHistoryMenu.innerHTML = history.map((fStr, idx) => `
            <div class="history-item" data-idx="${idx}">
                <span class="history-item-text">${escapeHtml(fStr)}</span>
                <span class="history-item-remove" data-remove="${idx}">&times;</span>
            </div>
        `).join("");

        filterHistoryMenu.querySelectorAll(".history-item").forEach(el => {
            el.addEventListener("click", (e) => {
                const rmIdx = e.target.dataset.remove;
                if (rmIdx !== undefined) {
                    e.stopPropagation();
                    let hist = getLocalStorageArray("app_filter_history");
                    hist.splice(rmIdx, 1);
                    saveLocalStorageArray("app_filter_history", hist);
                    renderFilterHistory();
                    return;
                }
                const fStr = history[el.dataset.idx];
                activeFilterTokens = fStr.split(" ").filter(Boolean);
                renderFilterTokens();
                filterHistoryMenu.classList.add("hidden");
                applyFilterAndSort();
            });
        });
    }

    filterHistoryBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        searchHistoryMenu.classList.add("hidden");
        renderFilterHistory();
        filterHistoryMenu.classList.toggle("hidden");
    });

    // Close history dropdown menus on outside click
    document.addEventListener("click", (e) => {
        if (!searchHistoryMenu.contains(e.target) && e.target !== searchHistoryBtn) {
            searchHistoryMenu.classList.add("hidden");
        }
        if (!filterHistoryMenu.contains(e.target) && e.target !== filterHistoryBtn) {
            filterHistoryMenu.classList.add("hidden");
        }
    });

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

    // Reset search results when input is cleared
    searchInput.addEventListener("input", () => {
        if (!searchInput.value.trim()) {
            allResults = [];
            currentResults = [];
            resultsBody.innerHTML = `<tr><td colspan="7" class="empty-state">Enter a search query to explore media items.</td></tr>`;
            resultsCount.textContent = "";
        }
    });
    
    // Focus input when clicking inside wrapper (excluding the history button)
    const searchInputWrapper = document.querySelector(".search-input-wrapper");
    if (searchInputWrapper && searchInput) {
        searchInputWrapper.addEventListener("click", (e) => {
            if (!searchHistoryBtn.contains(e.target)) {
                searchInput.focus();
            }
        });
    }

    async function performSearch() {
        const q = searchInput.value.trim();
        if (!q) return;

        saveSearchHistory(q);

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

    // ==========================================
    // Token & Parsing Utilities
    // ==========================================

    function parseBytes(val) {
        if (typeof val === 'number') return val;
        if (!val) return 0;
        const match = String(val).trim().match(/^([0-9.]+)\s*([a-zA-Z]*)$/);
        if (!match) return parseFloat(val) || 0;
        const num = parseFloat(match[1]);
        const unit = match[2].toUpperCase();
        switch (unit) {
            case 'TB': case 'T': return num * 1024 * 1024 * 1024 * 1024;
            case 'GB': case 'G': return num * 1024 * 1024 * 1024;
            case 'MB': case 'M': return num * 1024 * 1024;
            case 'KB': case 'K': return num * 1024;
            case 'B': default: return num;
        }
    }

    function parseDurationSeconds(val) {
        if (typeof val === 'number') return val;
        if (!val) return 0;
        const str = String(val).trim().toLowerCase();
        
        let total = 0;
        const matches = [...str.matchAll(/(\d+)\s*([hms])?/g)];
        if (matches.length > 0) {
            for (const m of matches) {
                const num = parseInt(m[1], 10);
                const unit = m[2] || 's';
                if (unit === 'h') total += num * 3600;
                else if (unit === 'm') total += num * 60;
                else if (unit === 's') total += num;
            }
            return total;
        }
        return parseFloat(str) || 0;
    }

    function getItemDurationSeconds(item) {
        if (typeof item.duration === 'number') return item.duration;
        if (typeof item.metadata?.duration === 'number') return item.metadata.duration;
        const durStr = item.duration_formatted || item.metadata?.duration_formatted || '';
        if (!durStr) return 0;

        const parts = durStr.split(':').map(p => parseInt(p, 10));
        if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
        if (parts.length === 2) return parts[0] * 60 + parts[1];
        return parts[0] || 0;
    }

    function getItemResolutionHeight(item) {
        const resStr = String(item.resolution || item.metadata?.resolution || item.quality || item.metadata?.quality || '');
        const match = resStr.match(/(\d{3,4})/);
        if (match) return parseInt(match[1], 10);
        return null;
    }

    function getItemSizeBytes(item) {
        if (item.size_bytes !== undefined) return item.size_bytes;
        if (item.metadata?.file_size !== undefined) return item.metadata.file_size;
        if (typeof item.size === 'number') return item.size;
        return parseBytes(item.size_human || item.metadata?.file_size_human || '');
    }

    function evaluateToken(item, token) {
        const kvMatch = token.match(/^([a-zA-Z0-9_-]+)(:|=|>=|<=|>|<)(.*)$/);
        
        if (!kvMatch) {
            const needle = token.toLowerCase();
            const tagsStr = (item.folder_tags || []).join(" ").toLowerCase();
            const haystack = `${item.normalized_title || ""} ${item.file_name || ""} ${item.mount || ""} ${tagsStr}`.toLowerCase();
            return haystack.includes(needle);
        }

        const key = kvMatch[1].toLowerCase();
        const op = kvMatch[2];
        const rawVal = kvMatch[3];

        if (key === 'tag' || key === 'tags') {
            const val = rawVal.toLowerCase();
            const tags = item.folder_tags || [];
            return tags.some(t => String(t).toLowerCase().includes(val));
        }

        if (key === 'mount') {
            return (item.mount || "").toLowerCase().includes(rawVal.toLowerCase());
        }

        if (key === 'quality' || key === 'qality' || key === 'resolution' || key === 'res') {
            const itemRes = getItemResolutionHeight(item);
            if (!itemRes) return false;

            const targetRes = parseInt(rawVal.replace(/p$/i, ''), 10);
            if (isNaN(targetRes)) {
                return (item.resolution || item.quality || "").toLowerCase().includes(rawVal.toLowerCase());
            }

            const standardResolutions = [480, 576, 720, 1080, 1440, 2160];
            const closestStandard = standardResolutions.reduce((prev, curr) => 
                Math.abs(curr - itemRes) < Math.abs(prev - itemRes) ? curr : prev
            );

            return closestStandard === targetRes || Math.abs(itemRes - targetRes) <= 120;
        }

        if (key === 'duration' || key === 'dur') {
            const itemDur = getItemDurationSeconds(item);
            const targetDur = parseDurationSeconds(rawVal);
            const activeOp = op === ':' ? '=' : op;

            switch (activeOp) {
                case '>':  return itemDur > targetDur;
                case '<':  return itemDur < targetDur;
                case '>=': return itemDur >= targetDur;
                case '<=': return itemDur <= targetDur;
                case '=':
                default:   return Math.abs(itemDur - targetDur) <= 5;
            }
        }

        if (key === 'size') {
            const itemSizeBytes = getItemSizeBytes(item);
            const targetBytes = parseBytes(rawVal);
            const activeOp = op === ':' ? '=' : op;

            switch (activeOp) {
                case '>':  return itemSizeBytes > targetBytes;
                case '<':  return itemSizeBytes < targetBytes;
                case '>=': return itemSizeBytes >= targetBytes;
                case '<=': return itemSizeBytes <= targetBytes;
                case '=':  return Math.abs(itemSizeBytes - targetBytes) <= targetBytes * 0.05;
                default:   return false;
            }
        }

        const itemPropStr = String(item[key] || item.metadata?.[key] || "").toLowerCase();
        return itemPropStr.includes(rawVal.toLowerCase());
    }

    function evaluateTokensFilter(item, tokens) {
        if (!tokens || tokens.length === 0) return true;
        return tokens.every(token => evaluateToken(item, token));
    }

    // ==========================================
    // Interactive Tag Token Input Controller
    // ==========================================

    function renderFilterTokens() {
        filterTokensList.innerHTML = activeFilterTokens.map((token, index) => `
            <div class="filter-token">
                <span>${escapeHtml(token)}</span>
                <span class="token-remove" onclick="removeFilterToken(${index})">&times;</span>
            </div>
        `).join("");
    }

    window.removeFilterToken = (index) => {
        activeFilterTokens.splice(index, 1);
        renderFilterTokens();
        applyFilterAndSort();
    };

    function addFilterToken(tokenText) {
        const trimmed = tokenText.trim();
        if (!trimmed) return;
        if (!activeFilterTokens.includes(trimmed)) {
            activeFilterTokens.push(trimmed);
            renderFilterTokens();
            saveFilterHistory(activeFilterTokens);
            applyFilterAndSort();
        }
    }

    if (filterContainer && filterInputField) {
        filterContainer.addEventListener("click", (e) => {
            if (!filterHistoryBtn.contains(e.target)) {
                filterInputField.focus();
            }
        });

        filterInputField.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                const val = filterInputField.value;
                if (val) {
                    addFilterToken(val);
                    filterInputField.value = "";
                }
            } else if (e.key === "Backspace" && filterInputField.value === "" && activeFilterTokens.length > 0) {
                activeFilterTokens.pop();
                renderFilterTokens();
                applyFilterAndSort();
            }
        });
    }

    function applyFilterAndSort() {
        let filtered = activeFilterTokens.length === 0
            ? [...allResults]
            : allResults.filter((item) => evaluateTokensFilter(item, activeFilterTokens));

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

    const FOLDER_TAG_COLORS = ["#e67e22", "#2ecc71", "#3b82f6", "#e74c3c"];

    function folderTagsHtml(item) {
        const tags = [];
        if (item.mount) {
            tags.push(item.mount);
        }
        if (Array.isArray(item.folder_tags)) {
            tags.push(...item.folder_tags);
        }

        if (!tags.length) return "";

        return `<div class="folder-tags">` + tags.map((tag, i) => {
            const color = FOLDER_TAG_COLORS[i % FOLDER_TAG_COLORS.length];
            return `<span class="folder-tag" style="border-color:${color};color:${color};">${escapeHtml(tag)}</span>`;
        }).join("") + `</div>`;
    }

    // ==========================================
    // Library Tab (breadcrumb folder/card browser)
    // ==========================================

    function libraryThumbnailUrl(entry) {
        const jellyfinId = entry.jellyfin_id;
        if (!jellyfinId) return null;
        const size = Math.max(64, CARD_SIZE_PX[cardSize] * 2);
        const params = new URLSearchParams({ jellyfin_id: jellyfinId, width: size, height: size });
        if (entry.primary_image_tag) params.set("tag", entry.primary_image_tag);
        return `/api/media/thumbnail?${params.toString()}`;
    }

    function libraryCacheKey(mount, path, sort, sortDir) {
        return `${mount}::${path}::${sort}::${sortDir}`;
    }

    function renderBreadcrumb(breadcrumb) {
        if (!breadcrumb || !breadcrumb.length) {
            libraryBreadcrumb.innerHTML = `<span class="breadcrumb-item active" data-mount="all" data-path="">All Mounts</span>`;
            return;
        }

        const parts = [`<span class="breadcrumb-item" data-mount="all" data-path="">All Mounts</span>`];
        breadcrumb.forEach((crumb, idx) => {
            const isLast = idx === breadcrumb.length - 1;
            parts.push(`<span class="breadcrumb-sep">/</span>`);
            parts.push(
                `<span class="breadcrumb-item${isLast ? " active" : ""}" data-mount="${escapeHtml(crumb.mount)}" data-path="${escapeHtml(crumb.path)}">${escapeHtml(crumb.label)}</span>`
            );
        });
        libraryBreadcrumb.innerHTML = parts.join("");
    }

    libraryBreadcrumb.addEventListener("click", (e) => {
        const target = e.target.closest(".breadcrumb-item");
        if (!target || target.classList.contains("active")) return;
        navigateLibrary(target.dataset.mount, target.dataset.path || "");
    });

    function libraryCardHtml(entry, idx) {
        const isFolder = entry.type === "folder";
        const thumbSrc = libraryThumbnailUrl(entry);
        const thumbHtml = thumbSrc
            ? `<img src="${thumbSrc}" alt="" loading="lazy" onerror="this.remove()"/>`
            : "";

        const metaBits = isFolder
            ? [`${entry.item_count}${entry.count_capped ? "+" : ""} item${entry.item_count === 1 ? "" : "s"}`]
            : [entry.resolution, entry.size_human, entry.folder_name].filter(Boolean);

        const actionsHtml = isFolder ? "" : `
                    <div class="library-card-actions">
                        <button class="library-tile-icon-btn icon-rename" data-action="rename" title="Rename">
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <path d="M12 20h9"></path>
                                <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4Z"></path>
                            </svg>
                        </button>
                        <button class="library-tile-icon-btn icon-delete" data-action="delete" title="Delete">
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <polyline points="3 6 5 6 21 6"></polyline>
                                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                                <line x1="10" y1="11" x2="10" y2="17"></line>
                                <line x1="14" y1="11" x2="14" y2="17"></line>
                            </svg>
                        </button>
                    </div>`;

        return `
            <div class="library-card ${isFolder ? "is-folder" : "is-file"}" data-idx="${idx}" title="${escapeHtml(entry.name)}">
                <div class="library-card-thumb">
                    ${thumbHtml}
                    ${isFolder ? `<span class="library-card-count-badge">${entry.item_count}${entry.count_capped ? "+" : ""}</span>` : ""}
                    ${actionsHtml}
                </div>
                <div class="library-card-body">
                    <div class="library-card-name">${escapeHtml(entry.name)}</div>
                    <div class="library-card-meta">${metaBits.map(escapeHtml).join(" \u2022 ")}</div>
                </div>
            </div>
        `;
    }

    function renderLibraryGrid(append) {
        if (!append) libraryGrid.innerHTML = "";

        if (!libraryItems.length) {
            libraryGrid.innerHTML = `<div class="library-empty">This folder is empty.</div>`;
            return;
        }

        const startIdx = append ? libraryGrid.querySelectorAll(".library-card").length : 0;
        const freshItems = append ? libraryItems.slice(startIdx) : libraryItems;
        const html = freshItems.map((entry, i) => libraryCardHtml(entry, startIdx + i)).join("");

        if (append) {
            libraryGrid.insertAdjacentHTML("beforeend", html);
        } else {
            libraryGrid.innerHTML = html;
        }
    }

    // Folders open on double-click; files open the player on a single click.
    libraryGrid.addEventListener("click", (e) => {
        const actionBtn = e.target.closest(".library-tile-icon-btn");
        if (actionBtn) {
            e.stopPropagation();
            const actionCard = actionBtn.closest(".library-card");
            const actionIdx = Number(actionCard?.dataset.idx);
            const actionEntry = libraryItems[actionIdx];
            if (!actionEntry) return;

            if (actionBtn.dataset.action === "rename") {
                openRenameModal(actionEntry, "library");
            } else if (actionBtn.dataset.action === "delete") {
                deleteLibraryEntry(actionEntry);
            }
            return;
        }

        const card = e.target.closest(".library-card");
        if (!card) return;
        const idx = Number(card.dataset.idx);
        const entry = libraryItems[idx];
        if (!entry) return;

        if (entry.type === "file") {
            const playlist = libraryItems.filter((i) => i.type === "file");
            const playIdx = playlist.indexOf(entry);
            openLibraryPlayer(playlist, playIdx);
        } else {
            libraryGrid.querySelectorAll(".library-card.selected").forEach((el) => el.classList.remove("selected"));
            card.classList.add("selected");
        }
    });

    libraryGrid.addEventListener("dblclick", (e) => {
        const card = e.target.closest(".library-card.is-folder");
        if (!card) return;
        const idx = Number(card.dataset.idx);
        const entry = libraryItems[idx];
        if (entry) navigateLibrary(entry.mount, entry.path);
    });

    function navigateLibrary(mount, path) {
        libraryMount = mount;
        libraryPath = path || "";
        loadLibrary({ reset: true });
    }

    async function loadLibrary({ reset }) {
        if (reset) {
            libraryOffset = 0;
            libraryItems = [];
        }
        if (libraryLoading) return;

        const cacheKey = libraryCacheKey(libraryMount, libraryPath, librarySort, librarySortDir);
        if (reset) {
            const cached = libraryPageCache.get(cacheKey);
            if (cached && Date.now() - cached.cachedAt < LIBRARY_CLIENT_CACHE_TTL) {
                libraryItems = cached.items;
                libraryOffset = cached.items.length;
                libraryHasMore = cached.hasMore;
                renderBreadcrumb(cached.breadcrumb);
                renderLibraryGrid(false);
                updateLibraryCount(cached.total);
                return;
            }

            // Fall back to IndexedDB so a previously-browsed folder renders
            // instantly even after a full page reload, before hitting the server.
            const idbCached = await idbGetLibraryCache(cacheKey);
            if (idbCached) {
                libraryItems = idbCached.items;
                libraryOffset = idbCached.items.length;
                libraryHasMore = idbCached.hasMore;
                renderBreadcrumb(idbCached.breadcrumb);
                renderLibraryGrid(false);
                updateLibraryCount(idbCached.total);
                libraryPageCache.set(cacheKey, { ...idbCached, cachedAt: Date.now() });
                return;
            }
        }

        libraryLoading = true;
        libraryLoadingEl.classList.remove("hidden");
        const token = ++libraryRequestToken;

        try {
            const params = new URLSearchParams({
                mount: libraryMount,
                path: libraryPath,
                offset: libraryOffset,
                limit: LIBRARY_PAGE_SIZE,
                sort: librarySort,
                sort_dir: librarySortDir,
            });
            const res = await fetch(`/api/library/browse?${params.toString()}`);
            const data = await res.json();
            if (token !== libraryRequestToken) return; // a newer navigation superseded this request
            if (!res.ok) throw new Error(data.detail || "Failed to load folder");

            libraryItems = reset ? data.items : libraryItems.concat(data.items);
            libraryOffset = libraryItems.length;
            libraryHasMore = data.has_more;

            renderBreadcrumb(data.breadcrumb);
            renderLibraryGrid(!reset);
            updateLibraryCount(data.total);

            const cacheEntry = {
                items: libraryItems,
                hasMore: libraryHasMore,
                total: data.total,
                breadcrumb: data.breadcrumb,
                cachedAt: Date.now(),
            };
            libraryPageCache.set(cacheKey, cacheEntry);
            idbSetLibraryCache(cacheKey, cacheEntry);
        } catch (err) {
            if (reset) libraryGrid.innerHTML = `<div class="library-empty" style="color:var(--danger-red)">${escapeHtml(err.message)}</div>`;
            showToast(`Library load failed: ${err.message}`, "error");
        } finally {
            libraryLoading = false;
            libraryLoadingEl.classList.add("hidden");
        }
    }

    function updateLibraryCount(total) {
        libraryCountEl.textContent = total ? `${libraryItems.length} / ${total} items` : "";
    }

    // Infinite scroll: fetch the next page only once the sentinel enters view,
    // instead of loading (or rendering) an entire huge folder up front.
    const libraryObserver = new IntersectionObserver((entries) => {
        if (entries[0].isIntersecting && libraryHasMore && !libraryLoading) {
            loadLibrary({ reset: false });
        }
    }, { rootMargin: "300px" });
    libraryObserver.observe(librarySentinel);

    librarySortSelect.addEventListener("change", () => {
        librarySort = librarySortSelect.value;
        loadLibrary({ reset: true });
    });

    librarySortDirBtn.addEventListener("click", () => {
        librarySortDir = librarySortDir === "asc" ? "desc" : "asc";
        librarySortDirBtn.dataset.dir = librarySortDir;
        librarySortDirBtn.innerHTML = librarySortDir === "asc" ? "&#8593; Asc" : "&#8595; Desc";
        loadLibrary({ reset: true });
    });

    // Reflect the restored (or default) card size in the UI immediately, so the
    // active button and grid sizing match what will actually be rendered.
    cardSizeGroup.querySelectorAll(".card-size-btn").forEach((b) => b.classList.toggle("active", b.dataset.size === cardSize));
    libraryGrid.style.setProperty("--card-size", `${CARD_SIZE_PX[cardSize]}px`);

    cardSizeGroup.addEventListener("click", (e) => {
        const btn = e.target.closest(".card-size-btn");
        if (!btn) return;
        cardSize = btn.dataset.size;
        cardSizeGroup.querySelectorAll(".card-size-btn").forEach((b) => b.classList.toggle("active", b === btn));
        libraryGrid.style.setProperty("--card-size", `${CARD_SIZE_PX[cardSize]}px`);
        localStorage.setItem("library_card_size", cardSize);
    });

    btnLibraryRefresh.addEventListener("click", async () => {
        libraryPageCache.clear();
        await idbClearLibraryCache();
        loadLibrary({ reset: true });
    });

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

    scanModeToggle.addEventListener("click", () => {
        const isIncremental = scanModeToggle.dataset.mode === "incremental";
        const newMode = isIncremental ? "rescan" : "incremental";

        scanModeToggle.dataset.mode = newMode;
        scanModeLabel.textContent = newMode === "rescan" ? "Full Re-scan" : "Incremental";
        scanModeLabel.style.color = newMode === "rescan" ? "#ce9178" : "#4ec9b0";
    });

    btnScan.addEventListener("click", async () => {
        const selectedMount = mountSelect.value;
        const scanMode = scanModeToggle.dataset.mode;
        const rescanDisk = scanMode === "rescan";
        const incrementalScan = scanMode === "incremental";

        const targetMounts = selectedMount === "all" ? [...availableMounts] : [selectedMount];

        scanConsole.classList.remove("hidden");

        if (targetMounts.length === 0) {
            setMountStatus("config", "No mounts are enabled in config.yml", 0, 0);
            return;
        }

        btnScan.disabled = true;

        for (const mount of targetMounts) {
            setMountStatus(mount, `Requesting scan start (mode=${scanMode})...`, 0, 0);

            try {
                const response = await fetch(
                    `/api/scan/start?mount_name=${encodeURIComponent(mount)}&rescan_disk=${rescanDisk}&incremental_scan=${incrementalScan}`,
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
        const jellyfinId = item.jellyfin?.jellyfin_id || item.jellyfin?.jf_id || item.jellyfin_id;
        if (jellyfinId) {
            return `/api/media/jellyfin/stream?jellyfin_id=${encodeURIComponent(jellyfinId)}`;
        }
        return `/api/media/stream?path=${encodeURIComponent(item.file_path)}`;
    }

    // ---- Playback queue: playlist, shuffle, loop, next/prev ----
    const playerQueueInfo = document.getElementById("player-queue-info");
    const playerShuffleBtn = document.getElementById("player-shuffle");
    const playerLoopBtn = document.getElementById("player-loop");
    const playerPrevBtn = document.getElementById("player-prev");
    const playerNextBtn = document.getElementById("player-next");

    let currentPlaylist = [];
    let playOrder = [];
    let orderPos = -1;
    let shuffleOn = false;
    let loopMode = "off"; // off | all | one

    function buildPlayOrder(playlist, startIndex, shuffle) {
        const indices = playlist.map((_, i) => i);
        if (!shuffle) return indices;
        const rest = indices.filter((i) => i !== startIndex);
        for (let i = rest.length - 1; i > 0; i--) {
            const j = Math.floor(Math.random() * (i + 1));
            [rest[i], rest[j]] = [rest[j], rest[i]];
        }
        return [startIndex, ...rest];
    }

    function updateQueueInfo() {
        playerQueueInfo.textContent = currentPlaylist.length > 1
            ? `${orderPos + 1} / ${playOrder.length}`
            : "";
    }

    function renderPlayerItem(item) {
        playerTitle.innerText = item.normalized_title || item.file_name || item.name;
        const resolution = item.resolution || item.metadata?.resolution || "";
        const sizeHuman = item.size_human || item.metadata?.file_size_human || "";
        playerMeta.innerText = [resolution, sizeHuman].filter(Boolean).join(" \u2022 ");
        playerVideo.src = streamUrl(item);
        playerOverlay.classList.remove("hidden");
        playerVideo.volume = Number(playerVolume.value);
        playerVideo.play().catch(() => {});
        updateQueueInfo();
    }

    function playCurrentTrack() {
        const item = currentPlaylist[playOrder[orderPos]];
        if (item) renderPlayerItem(item);
    }

    // Single-item playback (used by the search tab's "Play" button).
    function openPlayer(item, playlist, index) {
        currentPlaylist = playlist || [item];
        const startIdx = index !== undefined ? index : Math.max(0, currentPlaylist.indexOf(item));
        playOrder = buildPlayOrder(currentPlaylist, startIdx, shuffleOn);
        orderPos = 0;
        playCurrentTrack();
    }

    // Playback from a Library folder: builds the queue from every file currently
    // shown in that folder so Next/Prev/Shuffle/Loop can move between them.
    function openLibraryPlayer(playlist, index) {
        openPlayer(playlist[index], playlist, index);
    }

    function nextTrack() {
        if (!currentPlaylist.length) return;
        if (orderPos < playOrder.length - 1) {
            orderPos++;
        } else if (loopMode === "all") {
            orderPos = 0;
        } else {
            return;
        }
        playCurrentTrack();
    }

    function prevTrack() {
        if (!currentPlaylist.length) return;
        if (orderPos > 0) {
            orderPos--;
        } else if (loopMode === "all") {
            orderPos = playOrder.length - 1;
        }
        playCurrentTrack();
    }

    playerShuffleBtn.addEventListener("click", () => {
        shuffleOn = !shuffleOn;
        playerShuffleBtn.classList.toggle("active", shuffleOn);
        if (currentPlaylist.length > 1) {
            const currentItem = currentPlaylist[playOrder[orderPos]];
            const currentIdx = currentPlaylist.indexOf(currentItem);
            playOrder = buildPlayOrder(currentPlaylist, currentIdx, shuffleOn);
            orderPos = 0;
            updateQueueInfo();
        }
    });

    playerLoopBtn.addEventListener("click", () => {
        loopMode = loopMode === "off" ? "all" : loopMode === "all" ? "one" : "off";
        playerLoopBtn.dataset.mode = loopMode;
        playerLoopBtn.title = `Loop: ${loopMode}`;
        playerLoopBtn.classList.toggle("active", loopMode !== "off");
        playerLoopBtn.innerHTML = loopMode === "one" ? "&#128257;<sub>1</sub>" : "&#128257;";
    });

    playerPrevBtn.addEventListener("click", prevTrack);
    playerNextBtn.addEventListener("click", nextTrack);

    playerVideo.addEventListener("ended", () => {
        if (loopMode === "one") {
            playerVideo.currentTime = 0;
            playerVideo.play().catch(() => {});
            return;
        }
        nextTrack();
    });

    function closePlayer() {
        if (document.fullscreenElement) document.exitFullscreen?.();
        playerVideo.pause();
        playerVideo.removeAttribute("src");
        playerVideo.load();
        playerOverlay.classList.add("hidden");
        currentPlaylist = [];
        playOrder = [];
        orderPos = -1;
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
        playerVideo.muted = playerVolume.value === 0;
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

    function renderResults(items) {
        currentResults = items || [];

        if (currentResults.length === 0) {
            resultsBody.innerHTML = `<tr><td colspan="7" class="empty-state">No semantic matches found.</td></tr>`;
            return;
        }

        resultsBody.innerHTML = currentResults.map((item, idx) => {
            const vectorId = item.id || item.vector_id || "N/A";
            const mysqlId = item.mysql_id || item.db_id || item.file_id || "N/A";

            return `
            <tr>
                <td class="col-thumb">
                    <img class="thumb-img" src="${thumbnailUrl(item)}" alt="thumb" loading="lazy" onerror="this.src='${THUMB_PLACEHOLDER}'"/>
                </td>
                <td class="col-details">
                    <div class="file-title" title="${escapeHtml(item.normalized_title)}">${escapeHtml(item.normalized_title)}</div>
                    <small class="file-name" title="${escapeHtml(item.file_name)}">${escapeHtml(item.file_name)}</small>
                    ${folderTagsHtml(item)}
                    
                    <!-- DB Identifiers with Inline Icon Button -->
                    <div class="db-identifiers" style="margin-top: 6px; font-size: 11px; color: #888; display: flex; align-items: center; gap: 6px;">
                        <span><strong>Vector ID:</strong> <code>${escapeHtml(vectorId)}</code></span> | 
                        <span><strong>MySQL ID:</strong> <code>${escapeHtml(mysqlId)}</code></span>
                        
                        <button class="btn-icon-clean" onclick="cleanRecord(${idx})" title="Clean index (Remove from DBs, keep disk file)">
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <polyline points="3 6 5 6 21 6"></polyline>
                                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                                <line x1="10" y1="11" x2="10" y2="17"></line>
                                <line x1="14" y1="11" x2="14" y2="17"></line>
                            </svg>
                        </button>
                    </div>
                </td>
                <!-- REMOVED: <td class="col-mount">${escapeHtml(item.mount)}</td> -->
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
            `;
        }).join("");
    }

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
    let renameSource = "search"; // "search" | "library" — controls which view refreshes after a rename

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

    function suggestName(item) {
        const [base, ext] = splitExtension(item.file_name || "");
        if ((item.mount || "").toLowerCase() === "movies") {
            const folder = tidy(parentFolderName(item.file_path));
            if (folder) return `${folder}${ext}`;
        }
        return `${tidy(base)}${ext}`;
    }

    function openRenameModal(item, source = "search") {
        renameTarget = item;
        renameSource = source;
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
            if (renameSource === "library") {
                loadLibrary({ reset: true });
            } else {
                performSearch();
            }
        } catch (err) {
            showToast(`Rename failed: ${err.message}`, "error", 8000);
        }
    }

    window.renameMedia = (index) => {
        const item = currentResults[index];
        if (item) openRenameModal(item, "search");
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

    async function deleteLibraryEntry(item) {
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
            loadLibrary({ reset: true });
        } catch (err) {
            showToast(`Delete failed: ${err.message}`, "error", 8000);
        }
    }

    window.cleanRecord = async (index) => {
        const item = currentResults[index];
        if (!item) return;

        const ok = await askConfirm({
            title: "Clean index records",
            message: `Remove database records from Qdrant and MySQL?<br/><br/>
                      <strong>${escapeHtml(item.file_name)}</strong><br/><br/>
                      <small style="color:#aaa;">Note: The physical file on disk will NOT be deleted.</small>`,
            okLabel: "Clean Index"
        });
        if (!ok) return;

        try {
            const res = await fetch(`/api/actions/clean-record?path=${encodeURIComponent(item.file_path)}`, { 
                method: "DELETE" 
            });
            const data = await res.json().catch(() => ({}));
            
            if (!res.ok) {
                showToast(`Cleaning failed: ${data.detail || res.statusText}`, "error", 8000);
                return;
            }
            
            showToast(`Removed DB records for ${item.file_name}. Disk file preserved.`, "success");
            performSearch();
        } catch (err) {
            showToast(`Clean operation failed: ${err.message}`, "error", 8000);
        }
    };

    // Navigation Tab Switcher
    const tabBtnSearch = document.getElementById("tab-btn-search");
    const tabBtnLibrary = document.getElementById("tab-btn-library");
    const tabBtnDownloads = document.getElementById("tab-btn-downloads");
    const tabPanelSearch = document.getElementById("tab-panel-search");
    const tabPanelLibrary = document.getElementById("tab-panel-library");
    const tabPanelDownloads = document.getElementById("tab-panel-downloads");

    const allTabBtns = [tabBtnSearch, tabBtnLibrary, tabBtnDownloads];
    const allTabPanels = [tabPanelSearch, tabPanelLibrary, tabPanelDownloads];

    function activateTab(btn, panel) {
        allTabBtns.forEach((b) => b?.classList.toggle("active", b === btn));
        allTabPanels.forEach((p) => p?.classList.toggle("active", p === panel));
    }

    tabBtnSearch?.addEventListener("click", () => activateTab(tabBtnSearch, tabPanelSearch));

    let libraryLoadedOnce = false;
    function ensureLibraryLoaded() {
        if (!libraryLoadedOnce) {
            libraryLoadedOnce = true;
            loadLibrary({ reset: true });
        }
    }

    tabBtnLibrary?.addEventListener("click", () => {
        activateTab(tabBtnLibrary, tabPanelLibrary);
        ensureLibraryLoaded();
    });

    tabBtnDownloads?.addEventListener("click", () => {
        activateTab(tabBtnDownloads, tabPanelDownloads);
        fetchDownloads();
    });

    // Library is the default active tab on page load (see index.html), so the
    // click handler above never fires on its own — load its data now too.
    if (tabPanelLibrary?.classList.contains("active")) {
        ensureLibraryLoaded();
    }

    // Download API Handlers
    async function fetchDownloads() {
        try {
            const res = await fetch("/api/actions/downloads");
            if (!res.ok) throw new Error("Failed to load downloads");
            downloadsList = await res.json();
            applyDownloadsFilterAndSort();
        } catch (err) {
            showToast(`Failed to load downloads: ${err.message}`, "error");
        }
    }

    function applyDownloadsFilterAndSort() {
        let items = downloadStatusFilter === "all"
            ? [...downloadsList]
            : downloadsList.filter(item => (item.status || "").toLowerCase() === downloadStatusFilter);

        items.sort((a, b) => {
            let va = a[downloadSortField] ?? "";
            let vb = b[downloadSortField] ?? "";
            if (typeof va === "string") {
                const cmp = va.localeCompare(String(vb));
                return downloadSortDir === "asc" ? cmp : -cmp;
            }
            return downloadSortDir === "asc" ? va - vb : vb - va;
        });

        renderDownloads(items);
    }

    function renderDownloads(items) {
        const downloadsBody = document.getElementById("downloads-body");
        const downloadsCount = document.getElementById("downloads-count");

        if (!downloadsBody) return; // Exit safely if the downloads tab isn't active/loaded

        if (!items.length) {
            downloadsBody.innerHTML = `<tr><td colspan="7" class="empty-state">No matching download tasks.</td></tr>`;
            if (downloadsCount) downloadsCount.textContent = "";
            return;
        }

        if (downloadsCount) {
            downloadsCount.textContent = `${items.length} / ${downloadsList.length} items`;
        }
        
        downloadsBody.innerHTML = items.map((item, idx) => `
            <tr>
                <td>${idx + 1}</td>
                <td class="col-details">
                    <div class="file-title" title="${escapeHtml(item.url)}">${escapeHtml(item.url)}</div>
                    <small class="file-name" title="Language: ${escapeHtml(item.language || 'N/A')}">Lang: ${escapeHtml(item.language || 'N/A')}</small>
                </td>
                <td><code>${escapeHtml(item.quality || 'N/A')}</code></td>
                <td><strong>${escapeHtml(item.title || 'N/A')}</strong></td>
                <td><span class="status-badge ${escapeHtml((item.status || 'pending').toLowerCase())}">${escapeHtml(item.status || 'Pending')}</span></td>
                <td><small>${escapeHtml(item.created_at ? new Date(item.created_at).toLocaleString() : 'N/A')}</small></td>
                <td class="col-actions">
                    <div class="row-actions" style="display: flex; align-items: center; gap: 6px;">
                        <button class="btn-icon-clean" onclick="retryDownload('${escapeHtml(item.id)}')" title="Retry download (re-add entry and set pending)">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <polyline points="23 4 23 10 17 10"></polyline>
                                <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path>
                            </svg>
                        </button>
                        ${item.status !== 'COMPLETED' && item.status !== 'completed' ? `<button class="btn btn-secondary" onclick="markDownloadComplete('${escapeHtml(item.id)}')">Mark Complete</button>` : ''}
                        <button class="btn btn-danger" onclick="deleteDownload('${escapeHtml(item.id)}')">Delete</button>
                    </div>
                </td>
            </tr>
        `).join("");
    }

    // Attach event listener for the refresh button
    document.getElementById("btn-refresh-downloads")?.addEventListener("click", () => {
        fetchDownloads();
    });

    window.retryDownload = async (entryString) => {
        try {
            const res = await fetch("/api/ytdlp/download-entry", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ entry: entryString })
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || "Failed to retry download");

            // Force status back to PENDING if necessary
            await fetch("/api/ytdlp/update-status", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ entry: entryString, status: "PENDING" })
            });

            showToast("Download entry re-queued as PENDING.", "success");
            fetchDownloads();
        } catch (err) {
            showToast(`Retry failed: ${err.message}`, "error");
        }
    };

    // Download Table Controls
    document.getElementById("downloads-status-filter")?.addEventListener("change", (e) => {
        downloadStatusFilter = e.target.value;
        applyDownloadsFilterAndSort();
    });

    document.getElementById("downloads-sort")?.addEventListener("change", (e) => {
        downloadSortField = e.target.value;
        applyDownloadsFilterAndSort();
    });

    document.getElementById("downloads-sort-dir")?.addEventListener("click", (e) => {
        downloadSortDir = downloadSortDir === "asc" ? "desc" : "asc";
        e.target.innerHTML = downloadSortDir === "asc" ? "&#8593; Asc" : "&#8595; Desc";
        applyDownloadsFilterAndSort();
    });

    window.markDownloadComplete = async (id) => {
        try {
            const res = await fetch(`/api/actions/downloads/${id}`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ status: "completed" })
            });
            if (!res.ok) throw new Error("Failed to update status");
            showToast("Task marked as completed.", "success");
            fetchDownloads();
        } catch (err) {
            showToast(`Update failed: ${err.message}`, "error");
        }
    };

    window.deleteDownload = async (id) => {
        const ok = await askConfirm({
            title: "Delete download record",
            message: "Are you sure you want to delete this download entry from history?",
            okLabel: "Delete"
        });
        if (!ok) return;

        try {
            const res = await fetch("/api/actions/downloads/delete", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ entry: id })
            });
            if (!res.ok) throw new Error("Failed to delete entry");
            showToast("Download record removed.", "success");
            fetchDownloads();
        } catch (err) {
            showToast(`Delete failed: ${err.message}`, "error");
        }
    };
});