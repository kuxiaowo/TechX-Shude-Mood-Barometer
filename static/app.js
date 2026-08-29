document.addEventListener("DOMContentLoaded", () => {
  const panasForm = document.querySelector("[data-panas-form]");
  if (panasForm) {
    const positiveKeys = ["cheerful", "lively", "happy", "joyful", "proud"];
    const negativeKeys = ["miserable", "mad", "afraid", "scared", "sad"];
    const moodOutput = panasForm.querySelector("[data-mood-score]");
    const positiveOutput = panasForm.querySelector("[data-positive-score]");
    const negativeOutput = panasForm.querySelector("[data-negative-score]");
    const statusOutput = panasForm.querySelector("[data-score-status]");
    const scoreDial = panasForm.querySelector("[data-score-dial]");

    const selectedValue = (name) => {
      const input = panasForm.querySelector(`input[name="${name}"]:checked`);
      return input ? Number(input.value) : null;
    };

    const updatePanasScore = () => {
      const allKeys = [...positiveKeys, ...negativeKeys];
      const responses = Object.fromEntries(
        allKeys.map((key) => [key, selectedValue(key)]),
      );
      const completed = Object.values(responses).filter(Number.isFinite).length;

      if (completed !== allKeys.length) {
        moodOutput.textContent = "--";
        positiveOutput.textContent = "--";
        negativeOutput.textContent = "--";
        statusOutput.textContent = `已完成 ${completed} / ${allKeys.length} 项`;
        scoreDial.style.setProperty("--score", 0);
        return;
      }

      const positiveSum = positiveKeys.reduce((sum, key) => sum + responses[key], 0);
      const negativeSum = negativeKeys.reduce((sum, key) => sum + responses[key], 0);
      const positiveScore = (positiveSum - 5) * 5;
      const negativeScore = (negativeSum - 5) * 5;
      const moodScore = Math.round((positiveScore + 100 - negativeScore) / 2);

      moodOutput.textContent = moodScore;
      positiveOutput.textContent = positiveScore;
      negativeOutput.textContent = negativeScore;
      statusOutput.textContent = "10 项已完成，可以保存";
      scoreDial.style.setProperty("--score", moodScore);
    };

    panasForm.addEventListener("change", updatePanasScore);
    updatePanasScore();
  }

  document.querySelectorAll("[data-mood-chart]").forEach((chart) => {
    const svg = chart.querySelector("svg");
    const emptyMessage = chart.querySelector(".mood-chart-empty");
    let points = [];
    try {
      points = JSON.parse(chart.dataset.points || "[]");
    } catch (_error) {
      points = [];
    }

    const availablePoints = points.filter((point) => Number.isFinite(point.score));
    chart.classList.toggle("is-empty", availablePoints.length === 0);
    if (!svg || availablePoints.length === 0) {
      return;
    }

    const namespace = "http://www.w3.org/2000/svg";
    const width = 720;
    const height = 250;
    const margin = { top: 18, right: 18, bottom: 38, left: 40 };
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    const xAt = (index) => margin.left + (plotWidth * index) / Math.max(points.length - 1, 1);
    const yAt = (value) => margin.top + plotHeight - (plotHeight * value) / 100;
    const appendSvg = (tag, attributes = {}, text = "") => {
      const element = document.createElementNS(namespace, tag);
      Object.entries(attributes).forEach(([name, value]) => element.setAttribute(name, value));
      if (text) element.textContent = text;
      svg.appendChild(element);
      return element;
    };

    [0, 25, 50, 75, 100].forEach((value) => {
      appendSvg("line", {
        x1: margin.left,
        y1: yAt(value),
        x2: width - margin.right,
        y2: yAt(value),
        class: "mood-chart-grid-line",
      });
      appendSvg("text", {
        x: margin.left - 10,
        y: yAt(value) + 4,
        class: "mood-chart-axis-label",
        "text-anchor": "end",
      }, String(value));
    });

    const labelStep = points.length <= 7 ? 1 : 5;
    points.forEach((point, index) => {
      if (index % labelStep !== 0 && index !== points.length - 1) return;
      appendSvg("text", {
        x: xAt(index),
        y: height - 12,
        class: "mood-chart-axis-label",
        "text-anchor": index === 0 ? "start" : index === points.length - 1 ? "end" : "middle",
      }, point.label);
    });

    const series = [
      { key: "positive", className: "is-positive" },
      { key: "negative", className: "is-negative" },
      { key: "score", className: "is-mood" },
    ];
    series.forEach(({ key, className }) => {
      let segment = [];
      const drawSegment = () => {
        if (segment.length > 1) {
          appendSvg("polyline", {
            points: segment.join(" "),
            class: `mood-chart-line ${className}`,
          });
        }
        segment = [];
      };

      points.forEach((point, index) => {
        const value = point[key];
        if (!Number.isFinite(value)) {
          return;
        }
        segment.push(`${xAt(index)},${yAt(value)}`);
        const circle = appendSvg("circle", {
          cx: xAt(index),
          cy: yAt(value),
          r: key === "score" ? 4.5 : 3,
          class: `mood-chart-point ${className}`,
        });
        const title = document.createElementNS(namespace, "title");
        title.textContent = `${point.date} · ${key === "score" ? "综合" : key === "positive" ? "正性" : "负性"} ${value}`;
        circle.appendChild(title);
      });
      drawSegment();
    });

    emptyMessage?.setAttribute("hidden", "");
  });

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

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) {
        event.preventDefault();
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
