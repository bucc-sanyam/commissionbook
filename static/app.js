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

  function updatePrivacyMode(isPrivacy) {
    document.body.classList.toggle("privacy-mode", Boolean(isPrivacy));
    const adminToggle = document.getElementById("privacy-toggle");
    if (adminToggle) {
      adminToggle.setAttribute("aria-pressed", isPrivacy ? "true" : "false");
      adminToggle.classList.toggle("privacy-active", Boolean(isPrivacy));
      const iconOff = adminToggle.querySelector(".privacy-icon-off");
      const iconOn = adminToggle.querySelector(".privacy-icon-on");
      if (iconOff) iconOff.style.display = isPrivacy ? "none" : "inline-flex";
      if (iconOn) iconOn.style.display = isPrivacy ? "inline-flex" : "none";
    }
    const clientToggle = document.getElementById("client-privacy-toggle");
    if (clientToggle) {
      clientToggle.setAttribute("aria-pressed", isPrivacy ? "true" : "false");
      const iconOff = clientToggle.querySelector(".privacy-icon-off");
      const iconOn = clientToggle.querySelector(".privacy-icon-on");
      const label = clientToggle.querySelector(".privacy-label");
      if (iconOff) iconOff.style.display = isPrivacy ? "none" : "inline-flex";
      if (iconOn) iconOn.style.display = isPrivacy ? "inline-flex" : "none";
      if (label) label.textContent = isPrivacy ? "Show values" : "Hide values";
    }
    // Clear any temporary peek reveals when toggling global mode
    document.querySelectorAll(".privacy-revealed").forEach((el) => el.classList.remove("privacy-revealed"));
  }
  window.updatePrivacyMode = updatePrivacyMode;

  let lastToggleTime = 0;
  function togglePrivacy() {
    const now = Date.now();
    if (now - lastToggleTime < 250) {
      return document.body.classList.contains("privacy-mode");
    }
    lastToggleTime = now;
    const next = !document.body.classList.contains("privacy-mode");
    localStorage.setItem("privacyMode", next ? "true" : "false");
    updatePrivacyMode(next);
    return next;
  }
  window.togglePrivacy = togglePrivacy;

  // Global event delegation for privacy toggles and tap-to-reveal
  document.addEventListener("click", (event) => {
    const btn = event.target.closest("#privacy-toggle, #client-privacy-toggle");
    if (btn) {
      event.preventDefault();
      event.stopPropagation();
      togglePrivacy();
      return;
    }

    // Tap/click to peek/reveal individual card or number when in privacy mode
    if (document.body.classList.contains("privacy-mode")) {
      // Ignore clicks on actionable controls
      if (event.target.closest("button, a, input, select, textarea, form, dialog, .actions, .filter-actions")) {
        return;
      }
      const card = event.target.closest(".kpi-card, .metric-card, .activity-row, .ledger-summary-item, [data-privacy-value], .chart-summary, .mobile-trade-card");
      if (card) {
        event.preventDefault();
        card.classList.toggle("privacy-revealed");
      }
    }
  });

  function initPrivacy() {
    let isPrivacy = false;
    try {
      isPrivacy = localStorage.getItem("privacyMode") === "true";
    } catch (e) {}
    updatePrivacyMode(isPrivacy);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initPrivacy);
  } else {
    initPrivacy();
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
    if (dialog instanceof HTMLDialogElement && !dialog.open) {
      dialog.showModal();
      if (typeof initStockAutocomplete === "function") initStockAutocomplete();
      if (typeof initPhoneInputs === "function") initPhoneInputs();
    }
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
      document.querySelectorAll(input.dataset.selectAll).forEach((row) => {
        if (!row.closest("tr")?.hidden) {
          row.checked = input.checked;
        } else if (!input.checked) {
          row.checked = false;
        }
      });
      updateSelection();
    });
  });
  document.querySelectorAll(".rowchk").forEach((input) => input.addEventListener("change", updateSelection));
  updateSelection();

  document.querySelectorAll("[data-table-search]").forEach((input) => {
    input.addEventListener("input", () => {
      const term = input.value.trim().toLocaleLowerCase();
      const targetTable = document.querySelector(input.dataset.tableSearch);
      const lazyControls = targetTable?.closest(".table-wrap")?.querySelector(".table-lazy-controls");
      let shown = 0;
      if (term) {
        if (lazyControls) lazyControls.style.display = "none";
        targetTable?.querySelectorAll("tbody tr").forEach((row) => {
          row.hidden = !row.textContent.toLocaleLowerCase().includes(term);
          if (!row.hidden) shown += 1;
        });
      } else {
        if (lazyControls) {
          lazyControls.style.display = "";
          initLazyTables();
          shown = targetTable?.querySelectorAll("tbody tr:not([hidden])").length || 0;
        } else {
          targetTable?.querySelectorAll("tbody tr").forEach((row) => {
            row.hidden = false;
            shown += 1;
          });
        }
      }
      const label = document.getElementById(input.dataset.searchCount);
      if (label) {
        const totalRows = targetTable?.querySelectorAll("tbody tr").length || 0;
        label.textContent = term ? `${shown} result${shown === 1 ? "" : "s"}` : `${totalRows} record${totalRows === 1 ? "" : "s"}`;
      }
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

  // -------------------------------------------------------------
  // Phone Number Sanitization (+91 removal, strictly 10 digits)
  // -------------------------------------------------------------
  function cleanPhoneNumberValue(val) {
    if (!val) return "";
    let s = String(val).trim();
    if (s.startsWith("+91")) {
      s = s.slice(3);
    } else if (s.startsWith("+")) {
      s = s.slice(1);
    }
    let digits = s.replace(/\D/g, "");
    if (digits.startsWith("91") && digits.length > 10) {
      digits = digits.slice(2);
    }
    while (digits.startsWith("0") && digits.length > 10) {
      digits = digits.slice(1);
    }
    if (digits.length > 10) {
      digits = digits.slice(-10);
    }
    return digits;
  }

  function initPhoneInputs() {
    const phoneSelectors = 'input[type="tel"], input[name="phone"], #phone, #quick-phone';
    document.querySelectorAll(phoneSelectors).forEach((input) => {
      if (input.dataset.phoneSanitized === "true") return;
      input.dataset.phoneSanitized = "true";

      const sanitize = () => {
        const orig = input.value;
        const cleaned = cleanPhoneNumberValue(orig);
        if (orig !== cleaned) {
          input.value = cleaned;
        }
      };

      input.addEventListener("input", sanitize);
      input.addEventListener("paste", () => setTimeout(sanitize, 10));
      input.addEventListener("blur", sanitize);

      // Initial check if prefilled
      if (input.value) sanitize();
    });
  }

  // -------------------------------------------------------------
  // Stock Autocomplete (Starts showing suggestions after 1 letter)
  // -------------------------------------------------------------
  let cachedStockSuggestions = window.__STOCK_SUGGESTIONS__ || [];

  async function loadStockSuggestions() {
    if (Array.isArray(cachedStockSuggestions) && cachedStockSuggestions.length > 0) {
      return cachedStockSuggestions;
    }
    try {
      const res = await fetch("/api/stocks/suggestions");
      if (res.ok) {
        const data = await res.json();
        if (Array.isArray(data) && data.length > 0) {
          cachedStockSuggestions = data;
          window.__STOCK_SUGGESTIONS__ = data;
        }
      }
    } catch (e) {
      console.warn("Could not fetch stock suggestions:", e);
    }
    return cachedStockSuggestions || [];
  }

  function initStockAutocomplete() {
    const targets = document.querySelectorAll(
      'input.stock-autocomplete-input, input[name="stock"], #trade-stock-input, #popup-trade-stock-input'
    );

    targets.forEach((input) => {
      if (input.dataset.stockAutocompleteActive === "true") return;
      input.dataset.stockAutocompleteActive = "true";

      input.addEventListener("focus", () => {
        loadStockSuggestions();
      }, { once: true });

      input.setAttribute("autocomplete", "off");
      input.setAttribute("autocorrect", "off");
      input.setAttribute("spellcheck", "false");

      let wrapper = input.parentElement;
      if (!wrapper || !wrapper.classList.contains("stock-autocomplete-wrapper")) {
        const wrap = document.createElement("div");
        wrap.className = "stock-autocomplete-wrapper";
        input.parentNode.insertBefore(wrap, input);
        wrap.appendChild(input);
        wrapper = wrap;
      }

      const dropdown = document.createElement("div");
      dropdown.className = "stock-autocomplete-dropdown";
      dropdown.hidden = true;
      wrapper.appendChild(dropdown);

      let filteredItems = [];
      let selectedIndex = -1;

      function renderDropdown(items, query) {
        filteredItems = items;
        selectedIndex = -1;
        dropdown.innerHTML = "";

        if (!items || items.length === 0) {
          const empty = document.createElement("div");
          empty.className = "stock-autocomplete-empty";
          empty.textContent = `No stocks matching "${query}"`;
          dropdown.appendChild(empty);
          dropdown.hidden = false;
          return;
        }

        items.forEach((item, index) => {
          const row = document.createElement("div");
          row.className = "stock-autocomplete-item";
          row.dataset.index = index;

          const sym = item.canonical || item.symbol;
          const qUpper = query.toUpperCase();
          const matchIdx = sym.toUpperCase().indexOf(qUpper);

          let symHtml = sym;
          if (matchIdx !== -1) {
            const before = sym.slice(0, matchIdx);
            const match = sym.slice(matchIdx, matchIdx + qUpper.length);
            const after = sym.slice(matchIdx + qUpper.length);
            symHtml = `${before}<span class="stock-match-highlight">${match}</span>${after}`;
          }

          const symSpan = document.createElement("span");
          symSpan.className = "stock-sym-label";
          symSpan.innerHTML = symHtml;
          row.appendChild(symSpan);

          row.addEventListener("pointerdown", (e) => {
            e.preventDefault();
            selectItem(item);
          });

          dropdown.appendChild(row);
        });

        dropdown.hidden = false;
      }

      function selectItem(item) {
        const targetVal = item.canonical || item.symbol;
        input.value = targetVal;
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.dispatchEvent(new Event("change", { bubbles: true }));
        dropdown.hidden = true;
        selectedIndex = -1;
      }

      function updateActiveItem() {
        const rows = dropdown.querySelectorAll(".stock-autocomplete-item");
        rows.forEach((r, idx) => {
          if (idx === selectedIndex) {
            r.classList.add("is-selected");
            r.scrollIntoView({ block: "nearest" });
          } else {
            r.classList.remove("is-selected");
          }
        });
      }

      async function handleInput() {
        const query = input.value.trim().toUpperCase();
        if (!query || query.length < 1) {
          dropdown.hidden = true;
          filteredItems = [];
          selectedIndex = -1;
          return;
        }

        const allList = await loadStockSuggestions();

        const startsWithMatches = [];
        const containsMatches = [];
        const seenSymbols = new Set();

        allList.forEach((item) => {
          const mainSym = (item.canonical || item.symbol || "").toUpperCase();
          if (!mainSym || seenSymbols.has(mainSym)) return;

          const sym = (item.symbol || "").toUpperCase();
          const canon = (item.canonical || "").toUpperCase();

          if (mainSym.startsWith(query) || sym.startsWith(query)) {
            seenSymbols.add(mainSym);
            startsWithMatches.push({ symbol: mainSym, canonical: mainSym });
          } else if (mainSym.includes(query) || canon.includes(query) || sym.includes(query)) {
            seenSymbols.add(mainSym);
            containsMatches.push({ symbol: mainSym, canonical: mainSym });
          }
        });

        const combined = [...startsWithMatches, ...containsMatches].slice(0, 16);
        renderDropdown(combined, query);
      }

      input.addEventListener("input", handleInput);
      input.addEventListener("focus", () => {
        if (input.value.trim().length >= 1) {
          handleInput();
        }
      });

      input.addEventListener("keydown", (e) => {
        if (dropdown.hidden) {
          if (e.key === "ArrowDown" && input.value.trim().length >= 1) {
            e.preventDefault();
            handleInput();
          }
          return;
        }

        if (e.key === "ArrowDown") {
          e.preventDefault();
          if (filteredItems.length > 0) {
            selectedIndex = (selectedIndex + 1) % filteredItems.length;
            updateActiveItem();
          }
        } else if (e.key === "ArrowUp") {
          e.preventDefault();
          if (filteredItems.length > 0) {
            selectedIndex = (selectedIndex - 1 + filteredItems.length) % filteredItems.length;
            updateActiveItem();
          }
        } else if (e.key === "Enter" || e.key === "Tab") {
          if (selectedIndex >= 0 && selectedIndex < filteredItems.length) {
            e.preventDefault();
            selectItem(filteredItems[selectedIndex]);
          }
        } else if (e.key === "Escape") {
          dropdown.hidden = true;
          selectedIndex = -1;
        }
      });

      document.addEventListener("pointerdown", (e) => {
        if (!wrapper.contains(e.target)) {
          dropdown.hidden = true;
          selectedIndex = -1;
        }
      });
    });
  }

  // Dynamic Holding Modals Handler (eliminates repeated per-holding dialogs)
  document.addEventListener("click", (event) => {
    const editBtn = event.target.closest(".edit-holding-btn");
    if (editBtn) {
      const dialog = document.getElementById("dynamic-edit-holding-dialog");
      if (dialog) {
        dialog.querySelector("form").action = editBtn.dataset.action;
        dialog.querySelector("#dynamic-edit-holding-title").textContent = `Edit ${editBtn.dataset.stock} holding`;
        dialog.querySelector("#dynamic-edit-holding-stock").value = editBtn.dataset.stock;
        dialog.querySelector("#dynamic-edit-holding-quantity").value = editBtn.dataset.qty;
        dialog.querySelector("#dynamic-edit-holding-buy-price").value = editBtn.dataset.buyPrice;
        dialog.showModal();
      }
      return;
    }
    const sellBtn = event.target.closest(".sell-holding-btn");
    if (sellBtn) {
      const dialog = document.getElementById("dynamic-sell-holding-dialog");
      if (dialog) {
        dialog.querySelector("form").action = sellBtn.dataset.action;
        dialog.querySelector("#dynamic-sell-holding-title").textContent = `Sell ${sellBtn.dataset.stock}`;
        const qtyInput = dialog.querySelector("#dynamic-sell-holding-quantity");
        qtyInput.value = sellBtn.dataset.qty;
        qtyInput.max = sellBtn.dataset.qty;
        dialog.querySelector("#dynamic-sell-holding-price").value = "";
        dialog.showModal();
      }
      return;
    }
  });

  // Progressive Table Lazy Loading (chunks rows to prevent mobile DOM lag)
  function initLazyTables() {
    document.querySelectorAll("table[data-lazy-table]").forEach((table) => {
      const tbody = table.querySelector("tbody");
      if (!tbody) return;
      const rows = Array.from(tbody.querySelectorAll("tr"));
      const limit = parseInt(table.dataset.lazyTable, 10) || 20;
      if (rows.length <= limit) return;

      let currentVisible = limit;
      rows.forEach((row, idx) => {
        if (idx >= limit) row.hidden = true;
      });

      let container = table.closest(".table-wrap");
      if (!container) container = table.parentElement;

      // Avoid duplicate controls if reinitialized
      container.querySelector(".table-lazy-controls")?.remove();

      const footer = document.createElement("div");
      footer.className = "table-lazy-controls";

      const infoSpan = document.createElement("span");
      infoSpan.textContent = `Showing 1–${currentVisible} of ${rows.length} records`;

      const btnWrap = document.createElement("div");
      btnWrap.style.cssText = "display:flex; gap:8px;";

      const loadMoreBtn = document.createElement("button");
      loadMoreBtn.type = "button";
      loadMoreBtn.className = "btn btn-ghost btn-xs";
      loadMoreBtn.textContent = `Show next ${Math.min(limit, rows.length - currentVisible)}`;
      loadMoreBtn.addEventListener("click", () => {
        const nextTarget = Math.min(rows.length, currentVisible + limit);
        for (let i = currentVisible; i < nextTarget; i++) {
          rows[i].hidden = false;
        }
        currentVisible = nextTarget;
        infoSpan.textContent = `Showing 1–${currentVisible} of ${rows.length} records`;
        if (currentVisible >= rows.length) {
          footer.remove();
        } else {
          loadMoreBtn.textContent = `Show next ${Math.min(limit, rows.length - currentVisible)}`;
        }
      });

      const showAllBtn = document.createElement("button");
      showAllBtn.type = "button";
      showAllBtn.className = "btn btn-ghost btn-xs";
      showAllBtn.textContent = "Show all";
      showAllBtn.addEventListener("click", () => {
        rows.forEach((r) => (r.hidden = false));
        footer.remove();
      });

      btnWrap.appendChild(loadMoreBtn);
      btnWrap.appendChild(showAllBtn);
      footer.appendChild(infoSpan);
      footer.appendChild(btnWrap);
      container.appendChild(footer);
    });

    // 2. Generic Card Lists (e.g. mobile cards)
    document.querySelectorAll("[data-lazy-list]").forEach((container) => {
      const items = Array.from(container.children).filter((el) => !el.classList.contains("table-lazy-controls"));
      const limit = parseInt(container.dataset.lazyList, 10) || 15;
      if (items.length <= limit) return;

      let currentVisible = limit;
      items.forEach((item, idx) => {
        if (idx >= limit) item.hidden = true;
      });

      container.querySelector(".table-lazy-controls")?.remove();

      const footer = document.createElement("div");
      footer.className = "table-lazy-controls";
      footer.style.borderRadius = "8px";
      footer.style.marginTop = "8px";

      const infoSpan = document.createElement("span");
      infoSpan.textContent = `Showing 1–${currentVisible} of ${items.length} items`;

      const btnWrap = document.createElement("div");
      btnWrap.style.cssText = "display:flex; gap:8px;";

      const loadMoreBtn = document.createElement("button");
      loadMoreBtn.type = "button";
      loadMoreBtn.className = "btn btn-ghost btn-xs";
      loadMoreBtn.textContent = `Show next ${Math.min(limit, items.length - currentVisible)}`;
      loadMoreBtn.addEventListener("click", () => {
        const nextTarget = Math.min(items.length, currentVisible + limit);
        for (let i = currentVisible; i < nextTarget; i++) {
          items[i].hidden = false;
        }
        currentVisible = nextTarget;
        infoSpan.textContent = `Showing 1–${currentVisible} of ${items.length} items`;
        if (currentVisible >= items.length) {
          footer.remove();
        } else {
          loadMoreBtn.textContent = `Show next ${Math.min(limit, items.length - currentVisible)}`;
        }
      });

      const showAllBtn = document.createElement("button");
      showAllBtn.type = "button";
      showAllBtn.className = "btn btn-ghost btn-xs";
      showAllBtn.textContent = "Show all";
      showAllBtn.addEventListener("click", () => {
        items.forEach((r) => (r.hidden = false));
        footer.remove();
      });

      btnWrap.appendChild(loadMoreBtn);
      btnWrap.appendChild(showAllBtn);
      footer.appendChild(infoSpan);
      footer.appendChild(btnWrap);
      container.appendChild(footer);
    });
  }

  // Instant Top Navigation Progress Bar
  const pBar = document.getElementById("app-progress-bar");
  function startProgress() {
    if (!pBar) return;
    pBar.hidden = false;
    pBar.classList.remove("done");
    pBar.classList.add("loading");
  }
  function completeProgress() {
    if (!pBar) return;
    pBar.classList.remove("loading");
    pBar.classList.add("done");
    setTimeout(() => {
      pBar.hidden = true;
    }, 300);
  }

  document.addEventListener("click", (e) => {
    const link = e.target.closest("a[href]");
    if (!link) return;
    const href = link.getAttribute("href");
    if (!href || href.startsWith("#") || href.startsWith("javascript:") || link.target === "_blank") return;
    startProgress();
  });

  document.addEventListener("submit", (e) => {
    const form = e.target;
    if (form && form.target !== "_blank") {
      startProgress();
    }
  });

  window.addEventListener("pageshow", completeProgress);

  const autoDialog = document.querySelector("[data-auto-open='true']");
  if (autoDialog) showDialog(autoDialog.id);

  initStockAutocomplete();
  initPhoneInputs();
  initLazyTables();
  window.initStockAutocomplete = initStockAutocomplete;
  window.initPhoneInputs = initPhoneInputs;
  window.initLazyTables = initLazyTables;
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

// ==========================================================================
// Live 1-Minute Stock Market Ticker for Indian Market (09:15 - 15:45 IST, Mon-Fri)
// ==========================================================================
window.formatRupee = function(num) {
  if (num === null || num === undefined || isNaN(num)) return "—";
  return "₹" + Number(num).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
};

window.initLiveMarketTicker = function(options = {}) {
  const badgeId = options.badgeId || "market-ticker-badge";
  const badgeEl = document.getElementById(badgeId);

  function isMarketOpenIST() {
    const now = new Date();
    // UTC time in ms
    const utcMs = now.getTime() + (now.getTimezoneOffset() * 60000);
    // IST is UTC + 5.5 hours
    const ist = new Date(utcMs + (3600000 * 5.5));
    const day = ist.getDay(); // 0 is Sunday, 6 is Saturday
    if (day === 0 || day === 6) return false;
    const mins = ist.getHours() * 60 + ist.getMinutes();
    return mins >= (9 * 60 + 15) && mins <= (15 * 60 + 45); // 09:15 to 15:45 IST
  }

  function updateBadgeUI(isOpen, timeText) {
    if (!badgeEl) return;
    if (isOpen) {
      badgeEl.className = "ticker-badge ticker-live";
      badgeEl.innerHTML = `
        <span class="ticker-pulse"></span>
        <span>Live 20s Ticker</span>
      `;
      badgeEl.title = "Live market: Auto-updates every 20s (9:15 AM - 3:45 PM IST)" + (timeText ? ` · Last: ${timeText}` : "");
    } else {
      badgeEl.className = "ticker-badge ticker-closed";
      badgeEl.innerHTML = `
        <span class="ticker-dot-closed"></span>
        <span>Market Closed (9:15 AM - 3:45 PM IST)</span>
      `;
      badgeEl.title = "Market trading hours: Mon-Fri 9:15 AM - 3:45 PM IST";
    }
  }

  let timerId = null;
  let isFetching = false;
  let lastFetchTime = Date.now();

  async function pollLiveQuotes(force = false) {
    if (isFetching) return;
    if (document.hidden && !force) return;

    const marketOpen = isMarketOpenIST();
    updateBadgeUI(marketOpen);

    // If market closed and not manually forced, do not poll
    if (!marketOpen && !force) return;

    isFetching = true;
    try {
      const resp = await fetch("/api/update-prices" + (force ? "?force=1" : ""), {
        headers: { "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" }
      });
      if (!resp.ok) return;
      const data = await resp.json();
      lastFetchTime = Date.now();
      if (data && data.updated) {
        applyLiveQuoteUpdates(data);
        updateBadgeUI(data.is_market_hours ?? marketOpen, data.updated_at_ist);
      }
    } catch (err) {
      console.warn("Live ticker update notice:", err);
    } finally {
      isFetching = false;
    }
  }

  function applyLiveQuoteUpdates(data) {
    if (!data || !Array.isArray(data.updated)) return;
    const quotes = data.updated;
    const quoteMap = {};
    quotes.forEach(q => {
      if (q.stock) quoteMap[q.stock.toUpperCase()] = q;
      if (q.ticker) quoteMap[q.ticker.toUpperCase()] = q;
    });

    // 1. Update Holdings Page rows (desktop table)
    document.querySelectorAll("#holdings-table tbody tr[data-stock], #portal-holdings-table tbody tr[data-stock]").forEach(row => {
      const stockKey = (row.getAttribute("data-stock") || "").toUpperCase();
      const q = quoteMap[stockKey];
      if (!q) return;

      const qty = parseFloat(row.getAttribute("data-qty") || "0");
      const buyPrice = parseFloat(row.getAttribute("data-buy") || "0");
      const cmp = parseFloat(q.current_price || "0");

      const cmpEl = row.querySelector("[data-cmp]");
      if (cmpEl) {
        const prevText = cmpEl.textContent.trim();
        const nextText = q.current_price_formatted || window.formatRupee(cmp);
        if (prevText !== nextText) {
          cmpEl.textContent = nextText;
          const isUp = q.change >= 0;
          cmpEl.classList.remove("price-flash-up", "price-flash-down");
          void cmpEl.offsetWidth; // trigger reflow
          cmpEl.classList.add(isUp ? "price-flash-up" : "price-flash-down");
        }
      }

      const updatedEl = row.querySelector("[data-updated]");
      if (updatedEl && data.updated_at_ist) {
        updatedEl.textContent = data.updated_at_ist;
      }

      const changeEl = row.querySelector("[data-change]");
      if (changeEl) {
        const isUp = q.change >= 0;
        changeEl.className = "daily-change " + (isUp ? "pos" : "neg");
        changeEl.textContent = `${q.change_pct_formatted || (isUp ? "+" : "") + q.change_pct + "%"} today`;
      }

      const curValEl = row.querySelector("[data-curval]");
      if (curValEl && qty > 0) {
        curValEl.textContent = window.formatRupee(qty * cmp);
      }

      const pnlEl = row.querySelector("[data-pnl]");
      if (pnlEl && qty > 0 && buyPrice > 0) {
        const cost = qty * buyPrice;
        const curVal = qty * cmp;
        const pnl = curVal - cost;
        const pnlPct = cost > 0 ? (pnl / cost * 100) : 0;
        const isUp = pnl >= 0;
        pnlEl.className = "tabular " + (isUp ? "pos" : "neg");
        pnlEl.innerHTML = `${isUp ? "+" : ""}${window.formatRupee(pnl)} <small>(${isUp ? "+" : ""}${pnlPct.toFixed(2)}%)</small>`;
      }
    });

    // 2. Update Mobile Cards (portal mobile holdings)
    document.querySelectorAll(".mobile-trade-card[data-stock]").forEach(card => {
      const stockKey = (card.getAttribute("data-stock") || "").toUpperCase();
      const q = quoteMap[stockKey];
      if (!q) return;

      const qty = parseFloat(card.getAttribute("data-qty") || "0");
      const buyPrice = parseFloat(card.getAttribute("data-buy") || "0");
      const cmp = parseFloat(q.current_price || "0");

      const cmpEl = card.querySelector("[data-cmp]");
      if (cmpEl) {
        const prevText = cmpEl.textContent.trim();
        const nextText = q.current_price_formatted || window.formatRupee(cmp);
        if (prevText !== nextText) {
          cmpEl.textContent = nextText;
          cmpEl.classList.remove("price-flash-up", "price-flash-down");
          void cmpEl.offsetWidth;
          cmpEl.classList.add(q.change >= 0 ? "price-flash-up" : "price-flash-down");
        }
      }

      const changeEl = card.querySelector("[data-change]");
      if (changeEl) {
        const isUp = q.change >= 0;
        changeEl.className = "daily-change " + (isUp ? "pos" : "neg");
        changeEl.textContent = `${q.change_pct_formatted || (isUp ? "+" : "") + q.change_pct + "%"}`;
      }

      const curValEl = card.querySelector("[data-curval]");
      if (curValEl && qty > 0) {
        curValEl.textContent = window.formatRupee(qty * cmp);
      }

      const pnlSummaryEl = card.querySelector(".mts-pnl");
      const pnlDetailEl = card.querySelector(".mtd-val[data-pnl]");
      if (qty > 0 && buyPrice > 0) {
        const cost = qty * buyPrice;
        const curVal = qty * cmp;
        const pnl = curVal - cost;
        const pnlPct = cost > 0 ? (pnl / cost * 100) : 0;
        const isUp = pnl >= 0;
        if (pnlSummaryEl) {
          pnlSummaryEl.className = "mts-pnl " + (isUp ? "pos" : "neg");
          pnlSummaryEl.innerHTML = `<strong>${isUp ? "+" : ""}${window.formatRupee(pnl)}</strong><small>${isUp ? "+" : ""}${pnlPct.toFixed(2)}%</small>`;
        }
        if (pnlDetailEl) {
          pnlDetailEl.className = "mtd-val " + (isUp ? "pos" : "neg");
          pnlDetailEl.textContent = `${isUp ? "+" : ""}${window.formatRupee(pnl)} (${pnlPct.toFixed(2)}%)`;
        }
      }
    });

    // 3. Recalculate Portfolio Hero / KPI totals if elements exist
    recalculateTotals();
  }

  function recalculateTotals() {
    let totalInvested = 0;
    let totalCurVal = 0;

    const rows = document.querySelectorAll("#holdings-table tbody tr[data-stock], #portal-holdings-table tbody tr[data-stock]");
    rows.forEach(r => {
      const qty = parseFloat(r.getAttribute("data-qty") || "0");
      const buy = parseFloat(r.getAttribute("data-buy") || "0");
      const curValText = r.querySelector("[data-curval]")?.textContent.replace(/[^0-9.-]/g, "") || "0";
      const curVal = parseFloat(curValText) || (qty * buy);
      totalInvested += (qty * buy);
      totalCurVal += curVal;
    });

    if (totalInvested > 0) {
      const totalPnl = totalCurVal - totalInvested;
      const totalPct = (totalPnl / totalInvested * 100);
      const isUp = totalPnl >= 0;

      // Holdings page KPIs
      const kpiPortVal = document.getElementById("kpi-portfolio-val");
      if (kpiPortVal) kpiPortVal.textContent = window.formatRupee(totalCurVal);

      const kpiPnlVal = document.getElementById("kpi-unrealized-val");
      if (kpiPnlVal) {
        kpiPnlVal.className = "kpi-value " + (isUp ? "pos" : "neg");
        kpiPnlVal.textContent = `${isUp ? "+" : ""}${window.formatRupee(totalPnl)}`;
      }

      const kpiPnlPct = document.getElementById("kpi-unrealized-pct");
      if (kpiPnlPct) {
        kpiPnlPct.className = "stat-pill " + (isUp ? "pos" : "neg");
        kpiPnlPct.textContent = `${isUp ? "+" : ""}${totalPct.toFixed(2)}%`;
      }

      // Portal page hero KPI
      const portalPortVal = document.getElementById("portal-portfolio-val");
      if (portalPortVal) portalPortVal.textContent = window.formatRupee(totalCurVal);

      const portalPnlSub = document.getElementById("portal-unrealized-sub");
      if (portalPnlSub) {
        portalPnlSub.innerHTML = `
          <span class="pill ${isUp ? 'pill-pos' : 'pill-neg'}">${isUp ? '+' : ''}${window.formatRupee(totalPnl)} (${isUp ? '+' : ''}${totalPct.toFixed(2)}%)</span>
          <small class="muted">Total Unrealized P&amp;L</small>
        `;
      }
    }
  }

  function startPolling() {
    stopPolling();
    if (isMarketOpenIST()) {
      updateBadgeUI(true);
      timerId = setInterval(() => {
        pollLiveQuotes(false);
      }, 20000); // 20-second interval
    } else {
      updateBadgeUI(false);
    }
  }

  function stopPolling() {
    if (timerId) {
      clearInterval(timerId);
      timerId = null;
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopPolling();
    } else {
      if (isMarketOpenIST()) {
        if (Date.now() - lastFetchTime >= 18000) {
          pollLiveQuotes(false);
        }
        startPolling();
      } else {
        updateBadgeUI(false);
      }
    }
  });

  // Start ticker
  startPolling();

  return {
    pollNow: () => pollLiveQuotes(true),
    stop: stopPolling,
    start: startPolling
  };
};




