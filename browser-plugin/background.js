const TOGGLE_MESSAGE = "media-indexer-toggle";

async function togglePanel(tab) {
    if (!tab?.id || !/^https?:/.test(tab.url || "")) return;

    try {
        await chrome.tabs.sendMessage(tab.id, { type: TOGGLE_MESSAGE });
    } catch {
        // Content script not present yet (e.g. tab loaded before install) - inject then retry
        await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
        await chrome.tabs.sendMessage(tab.id, { type: TOGGLE_MESSAGE });
    }
}

chrome.action.onClicked.addListener(togglePanel);

chrome.commands.onCommand.addListener(async (command) => {
    if (command !== "_execute_action") return;
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    togglePanel(tab);
});
