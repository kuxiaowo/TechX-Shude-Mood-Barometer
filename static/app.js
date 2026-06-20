document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-modal-target]").forEach((button) => {
    button.addEventListener("click", () => {
      const dialog = document.getElementById(button.dataset.modalTarget);
      if (dialog && typeof dialog.showModal === "function") {
        dialog.showModal();
      }
    });
  });

  document.querySelectorAll("[data-modal-close]").forEach((button) => {
    button.addEventListener("click", () => {
      button.closest("dialog")?.close();
    });
  });

  document.querySelectorAll("dialog.modal").forEach((dialog) => {
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) {
        dialog.close();
      }
    });
  });
});
