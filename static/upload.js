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
    images: [], busy: false, gridDirty: false, platform: "generic",
    submissionId: newId(), pendingPayload: null, complete: false,
  };
  let worker = null;
  let loadingWorker = null;
  const maxImages = 10;
  const maxFileSize = 10 * 1024 * 1024;
  const safeUploadSize = 3500 * 1024;
  const allowedTypes = new Set(["image/png", "image/jpeg", "image/webp"]);
  const notify = (message, error = false) => window.BookUI.notify(message, error);

  function newId() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  function status(message, error = false) {
    $("scan-status").textContent = message;
    $("scan-status").classList.toggle("error", error);
  }
  function csrfToken() {
    return document.querySelector('meta[name="csrf-token"]').content;
  }
  function authorization() {
    return { portal_token: config.portalToken, code: $("code")?.value.trim() || "" };
  }
  class RequestError extends Error {
    constructor(message, statusCode = 0) {
      super(message);
      this.statusCode = statusCode;
    }
  }
  class ScannerTimeout extends Error {}
  async function scannerDeadline(promise, milliseconds, message) {
    let timer;
    try {
      return await Promise.race([
        promise,
        new Promise((_, reject) => { timer = setTimeout(() => reject(new ScannerTimeout(message)), milliseconds); }),
      ]);
    } finally {
      clearTimeout(timer);
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
    $("upload-fields").disabled = busy || Boolean(state.pendingPayload);
    $("drop").setAttribute("aria-disabled", String(busy || Boolean(state.pendingPayload)));
    $("retry-submit").disabled = busy;
    if (label) $("submission-result").textContent = label;
  }
  function markEdited() {
    state.gridDirty = true;
    $("reviewed").checked = false;
  }
  function setStep(step) {
    ["upload", "review", "send"].forEach((name, index) => {
      const element = $(`step-${name}`);
      if (element) element.classList.toggle("active", index <= step);
    });
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
    if (!data.quantity || !Number.isFinite(Number(data.quantity)) || Number(data.quantity) <= 0) errors.quantity = "Quantity must be greater than zero.";
    if (!data.buy_price && !data.sell_price) {
      errors.buy_price = "Enter a buy or sell price.";
      errors.sell_price = "Enter a buy or sell price.";
    }
    for (const side of ["buy", "sell"]) {
      const price = data[`${side}_price`];
      const date = data[`${side}_date`];
      if (price && (!Number.isFinite(Number(price)) || Number(price) < 0)) errors[`${side}_price`] = "Price must be zero or greater.";
      if (price !== "" && !date) errors[`${side}_date`] = `Enter the ${side} date.`;
      if (date && price === "") errors[`${side}_price`] = `Enter the price for this ${side} date.`;
    }
    if (data.buy_date && data.sell_date && data.sell_date < data.buy_date) errors.sell_date = "Sell date cannot be earlier than buy date.";
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
    [...$("trade-rows").children].forEach((row, index) => {
      row.firstElementChild.textContent = String(index + 1);
      const { data, errors } = checkRow(row, false);
      row.querySelectorAll("input[data-column]").forEach((input) => {
        const label = columns.find((column) => column.key === input.dataset.column).label;
        input.setAttribute("aria-label", `Row ${index + 1}: ${label}`);
      });
      row.querySelector("button").setAttribute("aria-label", `Remove row ${index + 1}`);
      if (populated(data)) {
        count += 1;
        if (Object.keys(errors).length) needsReview += 1;
      }
    });
    $("row-summary").textContent = count
      ? `${count} trade${count === 1 ? "" : "s"}${needsReview ? ` · ${needsReview} need${needsReview === 1 ? "s" : ""} attention` : " · Ready for your review"}`
      : "Add a screenshot or enter your first trade.";
    if (count) setStep(1);
  }
  function addRow(data = {}, manual = false) {
    const row = document.createElement("tr");
    const indexCell = document.createElement("td");
    indexCell.className = "idx";
    row.append(indexCell);
    columns.forEach((column) => {
      const cell = document.createElement("td");
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
      if (!$("trade-rows").children.length) addRow();
      markEdited();
      updateRows();
    });
    removeCell.append(remove);
    row.append(removeCell);
    $("trade-rows").append(row);
    checkRow(row);
    updateRows();
    if (manual) {
      markEdited();
      row.querySelector("input").focus();
    }
  }
  $("add-row").addEventListener("click", () => addRow({}, true));

  function loadImage(file) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const image = new Image();
      image.onload = () => {
        URL.revokeObjectURL(url);
        if (!image.naturalWidth || !image.naturalHeight || image.naturalWidth * image.naturalHeight > 40000000) {
          reject(new Error("This image is too large to process safely. Crop it to the trade details and try again."));
        } else resolve(image);
      };
      image.onerror = () => {
        URL.revokeObjectURL(url);
        reject(new Error("This image couldn't be opened. Please choose a valid PNG, JPEG or WebP screenshot."));
      };
      image.src = url;
    });
  }
  async function preprocess(file) {
    const image = await loadImage(file);
    const scale = Math.min(2.5, 3200 / Math.max(image.naturalWidth, image.naturalHeight),
      Math.sqrt(8000000 / (image.naturalWidth * image.naturalHeight)));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(image.naturalWidth * scale);
    canvas.height = Math.round(image.naturalHeight * scale);
    const context = canvas.getContext("2d", { willReadFrequently: true });
    context.fillStyle = "#fff";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height);
    let brightness = 0;
    for (let index = 0; index < pixels.data.length; index += 4) {
      const gray = .299 * pixels.data[index] + .587 * pixels.data[index + 1] + .114 * pixels.data[index + 2];
      pixels.data[index] = gray;
      brightness += gray;
    }
    const dark = brightness / (pixels.data.length / 4) < 110;
    for (let index = 0; index < pixels.data.length; index += 4) {
      const gray = Math.max(0, Math.min(255, ((dark ? 255 - pixels.data[index] : pixels.data[index]) - 128) * 1.35 + 128));
      pixels.data[index] = pixels.data[index + 1] = pixels.data[index + 2] = gray;
    }
    context.putImageData(pixels, 0, 0);
    return canvas;
  }
  function updateProgress(progress, label = "") {
    const percent = Math.round(progress * 100);
    $("progress-fill").style.width = `${percent}%`;
    $("progress-percent").textContent = `${percent}%`;
    $("progress-track").setAttribute("aria-valuenow", String(percent));
    if (label) $("progress-label").textContent = label;
  }
  async function loadOCR() {
    if (window.Tesseract) return;
    await new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "https://cdn.jsdelivr.net/npm/tesseract.js@5.1.1/dist/tesseract.min.js";
      script.crossOrigin = "anonymous";
      const timeout = setTimeout(() => {
        script.remove();
        reject(new Error("The scanner took too long to load. Check your connection, then reselect your images, or enter trades manually."));
      }, 45000);
      script.onload = () => { clearTimeout(timeout); resolve(); };
      script.onerror = () => {
        clearTimeout(timeout);
        script.remove();
        reject(new Error("The scanner couldn't load. Check your internet connection or enter the trades manually."));
      };
      document.head.append(script);
    });
  }
  async function getWorker() {
    if (worker) return worker;
    if (!loadingWorker) {
      loadingWorker = (async () => {
        updateProgress(0, "Loading the on-device scanner...");
        await loadOCR();
        let expired = false;
        const creating = window.Tesseract.createWorker("eng", 1, {
          workerPath: "https://cdn.jsdelivr.net/npm/tesseract.js@5.1.1/dist/worker.min.js",
          corePath: "https://cdn.jsdelivr.net/npm/tesseract.js-core@5.1.1",
          logger: (message) => {
            if (message.status === "recognizing text") updateProgress(message.progress);
          },
        }).then((created) => {
          if (expired) created.terminate();
          return created;
        });
        let created;
        try {
          created = await scannerDeadline(creating, 60000, "The scanner could not start in time. Try again with a smaller screenshot.");
        } catch (error) {
          expired = true;
          throw error;
        }
        await created.setParameters({ preserve_interword_spaces: "1" });
        worker = created;
        return worker;
      })();
    }
    try {
      return await loadingWorker;
    } finally {
      loadingWorker = null;
    }
  }
  function makeThumbnail(item) {
    const thumbnail = document.createElement("div");
    thumbnail.className = "thumb";
    const image = document.createElement("img");
    item.previewURL = URL.createObjectURL(item.file);
    image.src = item.previewURL;
    image.alt = item.file.name || "Trade screenshot";
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
    label.textContent = "Waiting to scan";
    item.label = label;
    caption.append(name, label);
    thumbnail.append(image, remove, caption);
    item.element = thumbnail;
    $("thumbs").append(thumbnail);
    remove.addEventListener("click", () => {
      if (state.busy || state.pendingPayload) return;
      state.images = state.images.filter((imageItem) => imageItem !== item);
      URL.revokeObjectURL(item.previewURL);
      thumbnail.remove();
      $("reviewed").checked = false;
      updateRawText();
      status("Screenshot removed. Existing table rows are kept; use Re-extract if you want to rebuild them.");
    });
  }
  function updateRawText() {
    $("ocr-text").textContent = state.images.length
      ? state.images.map((item, index) => `--- Screenshot ${index + 1}: ${item.file.name || "image"} ---\n${item.text || "(No text extracted. Enter this trade manually.)"}`).join("\n\n")
      : "Scanned text will appear here. No AI service is involved.";
    $("reparse").hidden = !state.images.some((image) => image.text);
  }
  function validateIdentity() {
    if (!$("name").value.trim()) {
      $("name").setCustomValidity("Please enter the client's full name.");
      $("name").reportValidity();
      return false;
    }
    $("name").setCustomValidity("");
    if (!$("phone").value.trim()) {
      $("phone").setCustomValidity("Please enter the client's phone number.");
      $("phone").reportValidity();
      return false;
    }
    $("phone").setCustomValidity("");
    if (config.needCode && !$("code").value.trim()) {
      $("code").setCustomValidity("Enter the upload code shared with you.");
      $("code").reportValidity();
      return false;
    }
    return true;
  }
  $("name").addEventListener("input", () => { $("name").setCustomValidity(""); $("reviewed").checked = false; });
  $("phone").addEventListener("input", () => { $("phone").setCustomValidity(""); $("reviewed").checked = false; });
  $("code")?.addEventListener("input", () => $("code").setCustomValidity(""));

  async function parseImages() {
    const text = state.images.filter((image) => image.text).map((image) => image.text).join("\n\n");
    if (!text) {
      status("No text was read from these images. You can still enter the trades manually.", true);
      return;
    }
    const result = await requestJSON(config.parseUrl, { ...authorization(), text });
    if (!Array.isArray(result.trades)) throw new Error("The scanner returned an unreadable result. Please enter the rows manually.");
    $("trade-rows").replaceChildren();
    result.trades.forEach((row) => addRow(row));
    if (!result.trades.length) addRow();
    state.platform = result.platform || "generic";
    state.gridDirty = false;
    $("reviewed").checked = false;
    $("platform-badge").textContent = `${state.platform === "generic" ? "Generic" : state.platform === "zerodha" ? "Zerodha" : "Groww"} layout`;
    $("platform-badge").hidden = false;
    const warnings = Array.isArray(result.warnings) ? result.warnings.filter((warning) => typeof warning === "string") : [];
    status((result.trades.length
      ? `Found ${result.trades.length} trade${result.trades.length === 1 ? "" : "s"}. Check every row, especially the highlighted cells, before sending.`
      : "We couldn't identify trade rows in this layout. Your images are still attached; enter the details in the table below.") +
      (warnings.length ? ` ${warnings.join(" ")}` : ""),
    !result.trades.length);
    setStep(1);
  }
  async function handleFiles(fileList) {
    if (state.busy || state.pendingPayload) {
      notify("Please finish the current scan or save before adding more images.", true);
      return;
    }
    const files = Array.from(fileList);
    if (!files.length) return;
    if (config.needCode && !$("code").value.trim()) {
      status("Enter your upload code before adding screenshots.", true);
      $("code").focus();
      return;
    }
    if (state.images.length + files.length > maxImages) {
      status(`Please select no more than ${maxImages} screenshots per submission. Nothing from this selection was added.`, true);
      return;
    }
    const invalid = files.find((file) => !allowedTypes.has(file.type) || !file.size || file.size > maxFileSize);
    if (invalid) {
      status(`"${invalid.name || "This image"}" must be a PNG, JPEG or WebP screenshot under 10 MB. Nothing from this selection was added.`, true);
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
      status("Those files are already attached. Select a different screenshot or check the existing rows.");
      return;
    }
    const newItems = uniqueFiles.map((file) => ({ file, text: "", receipt: null, uploadFile: null }));
    newItems.forEach((item) => { state.images.push(item); makeThumbnail(item); });
    setBusy(true);
    $("scan-progress").hidden = false;
    $("reviewed").checked = false;
    status("");
    try {
      const scanner = await getWorker();
      for (const [index, item] of newItems.entries()) {
        item.label.textContent = "Reading on your device...";
        updateProgress(0, `Reading screenshot ${index + 1} of ${newItems.length}...`);
        try {
          const canvas = await preprocess(item.file);
          const { data } = await scannerDeadline(scanner.recognize(canvas), 90000, "Reading this screenshot took too long. Crop it to the order details, then try again.");
          item.text = data.text;
          item.label.textContent = data.text.trim() ? "Scanned · ready to review" : "No readable text";
          canvas.width = 0;
          canvas.height = 0;
        } catch (error) {
          item.label.textContent = "Couldn't read · enter manually";
          item.label.title = error.message;
          if (error instanceof ScannerTimeout) {
            await scanner.terminate();
            worker = null;
            throw error;
          }
          status(`A screenshot couldn't be read: ${error.message} You can enter its trade manually.`, true);
        }
      }
      updateRawText();
      if (state.gridDirty) {
        status("New screenshots scanned. Your edits are untouched. Use Re-extract to rebuild the table from all screenshots, or add the new trades manually.");
      } else {
        await parseImages();
      }
    } catch (error) {
      newItems.filter((item) => !item.text).forEach((item) => { item.label.textContent = "Attached · manual entry needed"; });
      status(`${error.message} Your images are still attached. You can remove and reselect them to retry, or enter the rows manually.`, true);
      updateRawText();
    } finally {
      $("scan-progress").hidden = true;
      setBusy(false);
      $("files").value = "";
    }
  }
  $("drop").addEventListener("click", () => {
    if (!state.busy && !state.pendingPayload) $("files").click();
  });
  $("drop").addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (!state.busy && !state.pendingPayload) $("files").click();
    }
  });
  $("files").addEventListener("change", () => handleFiles($("files").files));
  ["dragenter", "dragover"].forEach((name) => {
    $("drop").addEventListener(name, (event) => { event.preventDefault(); $("drop").classList.add("drag"); });
  });
  ["dragleave", "drop"].forEach((name) => {
    $("drop").addEventListener(name, (event) => { event.preventDefault(); $("drop").classList.remove("drag"); });
  });
  $("drop").addEventListener("drop", (event) => handleFiles(event.dataTransfer.files));
  document.addEventListener("paste", (event) => {
    const files = Array.from(event.clipboardData?.items || []).filter((item) => item.kind === "file").map((item) => item.getAsFile()).filter(Boolean);
    if (files.length) {
      event.preventDefault();
      handleFiles(files);
    }
  });
  $("reparse").addEventListener("click", () => $("reparse-dialog").showModal());
  $("confirm-reparse").addEventListener("click", async () => {
    $("reparse-dialog").close();
    setBusy(true);
    try { await parseImages(); }
    catch (error) { status(error.message, true); }
    finally { setBusy(false); }
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
    if (!compressed || compressed.size > safeUploadSize) throw new Error(`"${item.file.name}" is too large to send. Crop the screenshot and try again.`);
    item.uploadFile = new File([compressed], `${item.file.name.replace(/\.[^.]+$/, "") || "screenshot"}.jpg`, { type: "image/jpeg" });
    return item.uploadFile;
  }
  async function storeImages() {
    for (const [index, item] of state.images.entries()) {
      if (item.receipt) continue;
      $("submission-result").textContent = `Saving screenshot ${index + 1} of ${state.images.length}...`;
      const file = await storageFile(item);
      const ticket = await requestJSON(config.signUrl, {
        ...authorization(), filename: file.name, content_type: file.type, size: file.size,
      });
      if (!ticket.upload_url || !ticket.path || !ticket.receipt) throw new Error("The workspace couldn't prepare a secure image upload.");
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
      if (!response.ok) throw new RequestError(`Screenshot ${index + 1} couldn't be saved (HTTP ${response.status}). Please try again.`, response.status);
      item.receipt = { path: ticket.path, receipt: ticket.receipt };
      item.label.textContent = "Uploaded privately";
    }
  }
  function validateRows() {
    const rows = [];
    let firstInvalid = null;
    [...$("trade-rows").children].forEach((row) => {
      const { data, errors } = checkRow(row);
      if (!populated(data)) return;
      if (Object.keys(errors).length && !firstInvalid) firstInvalid = row.querySelector(".bad");
      rows.push(data);
    });
    updateRows();
    if (!rows.length) {
      $("submission-result").textContent = "Add at least one trade before sending.";
      $("trade-rows").querySelector("input")?.focus();
      return null;
    }
    if (firstInvalid) {
      $("submission-result").textContent = "Please correct the highlighted cells. All rows must be complete before anything is saved.";
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
      if (!result.ok || !Number.isInteger(result.saved)) throw new RequestError("We couldn't confirm the save. Retry safely to check the same submission.");
      state.complete = true;
      $("upload-form").hidden = true;
      $("submission-success").hidden = false;
      $("success-message").textContent = `${result.saved} trade${result.saved === 1 ? "" : "s"} for ${state.pendingPayload.name} ${result.saved === 1 ? "has" : "have"} been saved. The workspace administrator can now see the details and attached screenshots.`;
      $("success-reference").textContent = `Reference: ${result.submission_id || state.submissionId}`;
      $("submission-success").focus();
      setStep(2);
    } catch (error) {
      const knownRejection = error instanceof RequestError && error.statusCode >= 400 && error.statusCode < 500;
      if (knownRejection) {
        state.pendingPayload = null;
        $("submission-result").textContent = error.message;
      } else {
        $("submission-result").textContent = `${error.message} Don't re-enter these trades. Use "Retry save safely" to confirm this exact submission without creating duplicates.`;
        $("retry-submit").hidden = false;
      }
    } finally {
      setBusy(false);
    }
  }
  $("upload-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.busy || state.pendingPayload) return;
    $("submission-result").textContent = "";
    if (!validateIdentity()) return;
    const rows = validateRows();
    if (!rows) return;
    if (!$("reviewed").checked) {
      $("submission-result").textContent = "Please confirm that you've checked the trade details before sending.";
      $("reviewed").focus();
      $("reviewed").reportValidity();
      return;
    }
    setBusy(true);
    try {
      await storeImages();
      state.pendingPayload = {
        ...authorization(), name: $("name").value.trim(), phone: $("phone").value.trim(), rows,
        platform: state.images.length ? state.platform : "manual",
        ocr_text: state.images.map((item) => item.text).join("\n\n"),
        images: state.images.map((item) => item.receipt),
        submission_id: state.submissionId,
      };
    } catch (error) {
      $("submission-result").textContent = `${error.message} Your trade rows are unchanged; nothing has been added to the ledger yet.`;
      setBusy(false);
      return;
    }
    await commitSubmission();
  });
  $("retry-submit").addEventListener("click", () => {
    if (!state.busy && state.pendingPayload) commitSubmission();
  });
  $("send-another").addEventListener("click", () => {
    state.images.forEach((item) => URL.revokeObjectURL(item.previewURL));
    state.images = [];
    state.pendingPayload = null;
    state.submissionId = newId();
    state.gridDirty = false;
    state.complete = false;
    state.platform = "generic";
    $("thumbs").replaceChildren();
    $("trade-rows").replaceChildren();
    $("submission-success").hidden = true;
    $("upload-form").hidden = false;
    $("submission-result").textContent = "";
    $("reviewed").checked = false;
    $("platform-badge").hidden = true;
    $("files").value = "";
    status("");
    updateRawText();
    addRow();
    setBusy(false);
    setStep(0);
    $("drop").focus();
  });
  $("download-rows").addEventListener("click", () => {
    const rows = [...$("trade-rows").children].map(rowData).filter(populated);
    if (!rows.length) {
      notify("Add some trade details before downloading.");
      return;
    }
    const csvCell = (value) => {
      const text = String(value ?? "");
      const safe = /^[\t\r]|^\s*[=+\-@]/.test(text) ? `'${text}` : text;
      return `"${safe.replace(/"/g, '""')}"`;
    };
    const data = [["Username", ...columns.map((column) => column.label)],
      ...rows.map((row) => [$("name").value.trim(), ...columns.map((column) => row[column.key])])];
    const blob = new Blob(["\ufeff", data.map((line) => line.map(csvCell).join(",")).join("\r\n")], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "trade-details.csv";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    notify("CSV downloaded. You can open it in Excel.");
  });
  window.addEventListener("beforeunload", (event) => {
    if (!state.complete && (state.images.length || state.gridDirty || state.pendingPayload)) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  window.addEventListener("pagehide", (event) => {
    if (worker) {
      worker.terminate();
      worker = null;
    }
    if (!event.persisted) state.images.forEach((item) => URL.revokeObjectURL(item.previewURL));
  });
  addRow();
})();
