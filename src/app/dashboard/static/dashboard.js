(function () {
  "use strict";

  const STORAGE_KEY = "voice-inbox.dashboard.kanban.zoom.v1";
  const DEFAULT_SCALE = 1;
  const MIN_SCALE = 0.4;
  const MAX_SCALE = 1.2;
  const SCALE_STEP = 0.05;

  const clamp = (value, minimum, maximum) => Math.min(maximum, Math.max(minimum, value));

  const normalizeScale = (value, minimum = MIN_SCALE, maximum = MAX_SCALE, fallback = DEFAULT_SCALE) => {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) {
      return clamp(fallback, minimum, maximum);
    }
    return Math.round(clamp(parsed, minimum, maximum) * 10000) / 10000;
  };

  const decodeScaleState = (raw, minimum = MIN_SCALE, maximum = MAX_SCALE) => {
    if (raw === null || raw === undefined || raw === "") {
      return { state: { scale: DEFAULT_SCALE, mode: "manual" }, valid: raw === null || raw === undefined };
    }
    try {
      const value = JSON.parse(raw);
      if (!value || typeof value !== "object" || !Number.isFinite(Number(value.scale))) {
        throw new TypeError("Invalid Kanban scale state");
      }
      return {
        state: {
          scale: normalizeScale(value.scale, minimum, maximum),
          mode: value.mode === "fit" ? "fit" : "manual",
        },
        valid: true,
      };
    } catch (_error) {
      return { state: { scale: DEFAULT_SCALE, mode: "manual" }, valid: false };
    }
  };

  const loadScaleState = (storage, minimum = MIN_SCALE, maximum = MAX_SCALE) => {
    try {
      const raw = storage.getItem(STORAGE_KEY);
      const decoded = decodeScaleState(raw, minimum, maximum);
      if (!decoded.valid) {
        storage.setItem(STORAGE_KEY, JSON.stringify(decoded.state));
      }
      return decoded.state;
    } catch (_error) {
      return { scale: DEFAULT_SCALE, mode: "manual" };
    }
  };

  const calculateFitScale = (
    viewportWidth,
    viewportHeight,
    logicalWidth,
    logicalHeight,
    minimum = MIN_SCALE,
    maximum = 1,
  ) => {
    const dimensions = [viewportWidth, viewportHeight, logicalWidth, logicalHeight].map(Number);
    if (dimensions.some((value) => !Number.isFinite(value) || value <= 0)) {
      return normalizeScale(DEFAULT_SCALE, minimum, Math.min(maximum, 1));
    }
    const widthScale = dimensions[0] / dimensions[2];
    const heightScale = dimensions[1] / dimensions[3];
    return normalizeScale(Math.min(widthScale, heightScale, 1), minimum, Math.min(maximum, 1));
  };

  const api = {
    STORAGE_KEY,
    DEFAULT_SCALE,
    MIN_SCALE,
    MAX_SCALE,
    SCALE_STEP,
    normalizeScale,
    decodeScaleState,
    loadScaleState,
    calculateFitScale,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (typeof window !== "undefined") {
    window.KanbanZoom = api;
  }
  if (typeof document === "undefined") {
    return;
  }

  const initDashboard = () => {
    const body = document.body;
    const toggle = document.querySelector("[data-sidebar-toggle]");
    const closeTargets = document.querySelectorAll("[data-sidebar-close]");

    const setSidebar = (open) => {
      body.classList.toggle("sidebar-open", open);
      if (toggle instanceof HTMLElement) {
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
      }
    };

    if (toggle) {
      toggle.addEventListener("click", () => {
        setSidebar(!body.classList.contains("sidebar-open"));
      });
    }

    for (const target of closeTargets) {
      target.addEventListener("click", () => setSidebar(false));
    }

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        setSidebar(false);
      }
    });

    for (const link of document.querySelectorAll(".side-nav a")) {
      link.addEventListener("click", () => setSidebar(false));
    }

    for (const form of document.querySelectorAll("form.filters")) {
      const reset = form.querySelector(".filter-actions a");
      if (!reset) {
        continue;
      }
      reset.addEventListener("click", () => {
        for (const field of form.querySelectorAll("input, select")) {
          if (field instanceof HTMLInputElement || field instanceof HTMLSelectElement) {
            field.value = "";
          }
        }
      });
    }

    for (const form of document.querySelectorAll("[data-training-form]")) {
      const updateScope = () => {
        const selected = form.querySelector("input[name='scope']:checked");
        const scope = selected instanceof HTMLInputElement ? selected.value : "";
        for (const block of form.querySelectorAll("[data-scope-dependent]")) {
          const mode = block.getAttribute("data-scope-dependent");
          const visible =
            mode === "work"
              ? scope === "Рабочее" || scope === "Смешанное"
              : scope === "Личное" || scope === "Смешанное";
          block.setAttribute("data-scope-hidden", visible ? "false" : "true");
        }
      };
      for (const input of form.querySelectorAll("input[name='scope']")) {
        input.addEventListener("change", updateScope);
      }
      updateScope();
    }

    for (const root of document.querySelectorAll("[data-kanban-root]")) {
      initKanban(root);
    }
  };

  const initKanban = (root) => {
    const viewport = root.querySelector("[data-kanban-viewport]");
    const stage = root.querySelector("[data-kanban-stage]");
    const board = root.querySelector("[data-kanban-board]");
    const output = root.querySelector("[data-kanban-scale-output]");
    const zoomOut = root.querySelector("[data-kanban-zoom-out]");
    const zoomIn = root.querySelector("[data-kanban-zoom-in]");
    const zoomReset = root.querySelector("[data-kanban-zoom-reset]");
    const zoomFit = root.querySelector("[data-kanban-zoom-fit]");
    const live = root.querySelector("[data-kanban-live]");
    const csrfInput = root.querySelector("[data-kanban-csrf] input[name='csrf_token']");
    if (!(viewport instanceof HTMLElement) || !(stage instanceof HTMLElement) || !(board instanceof HTMLElement)) {
      return;
    }

    const minimum = normalizeScale(root.getAttribute("data-min-scale"), MIN_SCALE, MAX_SCALE, MIN_SCALE);
    const maximum = normalizeScale(root.getAttribute("data-max-scale"), minimum, MAX_SCALE, MAX_SCALE);
    const step = normalizeScale(root.getAttribute("data-scale-step"), 0.01, 0.2, SCALE_STEP);
    const storage = window.localStorage;
    const saved = loadScaleState(storage, minimum, maximum);
    let scale = normalizeScale(saved.scale, minimum, maximum);
    let scaleMode = saved.mode;
    let suppressCardClicksUntil = 0;
    let resizeFrame = 0;
    let dragFrame = 0;
    let gesture = null;
    let pendingDrag = null;
    let drag = null;
    const touchPointers = new Map();

    const announce = (message) => {
      if (live instanceof HTMLElement) {
        live.textContent = message;
      }
    };

    const persist = () => {
      try {
        storage.setItem(STORAGE_KEY, JSON.stringify({ scale, mode: scaleMode }));
      } catch (_error) {
        // Scaling remains usable when localStorage is unavailable.
      }
    };

    const updateDensity = () => {
      root.setAttribute("data-density", scale < 0.6 ? "overview" : scale < 0.85 ? "compact" : "normal");
    };

    const syncStageSize = () => {
      const logicalWidth = board.offsetWidth;
      const logicalHeight = board.offsetHeight;
      stage.style.width = `${Math.ceil(logicalWidth * scale)}px`;
      stage.style.height = `${Math.ceil(logicalHeight * scale)}px`;
    };

    const updateControls = () => {
      const percent = Math.round(scale * 100);
      if (output instanceof HTMLOutputElement) {
        output.value = `${percent}%`;
        output.textContent = `${percent}%`;
      }
      if (zoomOut instanceof HTMLButtonElement) {
        zoomOut.disabled = scale <= minimum + 0.0001;
      }
      if (zoomIn instanceof HTMLButtonElement) {
        zoomIn.disabled = scale >= maximum - 0.0001;
      }
      if (zoomFit instanceof HTMLButtonElement) {
        zoomFit.setAttribute("aria-pressed", scaleMode === "fit" ? "true" : "false");
      }
      root.setAttribute("data-scale", String(Math.round(scale * 100)));
    };

    const setScale = (nextScale, options = {}) => {
      const oldScale = scale;
      const next = normalizeScale(nextScale, minimum, maximum, scale);
      const preservePoint = options.preservePoint || null;
      let logicalX = 0;
      let logicalY = 0;
      let localX = 0;
      let localY = 0;
      if (preservePoint) {
        const rect = viewport.getBoundingClientRect();
        localX = preservePoint.clientX - rect.left;
        localY = preservePoint.clientY - rect.top;
        logicalX = (viewport.scrollLeft + localX) / oldScale;
        logicalY = (viewport.scrollTop + localY) / oldScale;
      }
      scale = next;
      scaleMode = options.mode === "fit" ? "fit" : "manual";
      root.style.setProperty("--kanban-scale", String(scale));
      updateDensity();
      syncStageSize();
      updateControls();
      if (preservePoint) {
        viewport.scrollLeft = logicalX * scale - localX;
        viewport.scrollTop = logicalY * scale - localY;
      }
      if (options.persist !== false) {
        persist();
      }
      root.dispatchEvent(
        new CustomEvent("kanban:scalechange", { detail: { scale, mode: scaleMode, previousScale: oldScale } }),
      );
    };

    const fitBoard = (shouldPersist = true) => {
      const fitted = calculateFitScale(
        viewport.clientWidth,
        viewport.clientHeight,
        board.offsetWidth,
        board.offsetHeight,
        minimum,
        Math.min(maximum, 1),
      );
      setScale(fitted, { mode: "fit", persist: shouldPersist });
    };

    const setManualScale = (value, preservePoint = null) => {
      setScale(value, { mode: "manual", preservePoint });
      announce(`Масштаб Kanban ${Math.round(scale * 100)} процентов`);
    };

    const queueResize = () => {
      window.cancelAnimationFrame(resizeFrame);
      resizeFrame = window.requestAnimationFrame(() => {
        if (scaleMode === "fit") {
          fitBoard(true);
        } else {
          syncStageSize();
        }
      });
    };

    root.setAttribute("data-kanban-ready", "true");
    setScale(scale, { mode: scaleMode, persist: false });
    if (scaleMode === "fit") {
      window.requestAnimationFrame(() => fitBoard(false));
    }

    if (zoomOut instanceof HTMLButtonElement) {
      zoomOut.addEventListener("click", () => setManualScale(scale - step));
    }
    if (zoomIn instanceof HTMLButtonElement) {
      zoomIn.addEventListener("click", () => setManualScale(scale + step));
    }
    if (zoomReset instanceof HTMLButtonElement) {
      zoomReset.addEventListener("click", () => setManualScale(DEFAULT_SCALE));
    }
    if (zoomFit instanceof HTMLButtonElement) {
      zoomFit.addEventListener("click", () => {
        fitBoard(true);
        announce(`Kanban вместился с масштабом ${Math.round(scale * 100)} процентов`);
      });
    }

    viewport.addEventListener(
      "wheel",
      (event) => {
        if (!event.ctrlKey) {
          return;
        }
        event.preventDefault();
        const direction = event.deltaY > 0 ? -step : step;
        setManualScale(scale + direction, { clientX: event.clientX, clientY: event.clientY });
      },
      { passive: false },
    );

    viewport.addEventListener("keydown", (event) => {
      if (!(event.ctrlKey || event.metaKey)) {
        return;
      }
      if (event.key === "+" || event.key === "=") {
        event.preventDefault();
        setManualScale(scale + step);
      } else if (event.key === "-") {
        event.preventDefault();
        setManualScale(scale - step);
      } else if (event.key === "0") {
        event.preventDefault();
        setManualScale(DEFAULT_SCALE);
      }
    });

    const pointerDistance = (first, second) => Math.hypot(first.x - second.x, first.y - second.y);

    const beginPinch = () => {
      cancelPendingDrag();
      if (drag) {
        finishDrag(true);
      }
      const points = Array.from(touchPointers.values()).slice(0, 2);
      if (points.length !== 2) {
        return;
      }
      const center = { x: (points[0].x + points[1].x) / 2, y: (points[0].y + points[1].y) / 2 };
      gesture = {
        type: "pinch",
        startDistance: Math.max(pointerDistance(points[0], points[1]), 1),
        startScale: scale,
        lastCenter: center,
      };
      for (const pointerId of touchPointers.keys()) {
        try {
          viewport.setPointerCapture(pointerId);
        } catch (_error) {
          // Pointer capture is an enhancement; the gesture remains viewport-local without it.
        }
      }
      root.removeAttribute("data-panning");
    };

    const updatePinch = () => {
      const points = Array.from(touchPointers.values()).slice(0, 2);
      if (!gesture || gesture.type !== "pinch" || points.length !== 2) {
        return;
      }
      const distance = Math.max(pointerDistance(points[0], points[1]), 1);
      const center = { clientX: (points[0].x + points[1].x) / 2, clientY: (points[0].y + points[1].y) / 2 };
      setScale(gesture.startScale * (distance / gesture.startDistance), {
        mode: "manual",
        persist: false,
        preservePoint: center,
      });
      gesture.lastCenter = { x: center.clientX, y: center.clientY };
    };

    const findDropColumn = (clientX, clientY) => {
      const boardRect = board.getBoundingClientRect();
      const logicalX = (clientX - boardRect.left) / scale;
      const logicalY = (clientY - boardRect.top) / scale;
      for (const column of board.querySelectorAll("[data-kanban-column]")) {
        if (!(column instanceof HTMLElement) || !column.dataset.status) {
          continue;
        }
        if (
          logicalX >= column.offsetLeft &&
          logicalX <= column.offsetLeft + column.offsetWidth &&
          logicalY >= column.offsetTop &&
          logicalY <= column.offsetTop + column.offsetHeight
        ) {
          return column;
        }
      }
      return null;
    };

    const markDropColumn = (column) => {
      for (const candidate of board.querySelectorAll("[data-kanban-column]")) {
        candidate.setAttribute("data-drop-target", candidate === column ? "true" : "false");
      }
      if (drag) {
        drag.targetColumn = column;
      }
    };

    const positionDragPreview = () => {
      if (!drag) {
        return;
      }
      drag.preview.style.transform = `translate(${Math.round(drag.x + 14)}px, ${Math.round(drag.y + 14)}px)`;
      markDropColumn(findDropColumn(drag.x, drag.y));
    };

    const runDragFrame = () => {
      if (!drag) {
        dragFrame = 0;
        return;
      }
      const rect = viewport.getBoundingClientRect();
      const edge = Math.min(54, rect.width / 5, rect.height / 5);
      const horizontal = drag.x < rect.left + edge ? -12 : drag.x > rect.right - edge ? 12 : 0;
      const vertical = drag.y < rect.top + edge ? -12 : drag.y > rect.bottom - edge ? 12 : 0;
      if (horizontal || vertical) {
        viewport.scrollLeft += horizontal;
        viewport.scrollTop += vertical;
      }
      positionDragPreview();
      dragFrame = window.requestAnimationFrame(runDragFrame);
    };

    const startDrag = (candidate, clientX, clientY) => {
      if (!candidate || drag) {
        return;
      }
      const card = candidate.card;
      const preview = card.cloneNode(true);
      if (!(preview instanceof HTMLElement)) {
        return;
      }
      const visualRect = card.getBoundingClientRect();
      preview.classList.add("kanban-drag-preview");
      preview.removeAttribute("data-kanban-card");
      preview.style.width = `${Math.max(180, Math.min(280, visualRect.width))}px`;
      document.body.append(preview);
      card.setAttribute("aria-grabbed", "true");
      root.setAttribute("data-dragging", "true");
      drag = {
        pointerId: candidate.pointerId,
        card,
        preview,
        sourceColumn: card.closest("[data-kanban-column]"),
        targetColumn: null,
        x: clientX,
        y: clientY,
      };
      pendingDrag = null;
      positionDragPreview();
      window.cancelAnimationFrame(dragFrame);
      dragFrame = window.requestAnimationFrame(runDragFrame);
      announce("Карточка поднята. Перетащите её в колонку назначения.");
    };

    const cancelPendingDrag = () => {
      if (pendingDrag && pendingDrag.timer) {
        window.clearTimeout(pendingDrag.timer);
      }
      pendingDrag = null;
    };

    const updateColumnCount = (column) => {
      if (!(column instanceof HTMLElement)) {
        return;
      }
      const count = column.querySelectorAll("[data-kanban-card]").length;
      const outputElement = column.querySelector("[data-kanban-count]");
      if (outputElement) {
        outputElement.textContent = String(count);
      }
      const stack = column.querySelector("[data-kanban-stack]");
      if (!(stack instanceof HTMLElement)) {
        return;
      }
      const empty = stack.querySelector(".kanban-empty");
      if (count && empty) {
        empty.remove();
      } else if (!count && !empty) {
        const placeholder = document.createElement("div");
        placeholder.className = "kanban-empty";
        placeholder.textContent = "Нет карточек";
        stack.append(placeholder);
      }
    };

    const applyMovedCard = (card, sourceColumn, targetColumn, result) => {
      const stack = targetColumn.querySelector("[data-kanban-stack]");
      if (!(stack instanceof HTMLElement)) {
        return;
      }
      stack.append(card);
      card.dataset.currentStatus = result.status;
      const pill = card.querySelector(".status-pill");
      if (pill instanceof HTMLElement) {
        for (const className of Array.from(pill.classList)) {
          if (className.startsWith("status-")) {
            pill.classList.remove(className);
          }
        }
        const statusClass = {
          "Awaiting Subscription": "subscription",
          Processing: "processing",
          "Processing Disabled": "disabled",
          "Needs Review": "review",
          Processed: "processed",
        }[result.status] || "unknown";
        pill.classList.add(`status-${statusClass}`);
        pill.textContent = result.status_display || result.status;
      }
      updateColumnCount(sourceColumn);
      updateColumnCount(targetColumn);
      syncStageSize();
    };

    const saveDrop = async (card, sourceColumn, targetColumn) => {
      if (!(targetColumn instanceof HTMLElement) || targetColumn === sourceColumn || !targetColumn.dataset.status) {
        return;
      }
      const recordId = card.dataset.recordId || "";
      const token = csrfInput instanceof HTMLInputElement ? csrfInput.value : "";
      const body = new URLSearchParams({ csrf_token: token, status: targetColumn.dataset.status });
      root.setAttribute("aria-busy", "true");
      try {
        const response = await window.fetch(`/kanban/records/${encodeURIComponent(recordId)}/move`, {
          method: "POST",
          credentials: "same-origin",
          headers: { Accept: "application/json" },
          body,
        });
        if (!response.ok) {
          throw new Error(`Kanban move failed with ${response.status}`);
        }
        const result = await response.json();
        applyMovedCard(card, sourceColumn, targetColumn, result);
        announce("Карточка перемещена.");
      } catch (_error) {
        announce("Не удалось переместить карточку. Данные не изменены.");
      } finally {
        root.removeAttribute("aria-busy");
      }
    };

    const finishDrag = (cancelled) => {
      if (!drag) {
        return;
      }
      const completed = drag;
      drag = null;
      window.cancelAnimationFrame(dragFrame);
      dragFrame = 0;
      completed.preview.remove();
      completed.card.removeAttribute("aria-grabbed");
      root.removeAttribute("data-dragging");
      markDropColumn(null);
      suppressCardClicksUntil = Date.now() + 350;
      if (!cancelled) {
        void saveDrop(completed.card, completed.sourceColumn, completed.targetColumn);
      } else {
        announce("Перемещение отменено.");
      }
    };

    viewport.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 && event.pointerType === "mouse") {
        return;
      }
      const point = { x: event.clientX, y: event.clientY };
      if (event.pointerType === "touch") {
        touchPointers.set(event.pointerId, point);
      }

      if (touchPointers.size === 2) {
        event.preventDefault();
        beginPinch();
        return;
      }

      const target = event.target instanceof Element ? event.target : null;
      const handle = target ? target.closest("[data-kanban-drag-handle]") : null;
      const card = handle ? handle.closest("[data-kanban-card]") : null;
      if (handle instanceof HTMLElement && card instanceof HTMLElement) {
        event.preventDefault();
        try {
          viewport.setPointerCapture(event.pointerId);
        } catch (_error) {
          // Synthetic test events and older browsers may not expose pointer capture.
        }
        pendingDrag = {
          pointerId: event.pointerId,
          pointerType: event.pointerType,
          card,
          startX: event.clientX,
          startY: event.clientY,
          lastX: event.clientX,
          lastY: event.clientY,
          timer: 0,
        };
        if (event.pointerType === "touch") {
          pendingDrag.timer = window.setTimeout(() => {
            if (pendingDrag && pendingDrag.pointerId === event.pointerId) {
              startDrag(pendingDrag, pendingDrag.lastX, pendingDrag.lastY);
            }
          }, 340);
        }
        return;
      }

      if (event.pointerType === "touch") {
        gesture = {
          type: "pan-pending",
          pointerId: event.pointerId,
          startX: event.clientX,
          startY: event.clientY,
          startScrollLeft: viewport.scrollLeft,
          startScrollTop: viewport.scrollTop,
        };
      }
    });

    viewport.addEventListener(
      "pointermove",
      (event) => {
        if (event.pointerType === "touch" && touchPointers.has(event.pointerId)) {
          touchPointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
        }
        if (gesture && gesture.type === "pinch") {
          event.preventDefault();
          updatePinch();
          return;
        }
        if (drag && drag.pointerId === event.pointerId) {
          event.preventDefault();
          drag.x = event.clientX;
          drag.y = event.clientY;
          positionDragPreview();
          return;
        }
        if (pendingDrag && pendingDrag.pointerId === event.pointerId) {
          event.preventDefault();
          pendingDrag.lastX = event.clientX;
          pendingDrag.lastY = event.clientY;
          const moved = Math.hypot(event.clientX - pendingDrag.startX, event.clientY - pendingDrag.startY);
          if (pendingDrag.pointerType === "mouse" && moved >= 3) {
            startDrag(pendingDrag, event.clientX, event.clientY);
          } else if (pendingDrag.pointerType === "touch" && moved > 9) {
            cancelPendingDrag();
          }
          return;
        }
        if (gesture && gesture.pointerId === event.pointerId && gesture.type.startsWith("pan")) {
          const deltaX = event.clientX - gesture.startX;
          const deltaY = event.clientY - gesture.startY;
          if (gesture.type === "pan-pending" && Math.hypot(deltaX, deltaY) >= 5) {
            gesture.type = "pan";
            root.setAttribute("data-panning", "true");
            try {
              viewport.setPointerCapture(event.pointerId);
            } catch (_error) {
              // Synthetic test events and older browsers may not expose pointer capture.
            }
          }
          if (gesture.type === "pan") {
            event.preventDefault();
            viewport.scrollLeft = gesture.startScrollLeft - deltaX;
            viewport.scrollTop = gesture.startScrollTop - deltaY;
          }
        }
      },
      { passive: false },
    );

    const endPointer = (event, cancelled) => {
      const endedPinch = gesture && gesture.type === "pinch" && touchPointers.has(event.pointerId);
      if (drag && drag.pointerId === event.pointerId) {
        finishDrag(cancelled);
      } else if (pendingDrag && pendingDrag.pointerId === event.pointerId) {
        cancelPendingDrag();
      }
      if (gesture && gesture.pointerId === event.pointerId) {
        if (gesture.type === "pan") {
          suppressCardClicksUntil = Date.now() + 350;
        }
        root.removeAttribute("data-panning");
        gesture = null;
      }
      touchPointers.delete(event.pointerId);
      if (endedPinch) {
        gesture = null;
        persist();
        announce(`Масштаб Kanban ${Math.round(scale * 100)} процентов`);
      }
    };

    viewport.addEventListener("pointerup", (event) => endPointer(event, false));
    viewport.addEventListener("pointercancel", (event) => endPointer(event, true));
    viewport.addEventListener(
      "click",
      (event) => {
        if (Date.now() < suppressCardClicksUntil && event.target instanceof Element && event.target.closest(".kanban-card-link")) {
          event.preventDefault();
          event.stopPropagation();
        }
      },
      true,
    );

    if (typeof ResizeObserver !== "undefined") {
      const resizeObserver = new ResizeObserver(queueResize);
      resizeObserver.observe(viewport);
      resizeObserver.observe(board);
    } else {
      window.addEventListener("resize", queueResize);
    }
    window.addEventListener("orientationchange", queueResize);
  };

  document.addEventListener("DOMContentLoaded", initDashboard);
})();
