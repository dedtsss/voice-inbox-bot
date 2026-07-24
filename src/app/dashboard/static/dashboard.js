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
});
