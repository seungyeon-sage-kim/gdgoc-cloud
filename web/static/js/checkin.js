// Door check-in: scan a QR with the camera (or type the code) and show the result.

(() => {
  const root = document.querySelector("[data-checkin]");
  if (!root) return;

  const video = root.querySelector("[data-video]");
  const frame = root.querySelector("[data-frame]");
  const idle = root.querySelector("[data-idle]");
  const note = root.querySelector("[data-camera-note]");
  const result = root.querySelector("[data-result]");
  const recent = root.querySelector("[data-recent]");

  const ICONS = {
    ok: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5 10 17.5 19 7"/></svg>',
    warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M12 5v9M12 19h.01"/></svg>',
    bad: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg>',
    busy: '<span class="spinner" aria-hidden="true"></span>',
  };

  // result → [tone, strip label, body title when there is no attendee name to show]
  const RESULTS = {
    checking: ["busy", "확인 중", "티켓을 확인하고 있습니다"],
    no_answer: ["warn", "확인 필요", "서버의 답을 받지 못했습니다"],
    ok: ["ok", "입장 완료", ""],
    already: ["warn", "이미 입장", "이미 입장한 티켓"],
    not_issued: ["warn", "확인 필요", "아직 발급 중인 티켓"],
    undone: ["warn", "입장 취소", ""],
    not_checked_in: ["warn", "확인 필요", "입장하지 않은 티켓"],
    waitlisted: ["bad", "입장 불가", "대기 명단의 신청"],
    cancelled: ["bad", "입장 불가", "취소된 티켓"],
    invalid: ["bad", "입장 불가", "유효하지 않은 QR"],
    unknown: ["bad", "입장 불가", "없는 티켓 코드"],
    wrong_event: ["bad", "입장 불가", "다른 행사의 티켓"],
  };

  const undoButton = root.querySelector("[data-result-undo]");

  function show(kind, title, detail, undoCode = "") {
    const [tone, label, fallback] = RESULTS[kind] || ["bad", "입장 불가", "확인할 수 없음"];
    result.className = tone === "busy" ? "result" : `result result--${tone}`;
    root.querySelector("[data-result-icon]").innerHTML = ICONS[tone];
    root.querySelector("[data-result-label]").textContent = label;
    root.querySelector("[data-result-time]").textContent = new Date().toTimeString().slice(0, 5);
    root.querySelector("[data-result-title]").textContent = title || fallback;
    root.querySelector("[data-result-detail]").textContent = detail;
    undoButton.hidden = !undoCode;
    undoButton.dataset.code = undoCode;
  }

  function showIdle() {
    result.className = "result";
    root.querySelector("[data-result-icon]").innerHTML = "";
    root.querySelector("[data-result-label]").textContent = "스캔 대기";
    root.querySelector("[data-result-time]").textContent = "";
    root.querySelector("[data-result-title]").textContent = "티켓을 스캔해 주세요";
    root.querySelector("[data-result-detail]").textContent = "확인 결과가 여기에 표시됩니다.";
    undoButton.hidden = true;
  }

  function addRecent(text, tone) {
    if (recent.querySelector(".subtle")) recent.innerHTML = "";
    const item = document.createElement("li");
    const time = new Date().toTimeString().slice(0, 5);
    item.innerHTML = `<span></span><span class="badge badge--${tone}"></span>`;
    item.firstChild.textContent = text;
    item.lastChild.textContent = time;
    recent.prepend(item);
    while (recent.children.length > 6) recent.lastChild.remove();
  }

  const post = (path, value) =>
    App.api(path, { method: "POST", timeout: 8000, body: { event_id: root.dataset.eventId, value } });

  // No answer says nothing about the ticket, so it must not look like a refusal
  const NO_ANSWER = "티켓 문제가 아닙니다. 연결을 확인하고 다시 스캔해 주세요.";

  // Show what happened at the door: the big result, the list and the counter
  function report(data) {
    const person = data.attendee;
    const about = person ? [person.affiliation, person.code].filter(Boolean).join(", ") : "";
    const outcomes = {
      ok: [person?.name, about],
      already: ["", person ? `${person.name}, ${person.checked_in_at?.slice(11, 16)}에 입장` : ""],
      not_issued: ["", person ? `${person.name}. 잠시 후 다시 확인해 주세요.` : ""],
      undone: [person?.name, "입장을 취소했습니다."],
      not_checked_in: ["", person ? `${person.name}. 입장 기록이 없습니다.` : ""],
      waitlisted: ["", person ? `${person.name}. 배정된 자리가 없습니다.` : ""],
      cancelled: ["", "이 티켓은 취소되어 입장할 수 없습니다."],
      invalid: ["", "서명이 맞지 않습니다. 위조되었거나 손상된 QR입니다."],
      unknown: ["", "코드를 다시 확인해 주세요."],
      wrong_event: ["", "이 행사의 티켓이 아닙니다. 행사 이름을 확인해 주세요."],
    };
    const [title, detail] = outcomes[data.result] || ["", ""];
    const canUndo = data.result === "ok" || data.result === "already";
    show(data.result, title, detail, canUndo ? person.code : "");
    if (person) addRecent(person.name, data.result === "ok" ? "success" : "warning");
    if (data.counts) {
      root.ownerDocument.querySelector("[data-count-in]").textContent = data.counts.checked_in;
      root.ownerDocument.querySelector("[data-count-issued]").textContent = data.counts.issued;
    }
    if (data.result === "ok") {
      if (navigator.vibrate) navigator.vibrate(60);
      App.toast(`${person.name} 입장 완료`);
    }
    if (data.result === "ok" || data.result === "undone") loadGuests();
  }

  // A valid ticket waits here until staff press 입장 처리
  const dialog = root.ownerDocument.querySelector("[data-door-dialog]");
  const confirmButton = dialog.querySelector("[data-door-confirm]");
  let waitingValue = "";

  let lastValue = "";
  let lastAt = 0;

  // The scan loop waits for this, so a QR still in the frame is tried again every few seconds
  async function retryAfterPause() {
    await new Promise((done) => setTimeout(done, 2000));
    lastValue = "";
  }

  async function check(value) {
    value = value.trim();
    if (!value || dialog.open) return;
    // The camera sees the same QR many times per second: it counts again only after leaving the frame
    if (value === lastValue && Date.now() - lastAt < 3000) {
      lastAt = Date.now();
      return;
    }
    lastValue = value;
    lastAt = Date.now();

    show("checking", "", "잠시만 기다려 주세요."); // the last person's result must not linger
    const { ok, status, data } = await post("/api/checkin/lookup", value);
    if (status === 401) return window.location.reload();
    if (!ok) {
      show("no_answer", "", data.message && status !== 0 && status < 500 ? data.message : NO_ANSWER);
      await retryAfterPause();
      return;
    }
    if (data.result !== "ready") return report(data);

    const person = data.attendee;
    waitingValue = value;
    dialog.querySelector("[data-door-name]").textContent = person.name;
    dialog.querySelector("[data-door-about]").textContent = [person.affiliation, person.code].filter(Boolean).join(", ");
    dialog.querySelector("[data-door-note]").hidden = !data.off_day;
    confirmButton.disabled = false;
    dialog.showModal();
  }

  confirmButton.addEventListener("click", async () => {
    confirmButton.disabled = true;
    const { ok, status, data } = await post("/api/checkin", waitingValue);
    dialog.close();
    if (status === 401) return window.location.reload();
    if (!ok) {
      // It may have gone through with only the answer lost. Scanning again shows 이미 입장 if so.
      show("no_answer", "", "입장 처리 여부를 확인하지 못했습니다. 다시 스캔하면 현재 상태가 표시됩니다.");
      await retryAfterPause();
      return;
    }
    report(data);
  });
  dialog.querySelector("[data-door-cancel]").addEventListener("click", () => {
    dialog.close();
    showIdle();
  });
  dialog.addEventListener("cancel", showIdle); // Escape
  dialog.addEventListener("close", () => {
    lastAt = Date.now(); // the same QR may still be in the frame
  });

  async function undo(code) {
    const { ok, status, data } = await post("/api/checkin/undo", code);
    if (status === 401) return window.location.reload();
    if (!ok) return App.toast(data.message || "취소하지 못했습니다");
    report(data);
  }
  undoButton.addEventListener("click", () => undo(undoButton.dataset.code));

  // The guest list: find someone by name and check them in by hand
  const guestList = root.querySelector("[data-guests]");
  const guestSearch = root.querySelector("[data-guest-search]");
  let guests = [];

  function drawGuests() {
    const term = guestSearch.value.trim().toUpperCase();
    const shown = guests.filter((g) => !term || g.name.toUpperCase().includes(term) || g.code.includes(term)).slice(0, 30);
    guestList.innerHTML = "";
    if (!shown.length) {
      guestList.innerHTML = '<li class="subtle">맞는 이름이 없습니다</li>';
      return;
    }
    for (const g of shown) {
      const item = document.createElement("li");
      item.innerHTML = '<span class="guest-text"><b></b><span></span></span>';
      item.querySelector("b").textContent = g.name;
      item.querySelector(".guest-text span").textContent = [g.affiliation, g.code].filter(Boolean).join(", ");
      if (g.checked_in) {
        const done = document.createElement("span");
        done.className = "guest-done";
        done.innerHTML = '<span class="badge badge--success">입장 완료</span><button class="link-button" type="button">취소</button>';
        done.querySelector("button").addEventListener("click", () => undo(g.code));
        item.append(done);
      } else {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn btn--secondary btn--sm";
        button.textContent = "입장";
        button.addEventListener("click", () => {
          lastValue = "";
          check(g.code);
        });
        item.append(button);
      }
      guestList.append(item);
    }
  }

  async function loadGuests() {
    const { ok, data } = await App.api(`/api/events/${root.dataset.eventId}/attendees`);
    if (ok) {
      guests = data;
      drawGuests();
    }
  }

  guestSearch.addEventListener("input", drawGuests);
  loadGuests();

  root.querySelector("[data-code-form]").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = event.target.elements.code;
    lastValue = "";
    const typed = input.value.trim();
    check(typed.includes(".") ? typed : typed.toUpperCase()); // a signed QR string is case-sensitive; a short code is not
    input.value = "";
  });

  // A reader takes the current camera frame and returns the QR's text, if there is one.
  // Chrome on Android and Mac have a built-in detector. Safari, Firefox and the rest
  // use jsQR, a decoder that runs in the page (static/js/vendor, Apache-2.0).
  async function builtInReader() {
    if (!("BarcodeDetector" in window)) return null;
    const formats = await BarcodeDetector.getSupportedFormats().catch(() => []);
    if (!formats.includes("qr_code")) return null;
    const detector = new BarcodeDetector({ formats: ["qr_code"] });
    return async () => (await detector.detect(video))[0]?.rawValue;
  }

  function pageReader() {
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d", { willReadFrequently: true });
    return () => {
      if (!video.videoWidth) return null;
      // A large frame only slows decoding down; a ticket held up to the camera fits in 800px
      const scale = Math.min(1, 800 / Math.max(video.videoWidth, video.videoHeight));
      canvas.width = Math.round(video.videoWidth * scale);
      canvas.height = Math.round(video.videoHeight * scale);
      context.drawImage(video, 0, 0, canvas.width, canvas.height);
      const { data, width, height } = context.getImageData(0, 0, canvas.width, canvas.height);
      return jsQR(data, width, height, { inversionAttempts: "dontInvert" })?.data;
    };
  }

  root.querySelector("[data-start]").addEventListener("click", async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
      video.srcObject = stream;
      video.hidden = false; // before playing: iPhones may not start a video that is display: none
      await video.play();
    } catch {
      video.hidden = true;
      note.textContent = "카메라를 사용할 수 없습니다. 브라우저의 카메라 권한을 확인하거나 신청 번호를 입력해 주세요.";
      return;
    }
    idle.hidden = true;
    frame.hidden = false;

    const builtIn = await builtInReader();
    const inPage = pageReader();
    (async function scan() {
      // Built-in detectors can fail or quietly find nothing on some devices, so jsQR always gets a turn
      const value = (await builtIn?.().catch(() => null)) || inPage();
      if (value) await check(value);
      setTimeout(scan, 120);
    })();
  });

  // iPhones pause the camera while the page is in the background
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && video.srcObject) video.play().catch(() => {});
  });
})();
