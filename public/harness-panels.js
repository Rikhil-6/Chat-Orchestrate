(() => {
  const staleRailId = "chat-orchestrate-panel-rail";
  const staleRailStyleId = "chat-orchestrate-panel-rail-style";
  const styleId = "chat-orchestrate-sidebar-polish-style";
  const polishVersion = "sidebar-polish-17";

  if (window.__chatOrchestrateSidebarPolishVersion === polishVersion) return;
  window.__chatOrchestrateSidebarPolishVersion = polishVersion;

  document.getElementById(staleRailId)?.remove();
  document.getElementById(staleRailStyleId)?.remove();
  document.getElementById(styleId)?.remove();

  function isVisible(element) {
    if (!element) return false;
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function addStyle() {
    if (document.getElementById(styleId)) return;
    const style = document.createElement("style");
    style.id = styleId;
    style.textContent = `
      .chat-orchestrate-sidebar-close {
        width: 32px !important;
        height: 32px !important;
        min-width: 32px !important;
        border-radius: 8px !important;
        display: inline-grid !important;
        place-items: center !important;
      }
      .chat-orchestrate-sidebar-close svg {
        width: 18px;
        height: 18px;
        stroke-width: 2.2;
      }
      .chat-orchestrate-dashboard-pinned-close {
        display: none !important;
        pointer-events: none !important;
      }
      div[role="dialog"]:has([data-harness-dashboard="true"]) > button.absolute.right-4.top-4,
      div[role="dialog"]:has([data-harness-dashboard="true"]) > button[aria-label="Close"],
      div[role="dialog"]:has([data-harness-dashboard="true"]) > button:has(svg) {
        display: none !important;
        pointer-events: none !important;
      }
    `;
    document.head.appendChild(style);
  }

  function renameSidebarTitle() {
    if (!document.body) return;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      if (walker.currentNode.nodeValue.trim() === "HarnessDashboard") {
        walker.currentNode.nodeValue = walker.currentNode.nodeValue.replace("HarnessDashboard", "Harness Dashboard");
      }
    }
  }

  function settingsDialog() {
    return Array.from(document.querySelectorAll("[role='dialog']")).find((dialog) => {
      return (dialog.textContent || "").trim().startsWith("Settings panel");
    });
  }

  function selectedAgent(dialog) {
    const combobox = dialog?.querySelector("[role='combobox']");
    return (combobox?.textContent || "").trim();
  }

  function replaceText(dialog, candidates, replacement) {
    if (!dialog || !replacement) return;
    const walker = document.createTreeWalker(dialog, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const value = walker.currentNode.nodeValue.trim();
      if (candidates.includes(value)) {
        walker.currentNode.nodeValue = walker.currentNode.nodeValue.replace(value, replacement);
      }
    }
  }

  function polishSettingsFields() {
    const dialog = settingsDialog();
    const agent = selectedAgent(dialog);
    const apiLabels = ["OpenAI API Key", "Claude API Key", "Gemini API Key"];
    const commandLabels = ["Codex Command", "Claude Command", "Claude Code Command", "Gemini CLI Command"];
    const textboxes = dialog ? Array.from(dialog.querySelectorAll("input, textarea")) : [];
    const apiInput = textboxes[0];
    const commandInput = textboxes[1];

    if (agent === "claude-code") {
      replaceText(dialog, apiLabels, "Claude API Key");
      replaceText(dialog, commandLabels, "Claude Code Command");
      apiInput?.setAttribute("placeholder", "Saved locally; optional for Claude SDK/API flows");
      commandInput?.setAttribute("placeholder", "claude, claude.cmd, or full path");
    } else if (agent === "gemini-cli") {
      replaceText(dialog, apiLabels, "Gemini API Key");
      replaceText(dialog, commandLabels, "Gemini CLI Command");
      apiInput?.setAttribute("placeholder", "Saved locally; optional for Gemini API flows");
      commandInput?.setAttribute("placeholder", "gemini, gemini.cmd, or full path");
    } else if (agent === "codex") {
      replaceText(dialog, apiLabels, "OpenAI API Key");
      replaceText(dialog, commandLabels, "Codex Command");
      apiInput?.setAttribute("placeholder", "Saved locally; used for Codex API fallback");
      commandInput?.setAttribute("placeholder", "codex, codex.cmd, or full path");
    }
  }

  function dashboardDialog() {
    const dashboard = document.querySelector("[data-harness-dashboard='true']");
    return dashboard?.closest("[role='dialog']") || dashboard?.closest("[data-panel]");
  }

  function isSidebarHeaderButton(button, dialog) {
    if (!isVisible(button)) return false;
    if (!dialog || !dialog.contains(button) || !isVisible(dialog)) return false;

    const rect = button.getBoundingClientRect();
    const dialogRect = dialog.getBoundingClientRect();
    const text = (button.textContent || "").trim().toLowerCase();
    const nearHeader = rect.top <= dialogRect.top + 64;
    const nearLeft = rect.left <= dialogRect.left + 72;
    const nearRight = rect.right >= dialogRect.right - 72;
    const compact = rect.width <= 48 && rect.height <= 48;
    const iconLike = button.querySelector("svg") || text === "close" || text === "back";

    return nearHeader && compact && iconLike && (nearLeft || nearRight);
  }

  function polishSidebarCloseButtons() {
    const dashboard = dashboardDialog();
    const closeIcon = `
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
        <path d="M6 6l12 12M18 6 6 18" stroke-linecap="round"></path>
      </svg>
    `;

    for (const button of document.querySelectorAll("button")) {
      const dialog = button.closest("[role='dialog']");
      if (dashboard && dialog === dashboard && (button.textContent || "").trim().toLowerCase() === "close") {
        button.classList.add("chat-orchestrate-dashboard-pinned-close");
        button.setAttribute("aria-hidden", "true");
        button.tabIndex = -1;
        button.onclick = (event) => event.preventDefault();
        continue;
      }
      if (!isSidebarHeaderButton(button, dialog)) continue;
      if (dashboard && dashboard.contains(button)) {
        button.classList.add("chat-orchestrate-dashboard-pinned-close");
        button.setAttribute("aria-hidden", "true");
        button.tabIndex = -1;
        button.onclick = (event) => event.preventDefault();
        continue;
      }
      button.classList.add("chat-orchestrate-sidebar-close");
      button.setAttribute("aria-label", "Close sidebar");
      button.title = "Close sidebar";
      if (button.innerHTML.trim() !== closeIcon.trim()) {
        button.innerHTML = closeIcon;
      }
    }
  }

  function polish() {
    addStyle();
    document.getElementById(staleRailId)?.remove();
    renameSidebarTitle();
    polishSidebarCloseButtons();
    polishSettingsFields();
  }

  polish();
  window.addEventListener("load", polish);
  new MutationObserver(polish).observe(document.documentElement, { childList: true, subtree: true });
})();
