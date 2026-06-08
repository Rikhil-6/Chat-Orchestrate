(() => {
  const railId = "chat-orchestrate-panel-rail";
  const styleId = "chat-orchestrate-panel-rail-style";
  const railVersion = "panel-rail-10";
  const desiredVisibility = {
    dashboard: null,
    settings: null,
  };

  if (window.__chatOrchestratePanelRailVersion === railVersion) return;
  window.__chatOrchestratePanelRailVersion = railVersion;
  document.getElementById(railId)?.remove();
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
      #${railId} {
        position: fixed;
        z-index: 2147483000;
        right: 148px;
        top: 10px;
        display: flex;
        gap: 4px;
        border: 1px solid color-mix(in srgb, currentColor 14%, transparent);
        border-radius: 12px;
        padding: 4px;
        background: color-mix(in srgb, Canvas 88%, transparent);
        box-shadow: 0 8px 24px color-mix(in srgb, black 16%, transparent);
        backdrop-filter: blur(14px);
        pointer-events: auto;
      }
      #${railId} button {
        width: 34px;
        height: 34px;
        border: 0;
        border-radius: 9px;
        padding: 0;
        background: transparent;
        color: CanvasText;
        cursor: pointer;
        display: grid;
        place-items: center;
        opacity: 0.78;
        transition: background 120ms ease, color 120ms ease, opacity 120ms ease;
      }
      #${railId} svg {
        width: 17px;
        height: 17px;
        stroke-width: 2.15;
      }
      #${railId} .chat-orchestrate-rail-label {
        position: absolute;
        width: 1px;
        height: 1px;
        margin: -1px;
        padding: 0;
        overflow: hidden;
        clip: rect(0, 0, 0, 0);
        white-space: nowrap;
        border: 0;
      }
      #${railId} button:hover {
        background: color-mix(in srgb, currentColor 9%, transparent);
        opacity: 1;
      }
      #${railId} button[aria-pressed="true"] {
        background: #ff0f68;
        color: white;
        opacity: 1;
      }
      .chat-orchestrate-panel-hidden {
        display: none !important;
        pointer-events: none !important;
        visibility: hidden !important;
      }
      @media (max-width: 720px) {
        #${railId} {
          right: 12px;
          top: 52px;
        }
        #${railId} button {
          width: 34px;
        }
      }
    `;
    document.head.appendChild(style);
  }

  function findTextButton(text) {
    return Array.from(document.querySelectorAll("button")).find((button) => {
      return (
        isVisible(button) &&
        !button.closest(`#${railId}`) &&
        (button.textContent || "").trim().toLowerCase() === text.toLowerCase()
      );
    });
  }

  function visibleDialogs() {
    return Array.from(document.querySelectorAll("[role='dialog']")).filter((dialog) => {
      return isVisible(dialog) && !dialog.classList.contains("chat-orchestrate-panel-hidden");
    });
  }

  function dashboardDialog(includeHidden = false) {
    const dialog = document.querySelector("[data-harness-dashboard='true']")?.closest("[role='dialog']");
    if (!dialog) return null;
    return includeHidden || isVisible(dialog) ? dialog : null;
  }

  function settingsDialog(includeHidden = false) {
    const dialogs = includeHidden ? Array.from(document.querySelectorAll("[role='dialog']")) : visibleDialogs();
    return dialogs.find((dialog) => {
      return (dialog.textContent || "").trim().startsWith("Settings panel");
    });
  }

  function softHideDialog(dialog) {
    if (!dialog) return false;
    dialog.classList.add("chat-orchestrate-panel-hidden");
    return true;
  }

  function showDialog(dialog) {
    if (!dialog) return false;
    dialog.classList.remove("chat-orchestrate-panel-hidden");
    return true;
  }

  function closeDialog(dialog) {
    return softHideDialog(dialog);
  }

  function submitCommand(command) {
    const textarea = document.querySelector("textarea[placeholder*='message'], textarea");
    if (!textarea) return false;
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value")?.set;
    if (setter) setter.call(textarea, command);
    else textarea.value = command;
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    textarea.dispatchEvent(new Event("change", { bubbles: true }));
    textarea.focus();

    const textRect = textarea.getBoundingClientRect();
    const send = Array.from(document.querySelectorAll("button"))
      .filter(isVisible)
      .filter((button) => {
        const rect = button.getBoundingClientRect();
        return rect.top >= textRect.top - 24 && rect.top <= textRect.bottom + 36 && rect.left > textRect.right - 96;
      })
      .sort((a, b) => b.getBoundingClientRect().left - a.getBoundingClientRect().left)[0];
    if (send) {
      send.click();
      return true;
    }
    textarea.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "Enter", code: "Enter" }));
    return true;
  }

  function panelDialog(panel, includeHidden = false) {
    if (panel === "dashboard") return dashboardDialog(includeHidden);
    return settingsDialog(includeHidden);
  }

  function requestPanelOpen(panel) {
    if (panel === "dashboard") {
      const dashboardButton = findTextButton("Dashboard");
      if (dashboardButton) {
        dashboardButton.click();
        return;
      }
      submitCommand("/dashboard");
      return;
    }
    const topButtons = Array.from(document.querySelectorAll("button"))
      .filter(isVisible)
      .map((button) => ({ button, rect: button.getBoundingClientRect(), text: (button.textContent || "").trim() }))
      .filter((item) => item.rect.top < 72);
    const theme = topButtons.find((item) => item.text === "Toggle theme");
    const iconButtons = topButtons
      .filter((item) => !item.text && item.rect.width <= 48 && item.rect.height <= 48 && item.rect.left > 54)
      .sort((a, b) => a.rect.left - b.rect.left);
    const settings = theme
      ? iconButtons.filter((item) => item.rect.left < theme.rect.left).pop()
      : iconButtons.at(-1);
    settings?.button.click();
  }

  function enforceDesiredState(panel) {
    const desired = desiredVisibility[panel];
    if (desired === null) return;
    const dialog = panelDialog(panel, true);
    if (desired && dialog) showDialog(dialog);
    if (!desired && dialog) closeDialog(dialog);
  }

  function togglePanel(panel) {
    const actualVisible = Boolean(panelDialog(panel));
    const current = desiredVisibility[panel] === null ? actualVisible : desiredVisibility[panel];
    const next = !current;
    desiredVisibility[panel] = next;

    const dialog = panelDialog(panel, true);
    if (!next) {
      if (dialog) closeDialog(dialog);
      updateRailState();
      return;
    }
    if (dialog) showDialog(dialog);
    else requestPanelOpen(panel);

    setTimeout(() => enforceDesiredState(panel), 120);
    setTimeout(() => enforceDesiredState(panel), 500);
    setTimeout(updateRailState, 650);
  }

  function updateRailState() {
    const rail = document.getElementById(railId);
    if (!rail) return;
    const dashboard = rail.querySelector("[data-panel='dashboard']");
    const settings = rail.querySelector("[data-panel='settings']");
    dashboard?.setAttribute("aria-pressed", dashboardDialog() || desiredVisibility.dashboard === true ? "true" : "false");
    settings?.setAttribute("aria-pressed", settingsDialog() || desiredVisibility.settings === true ? "true" : "false");
  }

  function bindRailButton(button, callback) {
    button.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      event.stopPropagation();
      callback();
    });
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
    });
  }

  function ensureRail() {
    addStyle();
    if (document.getElementById(railId)) return;
    const rail = document.createElement("div");
    rail.id = railId;

    const dashboard = document.createElement("button");
    dashboard.type = "button";
    dashboard.dataset.panel = "dashboard";
    dashboard.innerHTML = `
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
        <rect x="3" y="3" width="7" height="7" rx="1.5"></rect>
        <rect x="14" y="3" width="7" height="7" rx="1.5"></rect>
        <rect x="3" y="14" width="7" height="7" rx="1.5"></rect>
        <rect x="14" y="14" width="7" height="7" rx="1.5"></rect>
      </svg>
      <span class="chat-orchestrate-rail-label">Dashboard</span>
    `;
    dashboard.setAttribute("aria-label", "Toggle harness dashboard");
    dashboard.title = "Toggle harness dashboard";
    dashboard.setAttribute("aria-pressed", "false");
    bindRailButton(dashboard, () => togglePanel("dashboard"));

    const settings = document.createElement("button");
    settings.type = "button";
    settings.dataset.panel = "settings";
    settings.innerHTML = `
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
        <path d="M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z"></path>
        <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06A1.7 1.7 0 0 0 15 19.4a1.7 1.7 0 0 0-1 1.55V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1-1.55 1.7 1.7 0 0 0-1.88.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.55-1H3a2 2 0 1 1 0-4h.09A1.7 1.7 0 0 0 4.6 9a1.7 1.7 0 0 0-.34-1.88l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-1.55V3a2 2 0 1 1 4 0v.09A1.7 1.7 0 0 0 15 4.6a1.7 1.7 0 0 0 1.88-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.7 1.7 0 0 0 19.4 9a1.7 1.7 0 0 0 1.55 1H21a2 2 0 1 1 0 4h-.09A1.7 1.7 0 0 0 19.4 15Z"></path>
      </svg>
      <span class="chat-orchestrate-rail-label">Settings</span>
    `;
    settings.setAttribute("aria-label", "Toggle local agent settings");
    settings.title = "Toggle local agent settings";
    settings.setAttribute("aria-pressed", "false");
    bindRailButton(settings, () => togglePanel("settings"));

    rail.append(dashboard, settings);
    document.body.appendChild(rail);
    updateRailState();
  }

  ensureRail();
  window.addEventListener("load", ensureRail);
  new MutationObserver(() => {
    ensureRail();
    enforceDesiredState("dashboard");
    enforceDesiredState("settings");
    updateRailState();
  }).observe(document.documentElement, { childList: true, subtree: true });
})();
