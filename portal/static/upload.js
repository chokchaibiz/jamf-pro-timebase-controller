(() => {
  "use strict";

  const isFileDrag = (event) => Array.from(event.dataTransfer?.types || []).includes("Files");
  const isCsv = (file) => Boolean(file && file.name.toLowerCase().endsWith(".csv"));

  document.querySelectorAll("[data-file-drop]").forEach((dropzone) => {
    const input = dropzone.querySelector("[data-file-input]");
    const label = dropzone.querySelector("[data-file-label]");
    const error = dropzone.parentElement?.querySelector("[data-file-error]");
    const defaultLabel = label?.dataset.defaultLabel || "เลือกไฟล์ CSV";
    let dragDepth = 0;

    if (!input || !label) {
      return;
    }

    const showError = (message) => {
      input.value = "";
      label.textContent = defaultLabel;
      dropzone.classList.remove("has-file");
      dropzone.classList.add("has-error");
      if (error) {
        error.textContent = message;
        error.hidden = false;
      }
    };

    const showFile = (file) => {
      dropzone.classList.remove("has-error");
      dropzone.classList.add("has-file");
      label.textContent = file?.name || defaultLabel;
      if (error) {
        error.textContent = "";
        error.hidden = true;
      }
    };

    const assignDroppedFile = (file, droppedFiles) => {
      if (!isCsv(file)) {
        showError("กรุณาเลือกไฟล์นามสกุล .csv เท่านั้น");
        return;
      }
      try {
        input.files = droppedFiles;
      } catch (_directAssignmentError) {
        try {
          const transfer = new DataTransfer();
          transfer.items.add(file);
          input.files = transfer.files;
        } catch (_transferError) {
          showError("Browser นี้ไม่รองรับ Drag and Drop กรุณาคลิกเพื่อเลือกไฟล์");
          return;
        }
      }
      input.dispatchEvent(new Event("change", { bubbles: true }));
    };

    input.addEventListener("change", () => {
      const files = Array.from(input.files || []);
      if (files.length === 0) {
        showFile(null);
        dropzone.classList.remove("has-file");
      } else if (files.length > 1) {
        showError("กรุณาเลือกไฟล์ CSV เพียง 1 ไฟล์");
      } else if (!isCsv(files[0])) {
        showError("กรุณาเลือกไฟล์นามสกุล .csv เท่านั้น");
      } else {
        showFile(files[0]);
      }
    });

    dropzone.addEventListener("dragenter", (event) => {
      if (!isFileDrag(event)) return;
      event.preventDefault();
      dragDepth += 1;
      dropzone.classList.add("is-dragover");
    });

    dropzone.addEventListener("dragover", (event) => {
      if (!isFileDrag(event)) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
    });

    dropzone.addEventListener("dragleave", (event) => {
      if (!isFileDrag(event)) return;
      event.preventDefault();
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) dropzone.classList.remove("is-dragover");
    });

    dropzone.addEventListener("drop", (event) => {
      if (!isFileDrag(event)) return;
      event.preventDefault();
      event.stopPropagation();
      dragDepth = 0;
      dropzone.classList.remove("is-dragover");
      const droppedFiles = event.dataTransfer?.files;
      const files = Array.from(droppedFiles || []);
      if (files.length !== 1) {
        showError("กรุณาวางไฟล์ CSV เพียง 1 ไฟล์");
        return;
      }
      assignDroppedFile(files[0], droppedFiles);
    });

    dropzone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        input.click();
      }
    });
  });

  document.addEventListener("dragover", (event) => {
    if (isFileDrag(event)) event.preventDefault();
  });
  document.addEventListener("drop", (event) => {
    if (isFileDrag(event) && !event.target.closest?.("[data-file-drop]")) {
      event.preventDefault();
    }
  });
})();
