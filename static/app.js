(() => {
  "use strict";

  function notify(message, error = false) {
    const region = document.getElementById("toast-region");
    if (!region) return;
    const toast = document.createElement("div");
    toast.className = `toast${error ? " error" : ""}`;
    toast.textContent = message;
    region.append(toast);
    window.setTimeout(() => toast.remove(), 5000);
  }
  window.BookUI = { notify };

  const menu = document.getElementById("menu-toggle");
  const sidebar = document.getElementById("sidebar");
  const scrim = document.getElementById("mobile-scrim");
  const mobileWidth = window.matchMedia("(max-width: 780px)");
  function setMenu(open) {
    if (!menu || !sidebar || !scrim) return;
    sidebar.classList.toggle("open", open);
    sidebar.inert = mobileWidth.matches && !open;
    scrim.hidden = !open;
    menu.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
  }

  const privacyToggle = document.getElementById("privacy-toggle");
  if (privacyToggle) {
    const isPrivacy = localStorage.getItem("privacyMode") === "true";
    document.body.classList.toggle("privacy-mode", isPrivacy);
    privacyToggle.setAttribute("aria-pressed", isPrivacy);
    privacyToggle.addEventListener("click", () => {
      const mode = document.body.classList.toggle("privacy-mode");
      localStorage.setItem("privacyMode", mode);
      privacyToggle.setAttribute("aria-pressed", mode);
    });
  }
  menu?.addEventListener("click", () => setMenu(!sidebar.classList.contains("open")));
  scrim?.addEventListener("click", () => setMenu(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && sidebar?.classList.contains("open")) {
      setMenu(false);
      menu.focus();
    }
  });
  mobileWidth.addEventListener("change", () => setMenu(false));
  setMenu(false);

  document.querySelectorAll("[data-today]").forEach((element) => {
    const date = new Date(`${element.dataset.today}T12:00:00`);
    if (!Number.isNaN(date.getTime())) {
      element.textContent = new Intl.DateTimeFormat("en-GB", {
        weekday: "short", day: "numeric", month: "short", year: "numeric",
      }).format(date);
    }
  });

  function showDialog(id) {
    const dialog = document.getElementById(id);
    if (dialog instanceof HTMLDialogElement && !dialog.open) dialog.showModal();
  }
  document.querySelectorAll("[data-dialog]").forEach((button) => {
    button.addEventListener("click", () => showDialog(button.dataset.dialog));
  });
  document.querySelectorAll("[data-dialog-close]").forEach((button) => {
    button.addEventListener("click", () => button.closest("dialog")?.close());
  });
  document.querySelectorAll("dialog").forEach((dialog) => {
    const heading = dialog.querySelector("h2");
    if (heading) {
      heading.id ||= `${dialog.id}-heading`;
      dialog.setAttribute("aria-labelledby", heading.id);
    }
    dialog.addEventListener("click", (event) => {
      if (dialog.hasAttribute("data-mandatory") || dialog.id === "popup-advisory-dialog") return;
      if (event.target !== dialog) return;
      const box = dialog.getBoundingClientRect();
      if (event.clientX < box.left || event.clientX > box.right ||
          event.clientY < box.top || event.clientY > box.bottom) dialog.close();
    });
    dialog.addEventListener("cancel", (event) => {
      if (dialog.hasAttribute("data-mandatory") || dialog.id === "popup-advisory-dialog") {
        event.preventDefault();
      }
    });
  });

  let pendingConfirmation = null;
  const confirmDialog = document.getElementById("confirm-dialog");
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (form.dataset.confirmed === "true") {
        delete form.dataset.confirmed;
        return;
      }
      event.preventDefault();
      pendingConfirmation = { form, submitter: event.submitter };
      document.getElementById("confirm-message").textContent = form.dataset.confirm;
      showDialog("confirm-dialog");
    });
  });
  document.getElementById("confirm-submit")?.addEventListener("click", () => {
    if (!pendingConfirmation) return;
    const { form, submitter } = pendingConfirmation;
    form.dataset.confirmed = "true";
    confirmDialog.close();
    form.requestSubmit(submitter || undefined);
    pendingConfirmation = null;
  });

  // Global button loading circle on clicking/submitting
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.checkValidity && !form.checkValidity()) return;

    const btn = event.submitter || form.querySelector('button[type="submit"], button:not([type="button"]), input[type="submit"]');
    if (!btn || btn.classList.contains("is-loading")) return;

    btn.classList.add("is-loading");
    if (!btn.querySelector(".spinner-circle")) {
      const spinner = document.createElement("span");
      spinner.className = "spinner-circle";
      spinner.setAttribute("aria-hidden", "true");
      btn.prepend(spinner);
    }

    setTimeout(() => {
      if (btn && btn.classList.contains("is-loading")) {
        btn.querySelector(".spinner-circle")?.remove();
        btn.classList.remove("is-loading");
      }
    }, 12000);
  }, true);

  document.addEventListener("invalid", (event) => {
    const form = event.target.form;
    if (form) {
      const btn = form.querySelector(".is-loading");
      if (btn) {
        btn.querySelector(".spinner-circle")?.remove();
        btn.classList.remove("is-loading");
      }
    }
  }, true);

  document.querySelectorAll("[data-close-trade]").forEach((button) => {
    button.addEventListener("click", () => {
      const form = document.getElementById("close-trade-form");
      form.action = button.dataset.closeTrade;
      form.elements.sell_price.value = "";
      form.elements.sell_date.min = button.dataset.buyDate || "";
      document.getElementById("close-trade-stock").textContent = button.dataset.stock;
      showDialog("close-trade-dialog");
    });
  });

  document.querySelectorAll("[data-copy]").forEach((button) => {
    button.addEventListener("click", async () => {
      const input = document.getElementById(button.dataset.copy);
      if (!input) return;
      try {
        if (!navigator.clipboard) throw new Error("Clipboard unavailable");
        await navigator.clipboard.writeText(input.value);
        notify(button.dataset.copyMessage || "Copied to clipboard.");
      } catch {
        input.focus();
        input.select();
        notify("The link is selected. Press Ctrl+C or Command+C to copy.");
      }
    });
  });

  document.querySelectorAll("[data-dismiss-flash]").forEach((button) => {
    button.addEventListener("click", () => button.closest(".flash").remove());
  });
  document.querySelectorAll("[data-go-back]").forEach((button) => {
    button.addEventListener("click", () => {
      if (window.history.length > 1) window.history.back();
      else window.location.assign(button.dataset.goBack);
    });
  });
  document.querySelectorAll("[data-toggle-password]").forEach((button) => {
    button.addEventListener("click", () => {
      const input = document.getElementById(button.dataset.togglePassword);
      const visible = input.type === "password";
      input.type = visible ? "text" : "password";
      button.setAttribute("aria-label", visible ? "Hide password" : "Show password");
      button.setAttribute("aria-pressed", String(visible));
    });
  });

  function updateSelection() {
    const rows = [...document.querySelectorAll(".rowchk")];
    const count = rows.filter((input) => input.checked).length;
    rows.forEach((input) => input.closest("tr").classList.toggle("selected", input.checked));
    const button = document.getElementById("bulk-delete");
    const label = document.getElementById("selected-count");
    if (button) button.disabled = count === 0;
    if (label) label.textContent = count ? ` (${count})` : "";
    document.querySelectorAll("[data-select-all]").forEach((input) => {
      input.checked = rows.length > 0 && count === rows.length;
      input.indeterminate = count > 0 && count < rows.length;
    });
  }
  document.querySelectorAll("[data-select-all]").forEach((input) => {
    input.addEventListener("change", () => {
      document.querySelectorAll(input.dataset.selectAll).forEach((row) => { row.checked = input.checked; });
      updateSelection();
    });
  });
  document.querySelectorAll(".rowchk").forEach((input) => input.addEventListener("change", updateSelection));
  updateSelection();

  document.querySelectorAll("[data-table-search]").forEach((input) => {
    input.addEventListener("input", () => {
      const term = input.value.trim().toLocaleLowerCase();
      let shown = 0;
      document.querySelectorAll(`${input.dataset.tableSearch} tbody tr`).forEach((row) => {
        row.hidden = !row.textContent.toLocaleLowerCase().includes(term);
        if (!row.hidden) shown += 1;
      });
      const label = document.getElementById(input.dataset.searchCount);
      if (label) label.textContent = `${shown} result${shown === 1 ? "" : "s"}`;
      const empty = document.getElementById(input.dataset.searchEmpty);
      if (empty) empty.hidden = shown !== 0;
    });
  });

  const isoDate = (date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  document.querySelectorAll("[data-date-preset]").forEach((link) => {
    const today = new Date();
    const from = new Date(today.getFullYear(), link.dataset.datePreset === "year" ? 0 : today.getMonth(), 1);
    const url = new URL(link.href);
    url.searchParams.set("from", isoDate(from));
    url.searchParams.set("to", isoDate(today));
    link.href = url.href;
  });

  document.querySelectorAll("[data-rule-select]").forEach((select) => {
    const input = document.getElementById(select.dataset.ruleSelect);
    const update = () => {
      const custom = Boolean(select.value);
      input.disabled = !custom;
      input.required = custom;
      input.placeholder = custom ? (select.value === "flat" ? "Fee per trade" : "Percentage") : "Uses workspace default";
    };
    select.addEventListener("change", update);
    update();
  });

  document.querySelectorAll("[data-trade-form]").forEach((form) => {
    const buy = form.elements.buy_price;
    const sell = form.elements.sell_price;
    const buyDate = form.elements.buy_date;
    const sellDate = form.elements.sell_date;
    const validate = () => {
      buy.setCustomValidity(!buy.value && !sell.value ? "Enter a buy price or a sell price." :
        buyDate.value && !buy.value ? "Enter a buy price for this buy date." : "");
      sell.setCustomValidity(sellDate.value && !sell.value ? "Enter a sell price for this sell date." : "");
      buyDate.required = buy.value !== "";
      sellDate.required = sell.value !== "";
      sellDate.min = buyDate.value;
    };
    form.addEventListener("input", validate);
    validate();
  });

  const autoDialog = document.querySelector("[data-auto-open='true']");
  if (autoDialog) showDialog(autoDialog.id);
})();

// Cryptographically Strong Password Generator
window.generateStrongPassword = function(...targetIds) {
  const upper = "ABCDEFGHJKLMNPQRSTUVWXYZ";
  const lower = "abcdefghijkmnpqrstuvwxyz";
  const digits = "23456789";
  const symbols = "!@#$%^&*";
  const allChars = upper + lower + digits + symbols;
  
  let pw = [];
  const getRandom = (str) => {
    const array = new Uint32Array(1);
    crypto.getRandomValues(array);
    return str[array[0] % str.length];
  };
  
  pw.push(getRandom(upper));
  pw.push(getRandom(upper));
  pw.push(getRandom(lower));
  pw.push(getRandom(lower));
  pw.push(getRandom(digits));
  pw.push(getRandom(digits));
  pw.push(getRandom(symbols));
  pw.push(getRandom(symbols));
  
  while (pw.length < 14) {
    pw.push(getRandom(allChars));
  }
  
  for (let i = pw.length - 1; i > 0; i--) {
    const array = new Uint32Array(1);
    crypto.getRandomValues(array);
    const j = array[0] % (i + 1);
    [pw[i], pw[j]] = [pw[j], pw[i]];
  }
  const password = pw.join("");

  targetIds.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = password;
  });

  if (navigator.clipboard) {
    navigator.clipboard.writeText(password).then(() => {
      if (typeof showToast === "function") {
        showToast("Strong 14-char password generated & copied to clipboard!", "success");
      }
    }).catch(() => {});
  }
  return password;
};

// Lightweight Toast Notification
window.showToast = function(message, type = "info") {
  let container = document.getElementById("toast-container");
  if (!container) {
    container = document.createElement("div");
    container.id = "toast-container";
    container.style.cssText = "position:fixed;bottom:24px;left:50%;transform:translateX(-50%);z-index:999999;display:flex;flex-direction:column;gap:8px;pointer-events:none;max-width:90vw;";
    document.body.appendChild(container);
  }
  const toast = document.createElement("div");
  const bg = type === "error" ? "#7f1d1d" : type === "success" ? "#14532d" : "#1f2937";
  toast.style.cssText = `background:${bg};color:#fff;padding:10px 18px;border-radius:8px;font-size:13px;font-weight:600;box-shadow:0 8px 24px rgba(0,0,0,0.35);transition:opacity 0.25s ease,transform 0.25s ease;opacity:0;transform:translateY(10px);pointer-events:auto;`;
  toast.textContent = message;
  container.appendChild(toast);
  requestAnimationFrame(() => {
    toast.style.opacity = "1";
    toast.style.transform = "translateY(0)";
  });
  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateY(10px)";
    setTimeout(() => toast.remove(), 250);
  }, 3200);
};



