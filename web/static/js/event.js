// Event page: the registration card, the signup dialog, and the mobile bottom bar.

(() => {
  const panel = document.querySelector("[data-panel]");
  if (!panel) return;

  const $ = (selector, scope = document) => scope.querySelector(selector);
  const api = `/api/events/${panel.dataset.eventId}`;
  const ctas = document.querySelectorAll("[data-cta]");
  const sheet = $("[data-signup]");
  const form = $("[data-form]");
  const submit = $("[data-submit]");
  const formError = $("[data-form-error]");
  const done = $("[data-done]");

  // Use the server's clock, not the laptop's, so every visitor sees the same countdown
  const opensAt = new Date(panel.dataset.opensAt).getTime();
  const clockOffset = new Date(panel.dataset.serverTime).getTime() - Date.now();
  const serverNow = () => Date.now() + clockOffset;

  let status = { state: panel.dataset.state, seats_left: Number($("[data-seats-left]").textContent) };
  let mine = null; // this browser's own registration, if it has one
  // The server already drew a signed-in person's ticket on the card: keep it until their registration arrives
  let mineLoaded = !panel.dataset.mine;
  let submitting = false;

  // What the card shows: the event's state, or "mine" once this browser has signed up
  const view = () => (mine && status.state !== "ended" ? "mine" : status.state);

  function render() {
    if (!mineLoaded) return;
    const current = view();
    document.querySelectorAll("[data-when]").forEach((el) => {
      el.hidden = el.dataset.when !== current || (el.hasAttribute("data-before-entry") && mine?.checked_in);
    });
    $("[data-seats-left]").textContent = status.seats_left;
    if ($("[data-held]") && status.held !== undefined) $("[data-held]").textContent = status.held;
    if ($("[data-waitlisted]") && status.waitlisted !== undefined) {
      $("[data-waitlisted]").textContent = status.waitlisted;
      $("[data-waitlisted-line]").hidden = !status.waitlisted;
    }

    const [label, enabled, note] = {
      upcoming: ["오픈 예정", false, "신청 오픈 전"],
      open: ["신청하기", true, `${status.seats_left}석 남음`],
      waitlist: ["대기 신청하기", true, "대기 신청 가능"],
      full: ["신청 마감", false, "정원 마감"],
      closed: ["신청 종료", false, "신청 기간 종료"],
      ended: ["종료된 행사", false, "종료된 행사"],
      mine: ["티켓 보기", true, mine?.card?.bar],
    }[current];
    ctas.forEach((button) => {
      button.textContent = label;
      button.disabled = !enabled;
      // On the card, a dead button is noise: the state text already says why
      if (button.closest("[data-panel]")) button.hidden = !enabled;
    });
    $("[data-buybar-note]").textContent = note;

    if (mine) {
      const { card } = mine;
      const badge = $("[data-mine-badge]");
      badge.className = card.badge ? `badge badge--${card.tone}` : "badge";
      badge.textContent = card.badge;
      badge.hidden = !card.badge;
      $("[data-mine-title]").textContent = card.title;
      $("[data-mine-text]").textContent = card.text;
    }
  }

  function tickCountdown() {
    const remaining = Math.max(0, opensAt - serverNow());
    const s = Math.floor(remaining / 1000);
    const d = Math.floor(s / 86400);
    $("[data-countdown]").textContent =
      `${d}일 ${App.pad(Math.floor((s % 86400) / 3600))}:${App.pad(Math.floor((s % 3600) / 60))}:${App.pad(s % 60)}`;
    if (remaining === 0 && status.state === "upcoming") refresh();
  }

  async function refresh() {
    const { ok, data } = await App.api(`${api}/status`);
    if (ok) {
      status = data;
      render();
    }
  }

  async function loadMine() {
    // The server knows a signed-in person's registration; otherwise this browser may remember one
    const id = panel.dataset.mine || App.myTicket(panel.dataset.eventId);
    if (!id) return;
    const { ok, data } = await App.api(`/api/registrations/${id}`);
    mineLoaded = true;
    if (!ok || data.status === "cancelled") {
      App.forgetTicket(panel.dataset.eventId);
      mine = null;
    } else {
      mine = data;
    }
    render();
  }

  // ---------- signup dialog ----------

  function openSheet() {
    if (mine) return (window.location.href = `/t/${mine.id}`);
    const waitlist = status.state === "waitlist";
    $("[data-sheet-title]").textContent = waitlist ? "대기 신청" : "참가 신청";
    $("[data-order-ticket]").textContent = waitlist ? "일반 참가 대기 1건" : "일반 참가 1매";
    submit.textContent = waitlist ? "대기 신청하기" : "신청하기";
    form.hidden = false;
    done.hidden = true;
    showError("");
    sheet.showModal();
    $("#name").focus();
  }

  function showError(message) {
    formError.textContent = message;
    formError.hidden = !message;
  }

  ctas.forEach((button) => button.addEventListener("click", openSheet));
  $("[data-close]").addEventListener("click", () => sheet.close());
  // A click on the dimmed backdrop closes the dialog too
  sheet.addEventListener("click", (event) => {
    if (event.target === sheet) sheet.close();
  });

  // An error goes away as soon as the person starts fixing it
  form.addEventListener("input", () => showError(""));

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    showError("");
    const fields = Object.fromEntries(new FormData(form));
    if (!fields.name.trim()) return showError("이름을 입력해 주세요.");
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(fields.email.trim())) return showError("이메일 형식을 확인해 주세요.");
    if (!fields.agree) return showError("개인정보 수집 및 이용에 동의해야 신청할 수 있습니다.");
    delete fields.agree;

    submitting = true;
    submit.disabled = true;
    const label = submit.textContent;
    submit.innerHTML = '<span class="spinner" aria-hidden="true"></span> 신청하는 중';

    // Invisible bot check: Google scores this visit, the server reads the score
    if (window.RECAPTCHA_SITE_KEY && window.grecaptcha) {
      fields.recaptcha_token = await new Promise((resolve) =>
        grecaptcha.enterprise.ready(() =>
          grecaptcha.enterprise.execute(window.RECAPTCHA_SITE_KEY, { action: "signup" }).then(resolve, () => resolve(""))
        )
      );
    }

    const { ok, data } = await App.api(`${api}/registrations`, { method: "POST", body: fields });
    submitting = false;
    submit.disabled = false;
    submit.textContent = label;

    if (!ok) {
      if (data.error === "duplicate" && App.myTicket(panel.dataset.eventId)) {
        showError("이미 신청하셨습니다. 상단의 '마이페이지'에서 확인할 수 있습니다.");
      } else {
        showError(data.message || "신청하지 못했습니다. 잠시 후 다시 시도해 주세요.");
      }
      return refresh();
    }

    App.rememberTicket(panel.dataset.eventId, data.id);
    // Seats can run out between opening the dialog and submitting: the server
    // then puts the person on the waitlist, and this step says so
    const waiting = data.status === "waitlisted";
    $("[data-done-title]").textContent = waiting ? "대기 명단에 등록되었습니다" : "신청이 완료되었습니다";
    $("[data-done-text]").textContent = waiting
      ? "자리가 생기면 신청 순서대로 자동 배정되고, 티켓 페이지에 QR 코드가 나타납니다."
      : "티켓을 발급하고 있습니다. 티켓 페이지에서 입장용 QR 코드를 확인할 수 있습니다.";
    $("[data-done-code]").textContent = data.code;
    $("[data-done-name]").textContent = fields.name.trim();
    $("[data-done-ticket]").href = data.ticket_url;
    const mailNote = $("[data-done-mail]");
    mailNote.hidden = !panel.dataset.mailReady;
    mailNote.textContent = `${fields.email.trim()} 주소로 확인 메일을 보냈습니다.`;
    $("[data-done-deadline]").hidden = waiting || data.confirmed;
    form.hidden = true;
    done.hidden = false;
    form.reset();
    await Promise.all([refresh(), loadMine()]);
  });

  render();
  loadMine();
  tickCountdown();
  setInterval(tickCountdown, 1000);
  // Seats change as others sign up; poll faster while signups are open
  (function poll() {
    const delay = status.state === "open" ? 2000 : 8000;
    setTimeout(async () => {
      if (!submitting) await refresh();
      poll();
    }, delay);
  })();
})();
