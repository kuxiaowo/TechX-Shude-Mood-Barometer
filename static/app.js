document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("dialog[data-auto-show-modal]").forEach((dialog) => {
    if (!dialog.open && typeof dialog.showModal === "function") {
      dialog.showModal();
    }
  });

  document.querySelectorAll("form[data-consent-dialog]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      const consentInput = form.querySelector('input[name="privacy_consent"]');
      if (consentInput?.value === "yes") {
        return;
      }

      if (!form.reportValidity()) {
        return;
      }

      event.preventDefault();
      const dialog = document.getElementById(form.dataset.consentDialog);
      if (dialog && typeof dialog.showModal === "function") {
        dialog.showModal();
      }
    });
  });

  document.querySelectorAll("[data-consent-confirm]").forEach((button) => {
    button.addEventListener("click", () => {
      const dialog = button.closest("dialog");
      const checkbox = dialog?.querySelector('input[name="privacy_consent"]');
      if (!checkbox?.reportValidity()) {
        return;
      }

      const form = document.getElementById(button.dataset.consentConfirm);
      const consentInput = form?.querySelector('input[name="privacy_consent"]');
      if (!form || !consentInput) {
        return;
      }

      consentInput.value = checkbox.value;
      dialog.close();
      form.requestSubmit();
    });
  });

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
    dialog.addEventListener("cancel", (event) => {
      if (dialog.hasAttribute("data-required-modal")) {
        event.preventDefault();
      }
    });

    dialog.addEventListener("click", (event) => {
      if (dialog.hasAttribute("data-required-modal")) {
        return;
      }

      if (event.target === dialog) {
        dialog.close();
      }
    });
  });
});
