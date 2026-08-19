import {
  GlobalWorkerOptions,
  TextLayer,
  getDocument,
} from "./vendor/pdfjs/build/pdf.mjs";
import {
  groupSelectionRects,
  pdfQuadToViewportRect,
} from "./reader-geometry.mjs";

GlobalWorkerOptions.workerSrc = new URL(
  "./vendor/pdfjs/build/pdf.worker.mjs",
  import.meta.url,
).href;

const root = document.querySelector("[data-reader]");
const DEFAULT_HIGHLIGHT_GUIDANCE = "Select text in the document, then choose Highlight selection.";

function clamp(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, value));
}

function pageLabel(highlight) {
  const pages = highlight.fragments.map((fragment) => fragment.page_number);
  const first = Math.min(...pages);
  const last = Math.max(...pages);
  return first === last ? `Page ${first}` : `Pages ${first}–${last}`;
}

function errorMessage(error) {
  if (error?.message) return error.message;
  return "The operation could not be completed.";
}

class CortexPdfReader {
  constructor(element) {
    this.root = element;
    this.scrim = document.querySelector("[data-reader-scrim]");
    this.workspace = element.querySelector(".reader-workspace");
    this.viewport = element.querySelector("[data-reader-viewport]");
    this.pagesHost = element.querySelector("[data-reader-pages]");
    this.status = element.querySelector("[data-reader-status]");
    this.textLayerNotice = element.querySelector("[data-text-layer-notice]");
    this.selectionAction = element.querySelector("[data-selection-action]");
    this.highlightList = element.querySelector("[data-highlight-list]");
    this.highlightCount = element.querySelector("[data-highlight-count]");
    this.highlightGuidance = element.querySelector("[data-highlight-guidance]");
    this.paperNoteInput = element.querySelector("[data-paper-note]");
    this.paperNoteStatus = element.querySelector("[data-paper-note-status]");
    this.paperNoteRetry = element.querySelector("[data-paper-note-retry]");
    this.documentSelect = element.querySelector("[data-document-select]");
    this.staleBanner = element.querySelector("[data-stale-banner]");
    this.pageInput = element.querySelector("[data-page-input]");
    this.pageTotal = element.querySelector("[data-page-total]");
    this.resizeHandle = element.querySelector("[data-reader-resize]");
    this.paperId = element.dataset.paperId;
    this.csrfToken = element.dataset.csrfToken;
    this.currentDocument = null;
    this.pdf = null;
    this.loadingTask = null;
    this.pageObserver = null;
    this.pageStates = new Map();
    this.highlights = [];
    this.paperNote = { body: "", revision: 0 };
    this.pendingSelection = null;
    this.zoomFactor = 1;
    this.currentPage = 1;
    this.openedByPush = false;
    this.opener = null;
    this.saveTimers = new Map();
    this.backgroundElements = [...document.querySelectorAll(
      "body > .site-header, body > .site-footer, #main-content > :not(.reader-drawer):not(.reader-scrim)",
    )];
    this.requestedHighlightId = Number(element.dataset.requestedHighlightId || 0);
    this.workspace.dataset.mobilePanel = "document";
    this.restoreDrawerWidth();
    this.bindEvents();
  }

  bindEvents() {
    document.querySelectorAll("[data-reader-open]").forEach((button) => {
      button.addEventListener("click", () => this.open({ updateHistory: true, opener: button }));
    });
    this.root.querySelector("[data-reader-close]").addEventListener("click", () => this.close());
    this.scrim.addEventListener("click", () => this.close());
    this.root.querySelector("[data-page-previous]").addEventListener("click", () => {
      this.goToPage(this.currentPage - 1);
    });
    this.root.querySelector("[data-page-next]").addEventListener("click", () => {
      this.goToPage(this.currentPage + 1);
    });
    this.pageInput.addEventListener("change", () => this.goToPage(Number(this.pageInput.value)));
    this.pageInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        this.goToPage(Number(this.pageInput.value));
      }
    });
    this.root.querySelector("[data-zoom-in]").addEventListener("click", () => this.setZoom(this.zoomFactor * 1.15));
    this.root.querySelector("[data-zoom-out]").addEventListener("click", () => this.setZoom(this.zoomFactor / 1.15));
    this.root.querySelector("[data-fit-width]").addEventListener("click", () => this.setZoom(1));
    this.root.querySelector("[data-open-latest]").addEventListener("click", () => {
      this.runLoadAction(() => this.ensureLatest());
    });
    this.documentSelect.addEventListener("change", () => {
      this.runLoadAction(() => this.loadAnnotations(Number(this.documentSelect.value)));
    });
    this.selectionAction.addEventListener("click", () => this.createHighlight());
    this.viewport.addEventListener("mouseup", () => window.setTimeout(() => this.captureSelection(), 0));
    this.viewport.addEventListener("keyup", () => window.setTimeout(() => this.captureSelection(), 0));
    this.viewport.addEventListener("scroll", () => this.updateCurrentPage(), { passive: true });
    this.root.querySelectorAll("[data-reader-tab]").forEach((button) => {
      button.addEventListener("click", () => this.selectMobilePanel(button.dataset.readerTab));
      button.addEventListener("keydown", (event) => this.handleTabKeydown(event));
    });
    this.paperNoteInput.addEventListener("input", () => this.queuePaperNoteSave());
    this.paperNoteRetry.addEventListener("click", () => this.retryPaperNote());
    this.root.addEventListener("keydown", (event) => this.handleKeydown(event));
    this.bindResize();
    window.addEventListener("popstate", () => {
      const requested = new URL(window.location.href).searchParams.get("reader") === "1";
      if (requested && this.root.hidden) this.open({ updateHistory: false });
      if (!requested && !this.root.hidden) this.hide();
    });
    let resizeTimer;
    window.addEventListener("resize", () => {
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(() => {
        this.syncMobilePanelA11y();
        this.syncResizeA11y();
        this.rerenderVisiblePages();
      }, 180);
    });
    this.syncMobilePanelA11y();
    this.syncResizeA11y();
  }

  async open({ updateHistory = false, opener = null } = {}) {
    if (!this.root.hidden) return;
    this.opener = opener || document.activeElement;
    this.root.hidden = false;
    this.scrim.hidden = false;
    this.backgroundElements.forEach((element) => { element.inert = true; });
    document.body.classList.add("reader-open");
    this.root.classList.add("reader-entering");
    window.setTimeout(() => this.root.classList.remove("reader-entering"), 220);
    this.root.querySelector("[data-reader-close]").focus();
    if (updateHistory) {
      const url = new URL(window.location.href);
      url.searchParams.set("reader", "1");
      this.openedByPush = true;
      window.history.pushState({ cortexReader: true }, "", url);
    }
    try {
      const selectedId = Number(this.root.dataset.selectedDocumentId || this.documentSelect.value || 0);
      if (selectedId) {
        await this.loadAnnotations(selectedId);
      } else {
        await this.ensureLatest();
      }
    } catch (error) {
      this.showError(errorMessage(error));
    }
  }

  close() {
    if (this.openedByPush) {
      this.openedByPush = false;
      window.history.back();
      return;
    }
    const url = new URL(window.location.href);
    url.searchParams.delete("reader");
    url.searchParams.delete("document");
    url.searchParams.delete("highlight");
    window.history.replaceState({}, "", url);
    this.hide();
  }

  hide() {
    this.root.hidden = true;
    this.scrim.hidden = true;
    this.selectionAction.hidden = true;
    document.body.classList.remove("reader-open");
    this.backgroundElements.forEach((element) => { element.inert = false; });
    this.opener?.focus?.();
  }

  async ensureLatest() {
    this.showStatus("Caching the PDF", "The first open stores a private local copy for stable highlights.");
    const response = await this.postJson(this.root.dataset.ensureUrl, {});
    const document = response.document;
    this.addDocumentOption(document);
    this.root.dataset.selectedDocumentId = String(document.id);
    await this.loadAnnotations(document.id);
  }

  async loadAnnotations(documentId) {
    this.showStatus("Loading annotations", "Restoring the passages tied to this PDF version.");
    const data = await this.fetchAnnotations(documentId);
    this.currentDocument = data.document;
    this.highlights = data.highlights;
    this.paperNote = data.paper_note;
    this.documentSelect.value = String(documentId);
    this.root.dataset.selectedDocumentId = String(documentId);
    this.staleBanner.hidden = !data.document.stale;
    this.restorePaperNoteDraft();
    this.renderHighlightList();
    this.updateReaderUrl();
    await this.loadPdf(data.document);
  }

  async loadPdf(document) {
    this.loadingTask?.destroy?.();
    this.pageObserver?.disconnect();
    this.pageStates.clear();
    this.pagesHost.replaceChildren();
    this.textLayerNotice.hidden = true;
    this.textLayerNotice.textContent = "";
    this.highlightGuidance.textContent = DEFAULT_HIGHLIGHT_GUIDANCE;
    this.showStatus("Opening the PDF", "Building the selectable reading layer.");
    this.loadingTask = getDocument({
      url: document.content_url,
      cMapUrl: new URL("./vendor/pdfjs/web/cmaps/", import.meta.url).href,
      cMapPacked: true,
      standardFontDataUrl: new URL("./vendor/pdfjs/web/standard_fonts/", import.meta.url).href,
      wasmUrl: new URL("./vendor/pdfjs/web/wasm/", import.meta.url).href,
      iccUrl: new URL("./vendor/pdfjs/web/iccs/", import.meta.url).href,
      isEvalSupported: false,
    });
    this.pdf = await this.loadingTask.promise;
    const fingerprint = this.pdf.fingerprints?.[0] || "";
    if (document.pdf_fingerprint && fingerprint && document.pdf_fingerprint !== fingerprint) {
      throw new Error("The cached PDF identity changed. Re-cache the source before annotating it.");
    }
    this.pageTotal.textContent = `/ ${this.pdf.numPages}`;
    this.pageInput.value = "1";
    this.currentPage = 1;
    this.pageObserver = new IntersectionObserver(
      (entries) => {
        entries.filter((entry) => entry.isIntersecting).forEach((entry) => {
          this.renderPage(Number(entry.target.dataset.pageNumber));
        });
      },
      { root: this.viewport, rootMargin: "900px 0px" },
    );
    for (let pageNumber = 1; pageNumber <= this.pdf.numPages; pageNumber += 1) {
      const shell = this.createPageShell(pageNumber);
      this.pagesHost.append(shell);
      this.pageObserver.observe(shell);
    }
    await this.renderPage(1);
    this.status.hidden = true;
    if (this.requestedHighlightId) {
      const target = this.requestedHighlightId;
      this.requestedHighlightId = 0;
      await this.scrollToHighlight(target);
    }
  }

  createPageShell(pageNumber) {
    const shell = document.createElement("article");
    shell.className = "reader-page";
    shell.dataset.pageNumber = String(pageNumber);
    shell.setAttribute("aria-label", `PDF page ${pageNumber}`);
    const number = document.createElement("span");
    number.className = "reader-page-number";
    number.textContent = String(pageNumber);
    shell.append(number);
    return shell;
  }

  async renderPage(pageNumber) {
    const shell = this.pagesHost.querySelector(`[data-page-number="${pageNumber}"]`);
    if (!shell || shell.dataset.rendered === "true") return this.pageStates.get(pageNumber);
    if (shell.renderPromise) return shell.renderPromise;
    shell.renderPromise = (async () => {
      const page = await this.pdf.getPage(pageNumber);
      const baseViewport = page.getViewport({ scale: 1 });
      const pageSpace = Math.max(260, this.viewport.clientWidth - (window.innerWidth <= 700 ? 16 : 54));
      const fitScale = Math.min(2.4, pageSpace / baseViewport.width);
      const viewport = page.getViewport({ scale: fitScale * this.zoomFactor });
      shell.style.minHeight = "";
      shell.style.width = `${Math.round(viewport.width)}px`;
      shell.style.height = `${Math.round(viewport.height)}px`;
      const content = document.createElement("div");
      content.className = "reader-page-content";
      content.style.width = `${viewport.width}px`;
      content.style.height = `${viewport.height}px`;
      const canvas = document.createElement("canvas");
      const outputScale = window.devicePixelRatio || 1;
      canvas.width = Math.floor(viewport.width * outputScale);
      canvas.height = Math.floor(viewport.height * outputScale);
      canvas.style.width = `${viewport.width}px`;
      canvas.style.height = `${viewport.height}px`;
      content.append(canvas);
      const textLayerElement = document.createElement("div");
      textLayerElement.className = "textLayer";
      content.append(textLayerElement);
      const highlightLayer = document.createElement("div");
      highlightLayer.className = "reader-highlight-layer";
      content.append(highlightLayer);
      shell.prepend(content);
      const state = { page, viewport, shell, content, textLayerElement, highlightLayer };
      this.pageStates.set(pageNumber, state);
      await page.render({
        canvasContext: canvas.getContext("2d", { alpha: false }),
        transform: outputScale === 1 ? null : [outputScale, 0, 0, outputScale, 0, 0],
        viewport,
      }).promise;
      const textLayer = new TextLayer({
        textContentSource: page.streamTextContent({ includeMarkedContent: true }),
        container: textLayerElement,
        viewport,
      });
      await textLayer.render();
      state.textLayer = textLayer;
      shell.dataset.rendered = "true";
      this.renderOverlays(pageNumber);
      if (pageNumber === 1 && textLayer.textDivs.length === 0) {
        const message = "This PDF has no selectable text. Paper notes remain available; text highlighting requires OCR, which is not included.";
        this.highlightGuidance.textContent = message;
        this.textLayerNotice.textContent = message;
        this.textLayerNotice.hidden = false;
      }
      return state;
    })().catch((error) => {
      shell.renderPromise = null;
      throw error;
    });
    return shell.renderPromise;
  }

  async rerenderVisiblePages() {
    if (!this.pdf || this.root.hidden) return;
    const rendered = [...this.pagesHost.querySelectorAll('.reader-page[data-rendered="true"]')];
    const visible = rendered
      .filter((shell) => {
        const rect = shell.getBoundingClientRect();
        const viewportRect = this.viewport.getBoundingClientRect();
        return rect.bottom > viewportRect.top && rect.top < viewportRect.bottom;
      })
      .map((shell) => Number(shell.dataset.pageNumber));
    for (const shell of rendered) {
      const pageNumber = Number(shell.dataset.pageNumber);
      const replacement = this.createPageShell(pageNumber);
      replacement.style.minHeight = `${shell.getBoundingClientRect().height}px`;
      this.pageObserver.unobserve(shell);
      shell.replaceWith(replacement);
      this.pageObserver.observe(replacement);
      this.pageStates.delete(pageNumber);
    }
    await Promise.all(visible.map((pageNumber) => this.renderPage(pageNumber)));
  }

  setZoom(value) {
    this.zoomFactor = clamp(value, 0.55, 2.5);
    this.rerenderVisiblePages();
  }

  captureSelection() {
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) {
      this.pendingSelection = null;
      this.selectionAction.hidden = true;
      return;
    }
    const anchor = selection.anchorNode?.nodeType === Node.ELEMENT_NODE
      ? selection.anchorNode
      : selection.anchorNode?.parentElement;
    if (!anchor?.closest?.(".textLayer") || !this.root.contains(anchor)) return;
    const range = selection.getRangeAt(0);
    const selectionRects = [...range.getClientRects()];
    const pages = [...this.pageStates.entries()].map(([pageNumber, state]) => ({
      pageNumber,
      rotation: state.viewport.rotation,
      viewport: state.viewport,
      bounds: state.content.getBoundingClientRect(),
    }));
    const fragments = groupSelectionRects(selectionRects, pages);
    const quote = selection.toString().trim().split(/\s+/).join(" ");
    if (!quote || !fragments.length) return;
    this.pendingSelection = { quote, fragments };
    const lastRect = selectionRects.at(-1);
    const buttonWidth = 142;
    this.selectionAction.style.left = `${clamp(lastRect.left, 8, window.innerWidth - buttonWidth - 8)}px`;
    this.selectionAction.style.top = `${clamp(lastRect.bottom + 8, 8, window.innerHeight - 48)}px`;
    this.selectionAction.hidden = false;
  }

  async createHighlight() {
    if (!this.pendingSelection || !this.currentDocument) return;
    this.selectionAction.disabled = true;
    this.selectionAction.textContent = "Saving…";
    try {
      const response = await this.postJson(this.root.dataset.createHighlightUrl, {
        document_id: this.currentDocument.id,
        quote: this.pendingSelection.quote,
        fragments: this.pendingSelection.fragments,
        client_request_id: crypto.randomUUID(),
        pdf_fingerprint: this.pdf.fingerprints?.[0] || "",
        page_count: this.pdf.numPages,
      });
      this.highlights.unshift(response.highlight);
      this.renderHighlightList(response.highlight.id);
      response.highlight.fragments.forEach((fragment) => this.renderOverlays(fragment.page_number));
      window.getSelection()?.removeAllRanges();
      this.pendingSelection = null;
      this.selectionAction.hidden = true;
      this.selectMobilePanel("notes");
      this.highlightList.querySelector(`[data-highlight-id="${response.highlight.id}"] textarea`)?.focus();
    } catch (error) {
      this.selectionAction.textContent = errorMessage(error);
      window.setTimeout(() => {
        this.selectionAction.textContent = "Highlight selection";
      }, 2200);
    } finally {
      this.selectionAction.disabled = false;
    }
  }

  renderHighlightList(selectedId = 0) {
    this.highlightList.replaceChildren();
    this.highlightCount.textContent = String(this.highlights.length);
    this.highlightGuidance.hidden = this.highlights.length > 0;
    for (const highlight of this.highlights) {
      const card = document.createElement("article");
      card.className = "reader-highlight-card";
      card.dataset.highlightId = String(highlight.id);
      card.setAttribute("aria-current", highlight.id === selectedId ? "true" : "false");
      const quoteButton = document.createElement("button");
      quoteButton.type = "button";
      quoteButton.className = "reader-highlight-quote";
      quoteButton.id = `reader-highlight-${highlight.id}-quote`;
      const meta = document.createElement("span");
      meta.textContent = `${pageLabel(highlight)} · ${highlight.revision_label}`;
      const quote = document.createElement("q");
      quote.textContent = highlight.quote;
      quoteButton.append(meta, quote);
      quoteButton.addEventListener("click", () => this.scrollToHighlight(highlight.id));
      const editor = document.createElement("div");
      editor.className = "reader-highlight-editor";
      const textarea = document.createElement("textarea");
      textarea.rows = 3;
      textarea.placeholder = "Add your note alongside this passage…";
      textarea.setAttribute("aria-labelledby", quoteButton.id);
      textarea.value = this.restoreDraft(`highlight:${highlight.id}`, highlight.note);
      const actions = document.createElement("div");
      actions.className = "reader-highlight-actions";
      const saveStatus = document.createElement("span");
      saveStatus.setAttribute("aria-live", "polite");
      const retryButton = document.createElement("button");
      retryButton.type = "button";
      retryButton.className = "reader-retry-save";
      retryButton.textContent = "Retry save";
      retryButton.hidden = true;
      retryButton.addEventListener("click", () => this.retryHighlightNote(
        highlight,
        textarea,
        saveStatus,
        retryButton,
      ));
      if (textarea.value !== highlight.note) {
        saveStatus.textContent = "Unsaved draft recovered";
        retryButton.hidden = false;
      }
      const deleteButton = document.createElement("button");
      deleteButton.type = "button";
      deleteButton.textContent = "Delete highlight";
      deleteButton.addEventListener("click", () => this.deleteHighlight(highlight));
      const actionButtons = document.createElement("div");
      actionButtons.append(retryButton, deleteButton);
      actions.append(saveStatus, actionButtons);
      editor.append(textarea, actions);
      card.append(quoteButton, editor);
      textarea.addEventListener("input", () => {
        const key = `highlight:${highlight.id}`;
        this.storeDraft(key, textarea.value);
        saveStatus.textContent = "Unsaved";
        retryButton.hidden = true;
        retryButton.dataset.conflict = "false";
        window.clearTimeout(this.saveTimers.get(key));
        this.saveTimers.set(key, window.setTimeout(
          () => this.saveHighlightNote(highlight, textarea, saveStatus, retryButton),
          650,
        ));
      });
      this.highlightList.append(card);
    }
  }

  async saveHighlightNote(highlight, textarea, status, retryButton) {
    status.textContent = "Saving…";
    try {
      const response = await this.postJson(`${this.root.dataset.createHighlightUrl}/${highlight.id}`, {
        note: textarea.value,
        revision: highlight.revision,
      });
      Object.assign(highlight, response.highlight);
      this.clearDraft(`highlight:${highlight.id}`);
      status.textContent = "Saved";
      retryButton.hidden = true;
      retryButton.dataset.conflict = "false";
      window.setTimeout(() => { if (status.textContent === "Saved") status.textContent = ""; }, 1600);
    } catch (error) {
      status.textContent = error.status === 409 ? "Conflict — your draft is safe" : errorMessage(error);
      retryButton.textContent = error.status === 409 ? "Reload & retry" : "Retry save";
      retryButton.dataset.conflict = String(error.status === 409);
      retryButton.hidden = false;
    }
  }

  async retryHighlightNote(highlight, textarea, status, retryButton) {
    if (retryButton.dataset.conflict === "true") {
      status.textContent = "Loading latest version…";
      try {
        const data = await this.fetchAnnotations(this.currentDocument.id);
        const latest = data.highlights.find((item) => item.id === highlight.id);
        if (!latest) throw new Error("This highlight no longer exists.");
        highlight.revision = latest.revision;
        highlight.note = latest.note;
      } catch (error) {
        status.textContent = errorMessage(error);
        return;
      }
    }
    await this.saveHighlightNote(highlight, textarea, status, retryButton);
  }

  async deleteHighlight(highlight) {
    if (!window.confirm("Delete this highlight and its note?")) return;
    try {
      await this.postJson(`${this.root.dataset.createHighlightUrl}/${highlight.id}/delete`, {});
      this.highlights = this.highlights.filter((item) => item.id !== highlight.id);
      this.clearDraft(`highlight:${highlight.id}`);
      this.renderHighlightList();
      highlight.fragments.forEach((fragment) => this.renderOverlays(fragment.page_number));
    } catch (error) {
      window.alert(errorMessage(error));
    }
  }

  renderOverlays(pageNumber) {
    const state = this.pageStates.get(pageNumber);
    if (!state) return;
    state.highlightLayer.replaceChildren();
    for (const highlight of this.highlights) {
      for (const fragment of highlight.fragments.filter((item) => item.page_number === pageNumber)) {
        for (const quad of fragment.quads) {
          const rect = pdfQuadToViewportRect(state.viewport, quad);
          const mark = document.createElement("span");
          mark.className = "reader-highlight-mark";
          mark.dataset.highlightMark = String(highlight.id);
          mark.style.left = `${rect.left}px`;
          mark.style.top = `${rect.top}px`;
          mark.style.width = `${rect.width}px`;
          mark.style.height = `${rect.height}px`;
          state.highlightLayer.append(mark);
        }
      }
    }
  }

  async scrollToHighlight(highlightId) {
    const highlight = this.highlights.find((item) => item.id === Number(highlightId));
    if (!highlight) return;
    this.selectMobilePanel("document");
    const pageNumber = Math.min(...highlight.fragments.map((fragment) => fragment.page_number));
    const state = await this.renderPage(pageNumber);
    state.shell.scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    this.currentPage = pageNumber;
    this.pageInput.value = String(pageNumber);
    this.highlightList.querySelectorAll("[data-highlight-id]").forEach((card) => {
      card.setAttribute("aria-current", card.dataset.highlightId === String(highlightId) ? "true" : "false");
    });
    window.setTimeout(() => {
      const marks = state.highlightLayer.querySelectorAll(`[data-highlight-mark="${highlightId}"]`);
      marks.forEach((mark) => mark.classList.add("is-target"));
      window.setTimeout(() => marks.forEach((mark) => mark.classList.remove("is-target")), 1000);
    }, 240);
    this.updateReaderUrl(highlightId);
  }

  queuePaperNoteSave() {
    const key = "paper-note";
    this.storeDraft(key, this.paperNoteInput.value);
    this.paperNoteStatus.textContent = "Unsaved";
    this.paperNoteRetry.hidden = true;
    window.clearTimeout(this.saveTimers.get(key));
    this.saveTimers.set(key, window.setTimeout(() => this.savePaperNote(), 650));
  }

  async savePaperNote() {
    this.paperNoteStatus.textContent = "Saving…";
    try {
      const response = await this.postJson(this.root.dataset.noteUrl, {
        body: this.paperNoteInput.value,
        revision: this.paperNote.revision || 0,
      });
      this.paperNote = response.paper_note;
      this.paperNoteInput.dataset.revision = String(this.paperNote.revision);
      this.clearDraft("paper-note");
      this.paperNoteStatus.textContent = "Saved";
      this.paperNoteRetry.hidden = true;
      window.setTimeout(() => {
        if (this.paperNoteStatus.textContent === "Saved") this.paperNoteStatus.textContent = "";
      }, 1600);
    } catch (error) {
      this.paperNoteStatus.textContent = error.status === 409 ? "Conflict — your draft is safe" : errorMessage(error);
      this.paperNoteRetry.textContent = error.status === 409 ? "Reload & retry" : "Retry save";
      this.paperNoteRetry.dataset.conflict = String(error.status === 409);
      this.paperNoteRetry.hidden = false;
    }
  }

  async retryPaperNote() {
    if (this.paperNoteRetry.dataset.conflict === "true") {
      this.paperNoteStatus.textContent = "Loading latest version…";
      try {
        const data = await this.fetchAnnotations(this.currentDocument.id);
        this.paperNote.revision = data.paper_note.revision;
      } catch (error) {
        this.paperNoteStatus.textContent = errorMessage(error);
        return;
      }
    }
    await this.savePaperNote();
  }

  restorePaperNoteDraft() {
    this.paperNoteInput.value = this.restoreDraft("paper-note", this.paperNote.body || "");
    this.paperNoteInput.dataset.revision = String(this.paperNote.revision || 0);
    const draftRecovered = this.paperNoteInput.value !== (this.paperNote.body || "");
    this.paperNoteStatus.textContent = draftRecovered ? "Unsaved draft recovered" : "";
    this.paperNoteRetry.hidden = !draftRecovered;
    this.paperNoteRetry.textContent = "Retry save";
    this.paperNoteRetry.dataset.conflict = "false";
  }

  draftKey(key) {
    return `arxiv-cortex:${this.paperId}:${key}`;
  }

  restoreDraft(key, fallback) {
    try {
      return window.localStorage.getItem(this.draftKey(key)) ?? fallback;
    } catch (_error) {
      return fallback;
    }
  }

  storeDraft(key, value) {
    try { window.localStorage.setItem(this.draftKey(key), value); } catch (_error) { /* storage is optional */ }
  }

  clearDraft(key) {
    try { window.localStorage.removeItem(this.draftKey(key)); } catch (_error) { /* storage is optional */ }
  }

  goToPage(pageNumber) {
    if (!this.pdf) return;
    const page = clamp(Number.isFinite(pageNumber) ? pageNumber : 1, 1, this.pdf.numPages);
    this.renderPage(page).then((state) => state.shell.scrollIntoView({ block: "start" }));
    this.currentPage = page;
    this.pageInput.value = String(page);
  }

  updateCurrentPage() {
    const viewportTop = this.viewport.getBoundingClientRect().top;
    const nearest = [...this.pagesHost.querySelectorAll(".reader-page")]
      .map((shell) => ({
        page: Number(shell.dataset.pageNumber),
        distance: Math.abs(shell.getBoundingClientRect().top - viewportTop - 12),
      }))
      .sort((a, b) => a.distance - b.distance)[0];
    if (nearest) {
      this.currentPage = nearest.page;
      this.pageInput.value = String(nearest.page);
    }
  }

  selectMobilePanel(panel) {
    this.workspace.dataset.mobilePanel = panel;
    this.syncMobilePanelA11y();
    const activePanel = document.activeElement?.closest?.("[data-reader-panel]");
    if (
      window.matchMedia("(max-width: 1100px)").matches
      && activePanel
      && activePanel.dataset.readerPanel !== panel
    ) {
      (panel === "document" ? this.viewport : this.paperNoteInput).focus();
    }
  }

  syncMobilePanelA11y() {
    const selectedPanel = this.workspace.dataset.mobilePanel || "document";
    const mobile = window.matchMedia("(max-width: 1100px)").matches;
    this.root.querySelectorAll("[data-reader-tab]").forEach((button) => {
      const selected = button.dataset.readerTab === selectedPanel;
      button.setAttribute("aria-selected", selected ? "true" : "false");
      button.tabIndex = selected ? 0 : -1;
    });
    this.root.querySelectorAll("[data-reader-panel]").forEach((panel) => {
      const inactive = mobile && panel.dataset.readerPanel !== selectedPanel;
      panel.setAttribute("aria-hidden", inactive ? "true" : "false");
    });
  }

  handleTabKeydown(event) {
    const tabs = [...this.root.querySelectorAll("[data-reader-tab]")];
    const currentIndex = tabs.indexOf(event.currentTarget);
    let nextIndex = currentIndex;
    if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % tabs.length;
    else if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = tabs.length - 1;
    else return;
    event.preventDefault();
    const next = tabs[nextIndex];
    this.selectMobilePanel(next.dataset.readerTab);
    next.focus();
  }

  async runLoadAction(action) {
    const previousState = {
      document: this.currentDocument,
      highlights: this.highlights,
      paperNote: this.paperNote,
      paperNoteValue: this.paperNoteInput.value,
      selectedDocumentId: this.root.dataset.selectedDocumentId,
      selectValue: this.documentSelect.value,
      staleHidden: this.staleBanner.hidden,
      url: window.location.href,
    };
    try {
      await action();
    } catch (error) {
      let restoreError = null;
      if (previousState.document?.id) {
        try {
          await this.loadAnnotations(previousState.document.id);
        } catch (caughtRestoreError) {
          restoreError = caughtRestoreError;
        }
      }
      if (restoreError || !previousState.document?.id) {
        this.currentDocument = previousState.document;
        this.highlights = previousState.highlights;
        this.paperNote = previousState.paperNote;
        this.paperNoteInput.value = previousState.paperNoteValue;
        this.root.dataset.selectedDocumentId = previousState.selectedDocumentId;
        this.documentSelect.value = previousState.selectValue;
        this.staleBanner.hidden = previousState.staleHidden;
        this.renderHighlightList();
      }
      window.history.replaceState(window.history.state, "", previousState.url);
      this.selectMobilePanel("document");
      const message = restoreError
        ? `${errorMessage(error)} The previous PDF version could not be restored: ${errorMessage(restoreError)}`
        : errorMessage(error);
      this.showError(message);
    }
  }

  addDocumentOption(documentRecord) {
    let option = this.documentSelect.querySelector(`option[value="${documentRecord.id}"]`);
    if (!option) {
      option = window.document.createElement("option");
      option.value = String(documentRecord.id);
      option.textContent = documentRecord.revision_label;
      this.documentSelect.prepend(option);
    }
    this.documentSelect.value = String(documentRecord.id);
  }

  updateReaderUrl(highlightId = 0) {
    if (this.root.hidden || !this.currentDocument) return;
    const url = new URL(window.location.href);
    url.searchParams.set("reader", "1");
    url.searchParams.set("document", String(this.currentDocument.id));
    if (highlightId) url.searchParams.set("highlight", String(highlightId));
    else url.searchParams.delete("highlight");
    window.history.replaceState(window.history.state, "", url);
  }

  handleKeydown(event) {
    if ((event.metaKey || event.ctrlKey) && event.shiftKey && event.key.toLowerCase() === "h") {
      event.preventDefault();
      this.captureSelection();
      if (this.pendingSelection) this.createHighlight();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      this.close();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = [...this.root.querySelectorAll(
      'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    )].filter((item) => item.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  bindResize() {
    this.resizeHandle.addEventListener("pointerdown", (event) => {
      if (window.innerWidth <= 1100) return;
      event.preventDefault();
      this.resizeHandle.setPointerCapture(event.pointerId);
      const startX = event.clientX;
      const startWidth = this.root.getBoundingClientRect().width;
      const move = (moveEvent) => {
        const width = clamp(startWidth + startX - moveEvent.clientX, 760, window.innerWidth - 28);
        this.setDrawerWidth(width);
      };
      const finish = () => {
        this.resizeHandle.removeEventListener("pointermove", move);
        this.resizeHandle.removeEventListener("pointerup", finish);
        this.rerenderVisiblePages();
      };
      this.resizeHandle.addEventListener("pointermove", move);
      this.resizeHandle.addEventListener("pointerup", finish);
    });
    this.resizeHandle.addEventListener("keydown", (event) => {
      if (!new Set(["ArrowLeft", "ArrowRight"]).has(event.key) || window.innerWidth <= 1100) return;
      event.preventDefault();
      const direction = event.key === "ArrowLeft" ? 1 : -1;
      this.setDrawerWidth(clamp(this.root.getBoundingClientRect().width + direction * 24, 760, window.innerWidth - 28));
      this.rerenderVisiblePages();
    });
  }

  setDrawerWidth(width) {
    this.root.style.setProperty("--reader-width", `${width}px`);
    this.resizeHandle.setAttribute("aria-valuenow", String(Math.round(width)));
    this.resizeHandle.setAttribute("aria-valuetext", `${Math.round(width)} pixels wide`);
    try { window.localStorage.setItem("arxiv-cortex:reader-width", String(Math.round(width))); } catch (_error) { /* optional */ }
  }

  restoreDrawerWidth() {
    try {
      const width = Number(window.localStorage.getItem("arxiv-cortex:reader-width"));
      if (width && window.innerWidth > 1100) {
        this.setDrawerWidth(clamp(width, 760, window.innerWidth - 28));
      }
    } catch (_error) { /* optional */ }
    this.syncResizeA11y();
  }

  syncResizeA11y() {
    const maximum = Math.max(760, window.innerWidth - 28);
    const fallback = clamp(Math.min(window.innerWidth * 0.76, 1120), 760, maximum);
    const current = this.root.style.getPropertyValue("--reader-width");
    const currentWidth = Number.parseFloat(current) || fallback;
    this.resizeHandle.setAttribute("aria-valuemax", String(Math.round(maximum)));
    this.resizeHandle.setAttribute("aria-valuenow", String(Math.round(currentWidth)));
    this.resizeHandle.setAttribute("aria-valuetext", `${Math.round(currentWidth)} pixels wide`);
  }

  showStatus(title, detail) {
    this.status.hidden = false;
    this.status.classList.remove("error");
    this.status.querySelector("strong").textContent = title;
    this.status.querySelector("p").textContent = detail;
  }

  showError(message) {
    this.status.hidden = false;
    this.status.classList.add("error");
    this.status.querySelector("strong").textContent = "The PDF could not be opened";
    this.status.querySelector("p").textContent = `${message} You can still open the source PDF in a new tab.`;
  }

  async postJson(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-CSRFToken": this.csrfToken,
      },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const error = new Error(await this.responseError(response));
      error.status = response.status;
      throw error;
    }
    if (response.status === 204) return {};
    return response.json();
  }

  async responseError(response) {
    const contentType = response.headers.get("Content-Type") || "";
    if (contentType.includes("application/json")) {
      const data = await response.json();
      return data.error || `Request failed with HTTP ${response.status}`;
    }
    const text = await response.text();
    const parsed = new DOMParser().parseFromString(text, "text/html");
    return parsed.body.textContent.trim() || `Request failed with HTTP ${response.status}`;
  }

  async fetchAnnotations(documentId) {
    const url = new URL(this.root.dataset.annotationsUrl, window.location.origin);
    url.searchParams.set("document", String(documentId));
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    if (!response.ok) {
      const error = new Error(await this.responseError(response));
      error.status = response.status;
      throw error;
    }
    return response.json();
  }
}

if (root) {
  const reader = new CortexPdfReader(root);
  if (root.dataset.autoOpen === "true") {
    const readerUrl = new URL(window.location.href);
    const paperUrl = new URL(readerUrl);
    paperUrl.searchParams.delete("reader");
    paperUrl.searchParams.delete("document");
    paperUrl.searchParams.delete("highlight");
    window.history.replaceState({}, "", paperUrl);
    window.history.pushState({ cortexReader: true }, "", readerUrl);
    reader.openedByPush = true;
    reader.open({ updateHistory: false });
  }
}
