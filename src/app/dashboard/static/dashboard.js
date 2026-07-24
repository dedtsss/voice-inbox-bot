document.addEventListener("DOMContentLoaded", () => {
  for (const form of document.querySelectorAll("form.filters")) {
    const reset = form.querySelector('a[href$="records"], a[href$="review"], a[href$="queue"], a[href$="processed"], a[href$="technical"]');
    if (reset) {
      reset.addEventListener("click", () => {
        for (const field of form.querySelectorAll("input, select")) {
          if (field instanceof HTMLInputElement || field instanceof HTMLSelectElement) {
            field.value = "";
          }
        }
      });
    }
  }
});
