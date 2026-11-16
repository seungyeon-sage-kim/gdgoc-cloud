// Shared helpers used by every page.

const App = (() => {
  const toastEl = document.querySelector("[data-toast]");
  let toastTimer;

  function toast(message) {
    if (!toastEl) return;
    toastEl.textContent = message;
    toastEl.classList.add("is-visible");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove("is-visible"), 2400);
  }

  // fetch JSON; returns { ok, status, data } and never throws on HTTP errors
  // timeout (ms): give up on a request that hangs, as one does on weak Wi-Fi. It reports status 0.
  async function api(url, { timeout, ...options } = {}) {
    const controller = new AbortController();
    const timer = timeout && setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(url, {
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        ...options,
        body: options.body ? JSON.stringify(options.body) : undefined,
      });
      const data = await response.json().catch(() => ({}));
      return { ok: response.ok, status: response.status, data };
    } catch {
      return { ok: false, status: 0, data: { message: "네트워크 연결을 확인해 주세요." } };
    } finally {
      clearTimeout(timer);
    }
  }

  // The browser remembers its own ticket for each event (event id → registration id),
  // so the event page can show "신청 완료"
  const TICKETS_KEY = "gdgoc-tickets";
  function savedTickets() {
    try { return JSON.parse(localStorage.getItem(TICKETS_KEY)) || {}; } catch { return {}; }
  }
  function saveTickets(tickets) {
    try { localStorage.setItem(TICKETS_KEY, JSON.stringify(tickets)); } catch {}
  }
  function rememberTicket(eventId, id) {
    saveTickets({ ...savedTickets(), [eventId]: id });
  }
  function myTicket(eventId) {
    return savedTickets()[eventId] || null;
  }
  function forgetTicket(eventId) {
    const tickets = savedTickets();
    delete tickets[eventId];
    saveTickets(tickets);
  }

  // Signing out forgets this device's tickets too: the next person on a shared computer starts clean
  document.querySelectorAll('form[action="/logout"], form[action="/me/delete"]').forEach((form) =>
    form.addEventListener("submit", () => saveTickets({}))
  );

  // The account menu closes the way menus do: a click elsewhere, or Escape
  const accountMenu = document.querySelector("[data-account-menu]");
  if (accountMenu) {
    document.addEventListener("click", (event) => {
      if (!accountMenu.contains(event.target)) accountMenu.open = false;
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && accountMenu.open) {
        accountMenu.open = false;
        accountMenu.querySelector("summary").focus();
      }
    });
  }

  const pad = (n) => String(n).padStart(2, "0");

  return { toast, api, rememberTicket, myTicket, forgetTicket, pad };
})();
