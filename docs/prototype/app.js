(function () {
  "use strict";

  function bySelector(selector) {
    return document.querySelector(selector);
  }

  document.addEventListener("click", function (event) {
    var freeze = event.target.closest("[data-freeze]");
    if (freeze) {
      var card = freeze.closest(".worker-card");
      var status = (card && card.querySelector(".status")) || bySelector("[data-profile-status]");
      var isFrozen = freeze.dataset.state === "frozen";

      freeze.dataset.state = isFrozen ? "enabled" : "frozen";
      freeze.textContent = isFrozen ? "冻结" : "激活";
      if (status) {
        status.className = "status " + (isFrozen ? "status-running" : "status-frozen");
        status.textContent = isFrozen ? "可用" : "已冻结";
      }
      return;
    }

    var openDialog = event.target.closest("[data-open-dialog]");
    if (openDialog) {
      var dialog = bySelector("#" + openDialog.dataset.openDialog);
      if (dialog && typeof dialog.showModal === "function") {
        dialog.showModal();
      }
      return;
    }

    var closeDialog = event.target.closest("[data-close-dialog]");
    if (closeDialog) {
      var activeDialog = closeDialog.closest("dialog");
      if (activeDialog) activeDialog.close();
      return;
    }

    var stop = event.target.closest("[data-confirm-stop]");
    if (stop) {
      var statusTarget = bySelector("[data-run-status]");
      var progressTitle = bySelector("[data-progress-title]");
      var stopButtons = document.querySelectorAll("[data-open-dialog]");
      if (statusTarget) {
        statusTarget.className = "status status-waiting";
        statusTarget.textContent = "正在结束";
      }
      if (progressTitle) progressTitle.textContent = "正在结束这个任务";
      stopButtons.forEach(function (button) { button.textContent = "正在结束"; });
      var firstStep = bySelector("[data-stop-first]");
      var forceStep = bySelector("[data-stop-force]");
      if (firstStep) firstStep.hidden = true;
      if (forceStep) forceStep.hidden = false;
      return;
    }

    var forceStop = event.target.closest("[data-force-stop]");
    if (forceStop) {
      var forceDialog = forceStop.closest("dialog");
      if (forceDialog) forceDialog.close();
      var finalStatus = bySelector("[data-run-status]");
      var finalTitle = bySelector("[data-progress-title]");
      var finalCopy = bySelector("[data-progress-copy]");
      var finalButtons = document.querySelectorAll("[data-open-dialog]");
      if (finalStatus) {
        finalStatus.className = "status status-stopped";
        finalStatus.textContent = "已停止";
      }
      if (finalTitle) finalTitle.textContent = "任务已停止";
      if (finalCopy) finalCopy.textContent = "任务已按你的选择结束。";
      finalButtons.forEach(function (button) {
        button.disabled = true;
        button.textContent = "已停止";
      });
      return;
    }

    var save = event.target.closest("[data-save-settings]");
    if (save) {
      var note = bySelector("[data-save-note]");
      if (note) note.textContent = "设置已保存";
    }
  });
}());
