// Sign-in and sign-up: Google's button, or a code sent by email.

(() => {
  const root = document.querySelector("[data-login]");
  if (!root) return;

  // Google's own button hands back a signed ID token; the server checks it and starts the session
  const google = root.querySelector("[data-google]");
  if (google) {
    const googleError = google.querySelector("[data-google-error]");
    const showGoogleError = (message) => {
      googleError.textContent = message;
      googleError.hidden = false;
    };
    const signIn = async ({ credential }) => {
      const { ok, data } = await App.api("/api/auth/google", {
        method: "POST",
        body: { credential, next: root.dataset.next },
      });
      if (ok) return (window.location.href = data.next);
      showGoogleError(data.message || "로그인하지 못했습니다.");
    };
    const start = () => {
      window.google.accounts.id.initialize({ client_id: google.dataset.clientId, callback: signIn });
      window.google.accounts.id.renderButton(google.querySelector("[data-google-button]"), {
        theme: "outline",
        size: "large",
        shape: "pill",
        text: google.dataset.text,
        locale: "ko",
        width: Math.min(google.clientWidth, 400),
      });
    };
    if (window.google?.accounts) start();
    else {
      const script = document.querySelector("script[data-gsi]");
      script.addEventListener("load", start);
      script.addEventListener("error", () => showGoogleError("Google 로그인을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요."));
    }
  }

  const startStep = root.querySelector('[data-step="start"]');
  const codeStep = root.querySelector('[data-step="code"]');
  const emailForm = root.querySelector("[data-email-form]");
  const codeForm = root.querySelector("[data-code-form]");
  if (!codeForm) return; // Google only, when email is not set up

  const VALID_EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
  let email = "";
  let name = ""; // from the sign-up form; the account gets it once the code proves the address

  function showError(form, message) {
    const box = form.querySelector("[data-error]");
    box.textContent = message;
    box.hidden = !message;
  }

  function busy(form, on) {
    const button = form.querySelector("button[type=submit]");
    button.disabled = on;
    if (on) {
      button.dataset.label = button.textContent;
      button.innerHTML = '<span class="spinner" aria-hidden="true"></span>';
    } else if (button.dataset.label) {
      button.textContent = button.dataset.label;
    }
  }

  async function sendCode(form) {
    busy(form, true);
    const { ok, data } = await App.api("/api/auth/code", { method: "POST", body: { email } });
    busy(form, false);
    if (!ok) return showError(form, data.message || "코드를 보내지 못했습니다.");
    showError(form, "");
    root.querySelector("[data-sent-to]").textContent = data.email;
    startStep.hidden = true;
    codeStep.hidden = false;
    codeForm.elements.code.value = "";
    codeForm.elements.code.focus();
    return true;
  }

  emailForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const fields = emailForm.elements;
    if (fields.name && !fields.name.value.trim()) return showError(emailForm, "이름을 입력해 주세요.");
    email = fields.email.value.trim();
    if (!VALID_EMAIL.test(email)) return showError(emailForm, "이메일 형식을 확인해 주세요.");
    if (fields.agree && !fields.agree.checked) return showError(emailForm, "이용약관과 개인정보 수집 및 이용에 동의해 주세요.");
    name = fields.name ? fields.name.value.trim() : "";
    sendCode(emailForm);
  });

  codeForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const code = codeForm.elements.code.value.replace(/\D/g, "");
    if (code.length !== 6) return showError(codeForm, "6자리 숫자를 입력해 주세요.");
    busy(codeForm, true);
    const { ok, data } = await App.api("/api/auth/verify", {
      method: "POST",
      body: { email, code, next: root.dataset.next, name },
    });
    if (ok) return (window.location.href = data.next);
    busy(codeForm, false);
    showError(codeForm, data.message || "확인하지 못했습니다.");
  });

  root.querySelector("[data-resend]").addEventListener("click", async () => {
    if (await sendCode(codeForm)) App.toast("코드를 다시 보냈습니다");
  });

  root.querySelector("[data-back]").addEventListener("click", () => {
    codeStep.hidden = true;
    startStep.hidden = false;
  });
})();
