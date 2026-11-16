// Ticket page: wait for the worker to issue the ticket, then keep the status fresh.

(() => {
  const root = document.querySelector("[data-ticket]");
  if (!root) return;

  const id = root.dataset.id;
  const qr = root.querySelector("[data-qr]");
  const note = root.querySelector("[data-pending-note]");
  let status = root.dataset.status;
  const checkedIn = root.dataset.checkedIn === "true";
  const confirmed = root.dataset.confirmed === "true";


  async function poll() {
    const { ok, data } = await App.api(`/api/registrations/${id}`);
    if (!ok) return;

    if (status === "waitlisted" && data.status !== "waitlisted") {
      // Promoted from the waitlist: reload to show the ticket and the confirm button
      App.toast("자리가 배정되었습니다");
      return window.location.reload();
    }
    if (data.status === "waitlisted" && data.waitlist_position) {
      root.querySelectorAll("[data-position]").forEach((el) => {
        el.textContent = `대기 ${data.waitlist_position}번`;
      });
    }
    if (status === "pending" && data.status === "issued") {
      qr.innerHTML = `<img src="/t/${id}/qr.svg" alt="입장 QR 코드">`;
      note.hidden = true;
      App.toast("티켓이 발급되었습니다");
    }
    // Scanned or undone at the door, confirmed elsewhere, the seat released, or the deadline edited:
    // the server draws the page, so ask it for the new one
    if (
      data.checked_in !== checkedIn ||
      data.confirmed !== confirmed ||
      data.status === "cancelled" ||
      data.deadline !== root.dataset.deadline
    ) {
      return window.location.reload();
    }
    status = data.status;
  }

  if (root.dataset.closed !== "true") (function loop() {
    // Fast while the worker is busy, slow afterwards
    const delay = status === "pending" ? 1000 : 5000;
    setTimeout(async () => { await poll(); if (status !== "cancelled") loop(); }, delay);
  })();

  root.querySelector("[data-copy]")?.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      App.toast("링크를 복사했습니다");
    } catch {
      App.toast("복사하지 못했습니다");
    }
  });

  const confirmButton = root.querySelector("[data-confirm]");
  confirmButton?.addEventListener("click", async () => {
    confirmButton.disabled = true;
    const { ok, data } = await App.api(`/api/registrations/${id}/confirm`, { method: "POST" });
    if (!ok) {
      confirmButton.disabled = false;
      return App.toast(data.message || "확인하지 못했습니다");
    }
    App.toast("참석이 확인되었습니다");
    window.location.reload();
  });

  const cancelButton = root.querySelector("[data-cancel]");
  cancelButton?.addEventListener("click", async () => {
    if (!confirm("신청을 취소할까요? 자리는 다른 사람에게 돌아갑니다.")) return;
    cancelButton.disabled = true;
    const { ok, data } = await App.api(`/api/registrations/${id}/cancel`, { method: "POST" });
    if (!ok) {
      App.toast(data.message || "취소하지 못했습니다");
      cancelButton.disabled = false;
      return;
    }
    App.forgetTicket(root.dataset.eventId);
    window.location.reload();
  });
})();
