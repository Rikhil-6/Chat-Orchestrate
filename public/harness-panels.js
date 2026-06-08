(() => {
  if (window.__chatOrchestratePanelRail) return;
  window.__chatOrchestratePanelRail = true;

  const railId = "chat-orchestrate-panel-rail";
  const styleId = "chat-orchestrate-panel-rail-style";

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
        left: 10px;
        top: 72px;
        display: flex;
        flex-direction: column;
        gap: 8px;
        pointer-events: auto;
      }
      #${railId} button {
        width: 116px;
        border: 1px solid color-mix(in srgb, currentColor 18%, transparent);
        border-radius: 8px;
        padding: 8px 10px;
        background: color-mix(in srgb, Canvas 88%, transparent);
        color: CanvasText;
        box-shadow: 0 8px 30px color-mix(in srgb, black 18%, transparent);
        cursor: pointer;
        font: 600 12px/1.2 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        text-align: left;
      }
      #${railId} button:hover {
        border-color: #ff0f68;
      }
      @media (max-width: 720px) {
        #${railId} {
          top: auto;
          bottom: 92px;
        }
        #${railId} button {
          width: 44px;
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

  function openDashboard() {
    if (document.querySelector("[data-harness-dashboard='true']")) return;
    const dashboardButton = findTextButton("Dashboard");
    if (dashboardButton) {
      dashboardButton.click();
      return;
    }
    submitCommand("/dashboard");
  }

  function openSettings() {
    const settingsOpen = Array.from(document.querySelectorAll("[role='dialog']")).some((dialog) => {
      return isVisible(dialog) && (dialog.textContent || "").trim().startsWith("Settings panel");
    });
    if (settingsOpen) return;

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

  function ensureRail() {
    addStyle();
    if (document.getElementById(railId)) return;
    const rail = document.createElement("div");
    rail.id = railId;

    const dashboard = document.createElement("button");
    dashboard.type = "button";
    dashboard.textContent = "Dashboard";
    dashboard.title = "Open harness dashboard";
    dashboard.addEventListener("click", openDashboard);

    const settings = document.createElement("button");
    settings.type = "button";
    settings.textContent = "Settings";
    settings.title = "Open local agent settings";
    settings.addEventListener("click", openSettings);

    rail.append(dashboard, settings);
    document.body.appendChild(rail);
  }

  ensureRail();
  window.addEventListener("load", ensureRail);
  new MutationObserver(ensureRail).observe(document.documentElement, { childList: true, subtree: true });
})();
