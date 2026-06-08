(() => {
  const railId = "chat-orchestrate-panel-rail";
  const styleId = "chat-orchestrate-panel-rail-style";
  const railVersion = "panel-rail-9";

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
        top: 12px;
        display: flex;
        gap: 4px;
        border: 1px solid color-mix(in srgb, currentColor 14%, transparent);
        border-radius: 10px;
        padding: 4px;
        background: color-mix(in srgb, Canvas 88%, transparent);
        box-shadow: 0 8px 24px color-mix(in srgb, black 16%, transparent);
        backdrop-filter: blur(14px);
        pointer-events: auto;
      }
      #${railId} button {
        width: 92px;
        min-height: 30px;
        border: 0;
        border-radius: 7px;
        padding: 7px 10px;
        background: transparent;
        color: CanvasText;
        cursor: pointer;
        font: 600 12px/1.2 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        text-align: center;
        opacity: 0.78;
        transition: background 120ms ease, color 120ms ease, opacity 120ms ease;
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
          width: 86px;
          overflow: hidden;
          white-space: nowrap;
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

  function toggleDashboard() {
    const existingDialog = dashboardDialog(true);
    if (existingDialog && existingDialog.classList.contains("chat-orchestrate-panel-hidden")) {
      showDialog(existingDialog);
      updateRailState();
      return;
    }
    const openDialog = dashboardDialog();
    if (openDialog) {
      closeDialog(openDialog);
      updateRailState();
      return;
    }
    const dashboardButton = findTextButton("Dashboard");
    if (dashboardButton) {
      dashboardButton.click();
      setTimeout(updateRailState, 200);
      return;
    }
    submitCommand("/dashboard");
    setTimeout(updateRailState, 800);
  }

  function toggleSettings() {
    const existingDialog = settingsDialog(true);
    if (existingDialog && existingDialog.classList.contains("chat-orchestrate-panel-hidden")) {
      showDialog(existingDialog);
      updateRailState();
      return;
    }
    const openDialog = settingsDialog();
    if (openDialog) {
      closeDialog(openDialog);
      updateRailState();
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
    setTimeout(updateRailState, 200);
  }

  function updateRailState() {
    const rail = document.getElementById(railId);
    if (!rail) return;
    const dashboard = rail.querySelector("[data-panel='dashboard']");
    const settings = rail.querySelector("[data-panel='settings']");
    dashboard?.setAttribute("aria-pressed", dashboardDialog() ? "true" : "false");
    settings?.setAttribute("aria-pressed", settingsDialog() ? "true" : "false");
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
    dashboard.textContent = "Dashboard";
    dashboard.title = "Toggle harness dashboard";
    dashboard.setAttribute("aria-pressed", "false");
    bindRailButton(dashboard, toggleDashboard);

    const settings = document.createElement("button");
    settings.type = "button";
    settings.dataset.panel = "settings";
    settings.textContent = "Settings";
    settings.title = "Toggle local agent settings";
    settings.setAttribute("aria-pressed", "false");
    bindRailButton(settings, toggleSettings);

    rail.append(dashboard, settings);
    document.body.appendChild(rail);
    updateRailState();
  }

  ensureRail();
  window.addEventListener("load", ensureRail);
  new MutationObserver(() => {
    ensureRail();
    updateRailState();
  }).observe(document.documentElement, { childList: true, subtree: true });
})();
