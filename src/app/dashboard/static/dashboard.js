document.addEventListener("DOMContentLoaded", () => {
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
});
