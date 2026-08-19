const HOST_ID = "media-indexer-companion-host";
const TOGGLE_MESSAGE = "media-indexer-toggle";
const MIN_WIDTH = 320;
const MIN_HEIGHT = 260;
const EDGE_MARGIN = 16;

if (!window.__mediaIndexerCompanionLoaded) {
    window.__mediaIndexerCompanionLoaded = true;
    chrome.runtime.onMessage.addListener((message) => {
        if (message?.type === TOGGLE_MESSAGE) togglePanel();
    });
}

function togglePanel() {
    const existing = document.getElementById(HOST_ID);
    if (existing) {
        existing.remove();
        return;
    }
    document.documentElement.appendChild(buildPanel());
}

function buildPanel() {
    const host = document.createElement("div");
    host.id = HOST_ID;
    // Isolated from the page's CSS so the card keeps the popup layout everywhere
    const shadow = host.attachShadow({ mode: "open" });

    const style = document.createElement("style");
    style.textContent = panelStyles();
    shadow.appendChild(style);

    const card = document.createElement("div");
    card.className = "mi-card";

    const bar = document.createElement("div");
    bar.className = "mi-bar";

    const title = document.createElement("span");
    title.className = "mi-title";
    title.textContent = "Media Indexer Companion";

    const closeBtn = document.createElement("button");
    closeBtn.className = "mi-close";
    closeBtn.type = "button";
    closeBtn.title = "Close";
    closeBtn.textContent = "\u00d7";
    closeBtn.addEventListener("click", () => host.remove());

    bar.append(title, closeBtn);

    const frame = document.createElement("iframe");
    frame.className = "mi-frame";
    frame.src = chrome.runtime.getURL("popup.html");
    frame.setAttribute("allow", "clipboard-write");

    card.append(bar, frame);
    shadow.appendChild(card);

    enableDrag(bar, card, frame);
    return host;
}

function enableDrag(handle, card, frame) {
    handle.addEventListener("pointerdown", (event) => {
        if (event.button !== 0 || event.target.classList.contains("mi-close")) return;

        const rect = card.getBoundingClientRect();
        const offsetX = event.clientX - rect.left;
        const offsetY = event.clientY - rect.top;

        // Switch from the bottom/right anchoring to absolute coordinates while dragging
        card.style.left = `${rect.left}px`;
        card.style.top = `${rect.top}px`;
        card.style.right = "auto";
        card.style.bottom = "auto";
        // The iframe would otherwise swallow the pointer stream mid-drag
        frame.style.pointerEvents = "none";
        handle.setPointerCapture(event.pointerId);

        const onMove = (move) => {
            const maxLeft = window.innerWidth - card.offsetWidth - EDGE_MARGIN;
            const maxTop = window.innerHeight - card.offsetHeight - EDGE_MARGIN;
            card.style.left = `${clamp(move.clientX - offsetX, EDGE_MARGIN, Math.max(EDGE_MARGIN, maxLeft))}px`;
            card.style.top = `${clamp(move.clientY - offsetY, EDGE_MARGIN, Math.max(EDGE_MARGIN, maxTop))}px`;
        };

        const onUp = () => {
            frame.style.pointerEvents = "";
            handle.removeEventListener("pointermove", onMove);
            handle.removeEventListener("pointerup", onUp);
            handle.removeEventListener("pointercancel", onUp);
        };

        handle.addEventListener("pointermove", onMove);
        handle.addEventListener("pointerup", onUp);
        handle.addEventListener("pointercancel", onUp);
        event.preventDefault();
    });
}

function clamp(value, min, max) {
    return Math.min(Math.max(value, min), max);
}

function panelStyles() {
    return `
        :host { all: initial; }

        .mi-card {
            position: fixed;
            right: ${EDGE_MARGIN}px;
            bottom: ${EDGE_MARGIN}px;
            width: 25vw;
            height: 80vh;
            min-width: ${MIN_WIDTH}px;
            min-height: ${MIN_HEIGHT}px;
            max-width: 96vw;
            max-height: 96vh;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            resize: both;
            background: #1e1e1e;
            border: 1px solid #333;
            border-radius: 6px;
            box-shadow: 0 12px 40px rgba(0, 0, 0, 0.55);
            z-index: 2147483647;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }

        .mi-bar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 8px;
            padding: 6px 10px;
            background: #252526;
            border-bottom: 1px solid #333;
            cursor: move;
            user-select: none;
            touch-action: none;
        }

        .mi-title { color: #cccccc; font-size: 12px; font-weight: 600; }

        .mi-close {
            border: none;
            background: transparent;
            color: #cccccc;
            font-size: 16px;
            line-height: 1;
            padding: 2px 6px;
            cursor: pointer;
            border-radius: 3px;
        }
        .mi-close:hover { background: #3c3c3c; color: #ffffff; }

        .mi-frame {
            flex: 1;
            width: 100%;
            border: none;
            display: block;
            background: #1e1e1e;
        }
    `;
}
