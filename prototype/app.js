const state = {
    filename: "",
    fields: [],
    pdfUrl: "",
    localPdfUrl: "",
    pageCount: 0,
    averageConfidence: null,
};

const $ = (selector) => document.querySelector(selector);
const uploadView = $("#upload-view");
const reviewView = $("#review-view");
const uploadZone = $("#upload-zone");
const pdfInput = $("#pdf-input");
const fieldList = $("#field-list");
const processingOverlay = $("#processing-overlay");
const processingMessage = $("#processing-message");
const toast = $("#toast");

const deleteIcon = `
    <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M8 3h8l1 2h4v2H3V5h4l1-2Zm1 6h2v9H9V9Zm4 0h2v9h-2V9ZM6 9h2v11h8V9h2v11a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V9Z"/>
    </svg>`;

function uid() {
    return window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
}

function canonical(value) {
    return String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
}

function dedupe(fields) {
    const seen = new Set();
    return fields.filter((field) => {
        const marker = `${canonical(field.label)}::${String(field.value || "").trim().toLowerCase()}`;
        if (seen.has(marker)) return false;
        seen.add(marker);
        return true;
    });
}

function showToast(message, type = "success") {
    toast.textContent = message;
    toast.className = `toast visible ${type}`;
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => {
        toast.className = "toast";
    }, 3600);
}

function setStep(step) {
    const order = ["upload", "review", "export"];
    const activeIndex = order.indexOf(step);
    document.querySelectorAll(".sidebar-item").forEach((item) => {
        const index = order.indexOf(item.dataset.step);
        item.classList.toggle("active", index === activeIndex);
        item.classList.toggle("completed", index < activeIndex);
        const indicator = item.querySelector(".step-indicator");
        indicator.textContent = index < activeIndex ? "✓" : String(index + 1);
    });
}

function updateStats(status = "Waiting") {
    $("#stat-status").textContent = status;
    $("#stat-pages").textContent = state.pageCount || "—";
    $("#stat-fields").textContent = state.fields.length || "—";
    $("#stat-confidence").textContent = state.averageConfidence == null
        ? "—"
        : `${state.averageConfidence}%`;
}

function confidenceClass(confidence) {
    if (confidence == null) return "medium";
    if (confidence >= 90) return "high";
    if (confidence >= 75) return "medium";
    return "low";
}

function fieldCard(field) {
    const card = document.createElement("article");
    card.className = "field-card";
    card.dataset.id = field.id;

    const nameInput = document.createElement("input");
    nameInput.className = "field-input field-name";
    nameInput.value = field.label || "Unnamed field";
    nameInput.setAttribute("aria-label", "Field name");
    nameInput.addEventListener("input", () => { field.label = nameInput.value; });

    const valueInput = document.createElement("input");
    valueInput.className = "field-input field-value";
    valueInput.value = field.value ?? "";
    valueInput.placeholder = "No value extracted";
    valueInput.setAttribute("aria-label", `${field.label || "Field"} value`);
    valueInput.addEventListener("input", () => { field.value = valueInput.value; });

    const meta = document.createElement("div");
    meta.className = "field-meta";
    if (field.confidence != null) {
        const confidence = document.createElement("span");
        confidence.className = `confidence ${confidenceClass(field.confidence)}`;
        confidence.textContent = `${Math.round(field.confidence)}%`;
        confidence.title = "OCR confidence";
        meta.appendChild(confidence);
    }

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "delete-field";
    remove.title = "Delete field";
    remove.setAttribute("aria-label", `Delete ${field.label || "field"}`);
    remove.innerHTML = deleteIcon;
    remove.addEventListener("click", () => {
        state.fields = state.fields.filter((item) => item.id !== field.id);
        renderFields();
    });
    meta.appendChild(remove);

    card.append(nameInput, valueInput, meta);
    return card;
}

function renderFields() {
    const query = $("#field-search").value.trim().toLowerCase();
    fieldList.replaceChildren();
    let visible = 0;

    state.fields.forEach((field) => {
        const card = fieldCard(field);
        const matches = !query || `${field.label} ${field.value}`.toLowerCase().includes(query);
        card.hidden = !matches;
        if (matches) visible += 1;
        fieldList.appendChild(card);
    });

    if (!state.fields.length) {
        const empty = document.createElement("div");
        empty.className = "empty-fields";
        empty.innerHTML = "<div><strong>No fields left</strong><br>Add a new field or upload another document.</div>";
        fieldList.appendChild(empty);
    }

    $("#visible-field-count").textContent = visible;
    updateStats(state.fields.length ? "Reviewing" : "No fields");
}

function showReview(data) {
    state.filename = data.filename;
    state.fields = dedupe(data.fields || []).map((field) => ({ ...field, id: field.id || uid() }));
    state.pdfUrl = data.pdf_url;
    state.pageCount = data.page_count;
    state.averageConfidence = data.average_confidence;

    $("#document-name").textContent = state.filename;
    $("#document-meta").textContent = `${state.pageCount} page${state.pageCount === 1 ? "" : "s"} · ${state.fields.length} unique fields`;
    $("#pdf-frame").src = `${state.pdfUrl}#toolbar=1&navpanes=0&view=FitH`;
    $("#open-pdf").href = state.pdfUrl;
    $("#field-search").value = "";
    uploadView.classList.remove("active");
    reviewView.classList.add("active");
    setStep("review");
    renderFields();
}

async function uploadPdf(file) {
    if (!file || !(file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf"))) {
        showToast("Please select a valid PDF file.", "error");
        return;
    }
    if (file.size > 30 * 1024 * 1024) {
        showToast("The PDF exceeds the 30 MB prototype limit.", "error");
        return;
    }

    if (state.localPdfUrl) URL.revokeObjectURL(state.localPdfUrl);
    state.localPdfUrl = URL.createObjectURL(file);
    processingOverlay.hidden = false;
    updateStats("Extracting");

    const messages = [
        "Rendering PDF pages…",
        "Reading form labels and values…",
        "Removing duplicate fields…",
        "Preparing the review workspace…",
    ];
    let messageIndex = 0;
    processingMessage.textContent = messages[0];
    const messageTimer = window.setInterval(() => {
        messageIndex = Math.min(messageIndex + 1, messages.length - 1);
        processingMessage.textContent = messages[messageIndex];
    }, 2400);

    try {
        const form = new FormData();
        form.append("file", file, file.name);
        const response = await fetch("/api/extract", { method: "POST", body: form });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Extraction failed.");
        showReview(data);
        showToast(`${data.field_count} unique fields extracted from ${data.page_count} page(s).`);
    } catch (error) {
        updateStats("Failed");
        showToast(error.message || "Could not extract the PDF.", "error");
    } finally {
        window.clearInterval(messageTimer);
        processingOverlay.hidden = true;
        pdfInput.value = "";
    }
}

async function approveAndDownload(button) {
    if (!state.fields.length) {
        showToast("Add at least one field before approval.", "error");
        return;
    }
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = "Preparing Excel…";

    try {
        const response = await fetch("/api/export", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ filename: state.filename, fields: state.fields }),
        });
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.error || "Excel export failed.");
        }
        const blob = await response.blob();
        const disposition = response.headers.get("Content-Disposition") || "";
        const match = disposition.match(/filename="?([^";]+)"?/i);
        const filename = match?.[1] || "approved_fields.xlsx";
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
        setStep("export");
        updateStats("Approved");
        showToast("Approved Excel downloaded successfully.");
    } catch (error) {
        showToast(error.message || "Could not download Excel.", "error");
    } finally {
        button.disabled = false;
        button.textContent = originalText;
    }
}

function resetWorkspace() {
    state.filename = "";
    state.fields = [];
    state.pdfUrl = "";
    state.pageCount = 0;
    state.averageConfidence = null;
    $("#pdf-frame").src = "about:blank";
    reviewView.classList.remove("active");
    uploadView.classList.add("active");
    setStep("upload");
    updateStats("Waiting");
}

pdfInput.addEventListener("change", () => uploadPdf(pdfInput.files[0]));
["dragenter", "dragover"].forEach((eventName) => {
    uploadZone.addEventListener(eventName, (event) => {
        event.preventDefault();
        uploadZone.classList.add("dragover");
    });
});
["dragleave", "drop"].forEach((eventName) => {
    uploadZone.addEventListener(eventName, (event) => {
        event.preventDefault();
        uploadZone.classList.remove("dragover");
    });
});
uploadZone.addEventListener("drop", (event) => uploadPdf(event.dataTransfer.files[0]));

$("#field-search").addEventListener("input", renderFields);
$("#add-field").addEventListener("click", () => {
    state.fields.unshift({ id: uid(), label: "New field", value: "", page: null, confidence: null });
    $("#field-search").value = "";
    renderFields();
    fieldList.querySelector(".field-card .field-name")?.focus();
});
$("#new-document").addEventListener("click", resetWorkspace);
$("#approve-button").addEventListener("click", (event) => approveAndDownload(event.currentTarget));
$("#approve-button-footer").addEventListener("click", (event) => approveAndDownload(event.currentTarget));

const savedTheme = localStorage.getItem("bv-theme");
if (savedTheme === "light") document.body.classList.add("light");
$("#theme-toggle").addEventListener("click", () => {
    document.body.classList.toggle("light");
    localStorage.setItem("bv-theme", document.body.classList.contains("light") ? "light" : "dark");
});

updateStats();
