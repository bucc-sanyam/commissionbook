(() => {
  "use strict";
  const configElement = document.getElementById("upload-config");
  if (!configElement) return;
  const config = JSON.parse(configElement.textContent);
  const $ = (id) => document.getElementById(id);
  const columns = [
    { key: "stock", label: "Stock", type: "text" },
    { key: "quantity", label: "Quantity", type: "number" },
    { key: "buy_price", label: "Buy price", type: "number" },
    { key: "sell_price", label: "Sell price", type: "number" },
    { key: "buy_date", label: "Buy date", type: "date" },
    { key: "sell_date", label: "Sell date", type: "date" },
  ];
  const state = {
    images: [], busy: false, gridDirty: false, platform: "manual",
    submissionId: newId(), pendingPayload: null, complete: false,
  };
  const maxImages = 10;
  const maxFileSize = 10 * 1024 * 1024;
  const safeUploadSize = 3500 * 1024;
  const allowedTypes = new Set(["image/png", "image/jpeg", "image/webp"]);
  const notify = (message, error = false) => window.BookUI ? window.BookUI.notify(message, error) : alert(message);

  function newId() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  function status(message, error = false) {
    const el = $("scan-status");
    if (!el) return;
    el.textContent = message;
    el.classList.toggle("error", error);
  }

  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
  }

  function authorization() {
    return { portal_token: config.portalToken || "", code: $("code")?.value.trim() || "" };
  }

  class RequestError extends Error {
    constructor(message, statusCode = 0) {
      super(message);
      this.statusCode = statusCode;
    }
  }

  async function requestJSON(url, data) {
    let response;
    try {
      response = await fetch(url, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
        body: JSON.stringify(data), signal: AbortSignal.timeout(60000),
      });
    } catch (error) {
      throw new RequestError(error.name === "TimeoutError" || error.name === "AbortError"
        ? "The request timed out. Please check your connection and try again."
        : "We couldn't reach the workspace. Please check your connection and try again.");
    }
    if (!response.headers.get("content-type")?.includes("application/json")) {
      throw new RequestError(response.status === 413
        ? "This request is too large. Reduce the number of rows or use smaller images."
        : `The workspace returned an unexpected response (HTTP ${response.status}). Please try again.`,
      response.status);
    }
    const result = await response.json();
    if (!response.ok) throw new RequestError(result.error || "The workspace couldn't process this request.", response.status);
    return result;
  }

  function setBusy(busy, label = "") {
    state.busy = busy;
    const fields = $("upload-fields");
    if (fields) fields.disabled = busy || Boolean(state.pendingPayload);
    const drop = $("drop");
    if (drop) drop.setAttribute("aria-disabled", String(busy || Boolean(state.pendingPayload)));
    const retry = $("retry-submit");
    if (retry) retry.disabled = busy;
    if (label) $("submission-result").textContent = label;
  }

  function markEdited() {
    state.gridDirty = true;
    const rev = $("reviewed");
    if (rev) rev.checked = false;
  }

  function rowData(row) {
    const value = {};
    row.querySelectorAll("input[data-column]").forEach((input) => { value[input.dataset.column] = input.value.trim(); });
    return value;
  }

  function populated(data) {
    return Object.values(data).some((value) => value !== "");
  }

  function checkRow(row, show = true) {
    const data = rowData(row);
    const errors = {};
    if (!populated(data)) {
      if (show) {
        row.querySelectorAll("input").forEach((input) => {
          input.classList.remove("bad");
          input.setAttribute("aria-invalid", "false");
          input.setCustomValidity("");
          input.title = "";
        });
      }
      return { data, errors };
    }
    if (!data.stock) errors.stock = "Enter the stock name.";
    if (!data.quantity || !Number.isFinite(Number(data.quantity)) || Number(data.quantity) <= 0) {
      errors.quantity = "Quantity must be greater than zero.";
    }
    if (!data.buy_price && !data.sell_price) {
      errors.buy_price = "Enter a buy or sell price.";
      errors.sell_price = "Enter a buy or sell price.";
    }
    for (const side of ["buy", "sell"]) {
      const price = data[`${side}_price`];
      if (price && (!Number.isFinite(Number(price)) || Number(price) < 0)) {
        errors[`${side}_price`] = "Price must be zero or greater.";
      }
    }
    if (data.buy_date && data.sell_date && data.sell_date < data.buy_date) {
      errors.sell_date = "Sell date cannot be earlier than buy date.";
    }
    if (show) {
      row.querySelectorAll("input[data-column]").forEach((input) => {
        const message = errors[input.dataset.column] || "";
        input.classList.toggle("bad", Boolean(message));
        input.setAttribute("aria-invalid", String(Boolean(message)));
        input.setCustomValidity(message);
        input.title = message;
      });
    }
    return { data, errors };
  }

  function updateRows() {
    let count = 0, needsReview = 0;
    const tableBody = $("trade-rows");
    if (!tableBody) return;
    [...tableBody.children].forEach((row, index) => {
      row.firstElementChild.textContent = String(index + 1);
      const { data, errors } = checkRow(row, false);
      row.querySelectorAll("input[data-column]").forEach((input) => {
        const colDef = columns.find((c) => c.key === input.dataset.column);
        const label = colDef ? colDef.label : input.dataset.column;
        input.setAttribute("aria-label", `Row ${index + 1}: ${label}`);
      });
      const delBtn = row.querySelector("button");
      if (delBtn) delBtn.setAttribute("aria-label", `Remove row ${index + 1}`);
      if (populated(data)) {
        count += 1;
        if (Object.keys(errors).length) needsReview += 1;
      }
    });
    const summary = $("row-summary");
    if (summary) {
      summary.textContent = count
        ? `${count} trade${count === 1 ? "" : "s"}${needsReview ? ` · ${needsReview} need${needsReview === 1 ? "s" : ""} attention` : " · Ready for your review"}`
        : "Enter your first trade.";
    }
  }

  function addRow(data = {}, manual = false) {
    const tableBody = $("trade-rows");
    if (!tableBody) return;
    const row = document.createElement("tr");
    const indexCell = document.createElement("td");
    indexCell.className = "idx";
    row.append(indexCell);
    columns.forEach((column) => {
      const cell = document.createElement("td");
      cell.dataset.label = column.label;
      const input = document.createElement("input");
      input.type = column.type;
      input.dataset.column = column.key;
      if (column.type === "number") {
        input.step = "any";
        input.min = column.key === "quantity" ? "0.000001" : "0";
        input.inputMode = "decimal";
      } else if (column.type === "text") {
        input.maxLength = 120;
        input.autocomplete = "off";
        if (column.key === "stock") {
          input.style.textTransform = "uppercase";
          input.placeholder = "e.g. RELIANCE";
        }
      }
      input.value = data[column.key] ?? "";
      input.addEventListener("input", () => {
        markEdited();
        checkRow(row);
        updateRows();
      });
      cell.append(input);
      row.append(cell);
    });
    const removeCell = document.createElement("td");
    removeCell.className = "idx";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "row-delete";
    remove.textContent = "×";
    remove.addEventListener("click", () => {
      row.remove();
      if (!tableBody.children.length) addRow();
      markEdited();
      updateRows();
    });
    removeCell.append(remove);
    row.append(removeCell);
    tableBody.append(row);
    checkRow(row);
    updateRows();
    if (manual) {
      markEdited();
      row.querySelector("input")?.focus();
    }
  }

  $("add-row")?.addEventListener("click", () => addRow({}, true));

  function loadImage(file) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const image = new Image();
      image.onload = () => {
        URL.revokeObjectURL(url);
        if (!image.naturalWidth || !image.naturalHeight || image.naturalWidth * image.naturalHeight > 40000000) {
          reject(new Error("This image is too large to process safely. Please resize or crop it."));
        } else resolve(image);
      };
      image.onerror = () => {
        URL.revokeObjectURL(url);
        reject(new Error("This image couldn't be opened. Please choose a valid PNG, JPEG or WebP screenshot."));
      };
      image.src = url;
    });
  }

  function makeThumbnail(item) {
    const thumbnail = document.createElement("div");
    thumbnail.className = "thumb";
    const image = document.createElement("img");
    item.previewURL = URL.createObjectURL(item.file);
    image.src = item.previewURL;
    image.alt = item.file.name || "Trade proof screenshot";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "thumb-remove";
    remove.textContent = "×";
    remove.setAttribute("aria-label", `Remove ${item.file.name || "screenshot"}`);
    const caption = document.createElement("div");
    caption.className = "thumb-caption";
    const name = document.createElement("strong");
    name.textContent = item.file.name || "Screenshot";
    name.title = name.textContent;
    const label = document.createElement("small");
    label.textContent = "Proof attached";
    item.label = label;
    caption.append(name, label);
    thumbnail.append(image, remove, caption);
    item.element = thumbnail;
    $("thumbs").append(thumbnail);
    remove.addEventListener("click", () => {
      if (state.busy || state.pendingPayload) return;
      state.images = state.images.filter((img) => img !== item);
      URL.revokeObjectURL(item.previewURL);
      thumbnail.remove();
      const rev = $("reviewed");
      if (rev) rev.checked = false;
      status(state.images.length
        ? `${state.images.length} proof screenshot${state.images.length === 1 ? "" : "s"} attached.`
        : "Screenshot removed.");
    });
  }

  function validateIdentity() {
    const nameInput = $("name");
    if (!nameInput.value.trim()) {
      nameInput.setCustomValidity("Please enter the client's full name.");
      nameInput.reportValidity();
      return false;
    }
    nameInput.setCustomValidity("");
    const phoneInput = $("phone");
    if (!phoneInput.value.trim()) {
      phoneInput.setCustomValidity("Please enter the client's phone number.");
      phoneInput.reportValidity();
      return false;
    }
    phoneInput.setCustomValidity("");
    if (config.needCode && $("code") && !$("code").value.trim()) {
      $("code").setCustomValidity("Enter the upload code shared with you.");
      $("code").reportValidity();
      return false;
    }
    return true;
  }

  $("name")?.addEventListener("input", () => { $("name").setCustomValidity(""); markEdited(); });
  $("phone")?.addEventListener("input", () => { $("phone").setCustomValidity(""); markEdited(); });
  $("code")?.addEventListener("input", () => $("code").setCustomValidity(""));

  function handleFiles(fileList) {
    if (state.busy || state.pendingPayload) {
      notify("Please wait until the current operation finishes.", true);
      return;
    }
    const files = Array.from(fileList || []);
    if (!files.length) return;
    if (config.needCode && $("code") && !$("code").value.trim()) {
      status("Enter your upload code before adding screenshots.", true);
      $("code").focus();
      return;
    }
    if (state.images.length + files.length > maxImages) {
      status(`Please select no more than ${maxImages} screenshots per submission.`, true);
      return;
    }
    const invalid = files.find((file) => !allowedTypes.has(file.type) || !file.size || file.size > maxFileSize);
    if (invalid) {
      status(`"${invalid.name || "This file"}" must be a PNG, JPEG or WebP image under 10 MB.`, true);
      return;
    }
    const fileKey = (file) => `${file.name}:${file.size}:${file.lastModified}`;
    const existing = new Set(state.images.map((item) => fileKey(item.file)));
    const uniqueFiles = files.filter((file) => {
      const key = fileKey(file);
      if (existing.has(key)) return false;
      existing.add(key);
      return true;
    });
    if (!uniqueFiles.length) {
      status("Those screenshots are already attached.");
      return;
    }
    const newItems = uniqueFiles.map((file) => ({ file, receipt: null, uploadFile: null }));
    newItems.forEach((item) => {
      state.images.push(item);
      makeThumbnail(item);
    });
    status(`${state.images.length} screenshot${state.images.length === 1 ? "" : "s"} attached as proof. Click 'Next: Enter trades' to enter your trade details.`);
    const rev = $("reviewed");
    if (rev) rev.checked = false;
    $("files").value = "";
  }

  const dropZone = $("drop");
  if (dropZone) {
    dropZone.addEventListener("click", () => {
      if (!state.busy && !state.pendingPayload) $("files")?.click();
    });
    dropZone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        if (!state.busy && !state.pendingPayload) $("files")?.click();
      }
    });
    ["dragenter", "dragover"].forEach((name) => {
      dropZone.addEventListener(name, (event) => { event.preventDefault(); dropZone.classList.add("drag"); });
    });
    ["dragleave", "drop"].forEach((name) => {
      dropZone.addEventListener(name, (event) => { event.preventDefault(); dropZone.classList.remove("drag"); });
    });
    dropZone.addEventListener("drop", (event) => handleFiles(event.dataTransfer.files));
  }
  $("files")?.addEventListener("change", () => handleFiles($("files").files));
  document.addEventListener("paste", (event) => {
    const files = Array.from(event.clipboardData?.items || []).filter((item) => item.kind === "file").map((item) => item.getAsFile()).filter(Boolean);
    if (files.length) {
      event.preventDefault();
      handleFiles(files);
    }
  });

  async function storageFile(item) {
    if (item.uploadFile) return item.uploadFile;
    if (item.file.size <= safeUploadSize) {
      item.uploadFile = item.file;
      return item.file;
    }
    const image = await loadImage(item.file);
    const canvas = document.createElement("canvas");
    const scale = Math.min(1, 3000 / Math.max(image.naturalWidth, image.naturalHeight));
    canvas.width = Math.round(image.naturalWidth * scale);
    canvas.height = Math.round(image.naturalHeight * scale);
    const context = canvas.getContext("2d");
    context.fillStyle = "#fff";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    let compressed = null;
    for (const quality of [.9, .78, .65]) {
      compressed = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", quality));
      if (compressed && compressed.size <= safeUploadSize) break;
    }
    canvas.width = 0;
    canvas.height = 0;
    if (!compressed || compressed.size > safeUploadSize) {
      throw new Error(`"${item.file.name}" is too large to send. Please compress or resize it.`);
    }
    item.uploadFile = new File([compressed], `${item.file.name.replace(/\.[^.]+$/, "") || "screenshot"}.jpg`, { type: "image/jpeg" });
    return item.uploadFile;
  }

  async function storeImages() {
    for (const [index, item] of state.images.entries()) {
      if (item.receipt) continue;
      $("submission-result").textContent = `Uploading proof screenshot ${index + 1} of ${state.images.length}...`;
      const file = await storageFile(item);
      const ticket = await requestJSON(config.signUrl, {
        ...authorization(), filename: file.name, content_type: file.type, size: file.size,
      });
      if (!ticket.upload_url || !ticket.path || !ticket.receipt) {
        throw new Error("The workspace couldn't prepare a secure image upload.");
      }
      const uploadUrl = new URL(ticket.upload_url, window.location.origin);
      const local = uploadUrl.origin === window.location.origin;
      const response = await fetch(uploadUrl, {
        method: ticket.method || "PUT",
        credentials: local ? "same-origin" : "omit",
        headers: {
          "Content-Type": file.type,
          ...(ticket.headers || {}),
          ...(local ? { "X-CSRF-Token": csrfToken() } : {}),
        },
        body: file, signal: AbortSignal.timeout(120000),
      });
      if (!response.ok) {
        throw new RequestError(`Screenshot ${index + 1} couldn't be saved (HTTP ${response.status}). Please try again.`, response.status);
      }
      item.receipt = { path: ticket.path, receipt: ticket.receipt };
      if (item.label) item.label.textContent = "Uploaded proof";
    }
  }

  function validateRows() {
    const rows = [];
    let firstInvalid = null;
    const tableBody = $("trade-rows");
    if (!tableBody) return null;
    [...tableBody.children].forEach((row) => {
      const { data, errors } = checkRow(row);
      if (!populated(data)) return;
      if (Object.keys(errors).length && !firstInvalid) firstInvalid = row.querySelector(".bad");
      rows.push(data);
    });
    updateRows();
    if (!rows.length) {
      $("submission-result").textContent = "Add at least one trade before submitting.";
      tableBody.querySelector("input")?.focus();
      return null;
    }
    if (firstInvalid) {
      $("submission-result").textContent = "Please complete the highlighted cells before submitting.";
      firstInvalid.focus();
      firstInvalid.reportValidity();
      return null;
    }
    return rows;
  }

  async function commitSubmission() {
    setBusy(true, "Saving your trades...");
    $("retry-submit").hidden = true;
    try {
      const result = await requestJSON(config.submitUrl, state.pendingPayload);
      if (!result.ok || !Number.isInteger(result.saved)) {
        throw new RequestError("We couldn't confirm the save. Retry safely to verify the same submission.");
      }
      state.complete = true;
      $("upload-form").hidden = true;
      $("submission-success").hidden = false;
      $("success-message").textContent = `${result.saved} trade${result.saved === 1 ? "" : "s"} for ${state.pendingPayload.name} ${result.saved === 1 ? "has" : "have"} been saved. Proof screenshots have been attached for verification.`;
      $("success-reference").textContent = `Reference: ${result.submission_id || state.submissionId}`;
      $("submission-success").focus();
    } catch (error) {
      const knownRejection = error instanceof RequestError && error.statusCode >= 400 && error.statusCode < 500;
      if (knownRejection) {
        state.pendingPayload = null;
        $("submission-result").textContent = error.message;
      } else {
        $("submission-result").textContent = `${error.message} Use "Retry save safely" to confirm this submission without creating duplicates.`;
        $("retry-submit").hidden = false;
      }
    } finally {
      setBusy(false);
    }
  }

  $("upload-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.busy || state.pendingPayload) return;
    $("submission-result").textContent = "";
    if (!validateIdentity()) return;
    const rows = validateRows();
    if (!rows) return;
    const rev = $("reviewed");
    if (rev && !rev.checked) {
      $("submission-result").textContent = "Please confirm that you've verified the trade details.";
      rev.focus();
      rev.reportValidity();
      return;
    }
    setBusy(true);
    try {
      await storeImages();
      state.pendingPayload = {
        ...authorization(), name: $("name").value.trim(), phone: $("phone").value.trim(), rows,
        platform: "manual",
        ocr_text: state.images.length ? "Screenshots attached as proof" : "",
        images: state.images.map((item) => item.receipt).filter(Boolean),
        submission_id: state.submissionId,
      };
    } catch (error) {
      $("submission-result").textContent = `${error.message} Nothing has been added to the ledger yet.`;
      setBusy(false);
      return;
    }
    await commitSubmission();
  });

  $("retry-submit")?.addEventListener("click", () => {
    if (!state.busy && state.pendingPayload) commitSubmission();
  });

  $("send-another")?.addEventListener("click", () => {
    state.images.forEach((item) => { if (item.previewURL) URL.revokeObjectURL(item.previewURL); });
    state.images = [];
    state.pendingPayload = null;
    state.submissionId = newId();
    state.gridDirty = false;
    state.complete = false;
    $("thumbs").replaceChildren();
    $("trade-rows").replaceChildren();
    $("submission-success").hidden = true;
    $("upload-form").hidden = false;
    $("submission-result").textContent = "";
    const rev = $("reviewed");
    if (rev) rev.checked = false;
    if ($("files")) $("files").value = "";
    status("");
    addRow();
    setBusy(false);
    if (window.wizardGoto) window.wizardGoto(1);
  });

  $("download-rows")?.addEventListener("click", () => {
    const tableBody = $("trade-rows");
    if (!tableBody) return;
    const rows = [...tableBody.children].map(rowData).filter(populated);
    if (!rows.length) {
      notify("Add some trade details before downloading.");
      return;
    }
    const csvCell = (value) => {
      const text = String(value ?? "");
      const safe = /^[\t\r]|^\s*[=+\-@]/.test(text) ? `'${text}` : text;
      return `"${safe.replace(/"/g, '""')}"`;
    };
    const data = [["Username", ...columns.map((c) => c.label)],
      ...rows.map((row) => [($("name")?.value || "").trim(), ...columns.map((c) => row[c.key])])];
    const blob = new Blob(["\ufeff", data.map((line) => line.map(csvCell).join(",")).join("\r\n")], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "trade-details.csv";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    notify("CSV downloaded.");
  });

  window.addEventListener("beforeunload", (event) => {
    if (!state.complete && (state.images.length || state.gridDirty || state.pendingPayload)) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  window.addEventListener("pagehide", (event) => {
    if (!event.persisted) state.images.forEach((item) => { if (item.previewURL) URL.revokeObjectURL(item.previewURL); });
  });

  // Initialize with one clean empty row
  addRow();
})();
