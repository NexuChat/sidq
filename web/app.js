const copyButton = document.querySelector("[data-copy]");
const copyStatus = document.querySelector("#copy-status");

copyButton.addEventListener("click", async () => {
  const command = copyButton.dataset.copy;
  try {
    await navigator.clipboard.writeText(command);
  } catch {
    const range = document.createRange();
    range.selectNodeContents(document.querySelector("#command-text"));
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    document.execCommand("copy");
    selection.removeAllRanges();
  }
  copyButton.textContent = "Copied";
  copyStatus.textContent = "Copied. Run it from the repository root.";
  window.setTimeout(() => {
    copyButton.textContent = "Copy";
    copyStatus.textContent = "One command. A local graph. The same deterministic verdict.";
  }, 2400);
});

// Each name is matched by the server against a closed table of fixed commands.
const expectedSeconds = {
  "gate-demo": 15,
  audit: 45,
  repair: 90,
  handoff: 30,
  claims: 90,
};
const runOutput = document.querySelector("#run-output");
const runProgress = document.querySelector("#run-progress");
const runStatus = document.querySelector("#run-status");
const runButtons = document.querySelectorAll("[data-run]");

for (const button of runButtons) {
  button.addEventListener("click", async () => {
    const label = button.textContent;
    const expected = expectedSeconds[button.dataset.run];
    const started = performance.now();
    const updateProgress = () => {
      const elapsed = Math.floor((performance.now() - started) / 1000);
      runProgress.textContent = `${elapsed}s elapsed · expected about ${expected}s`;
    };
    for (const other of runButtons) other.disabled = true;
    button.textContent = "Running…";
    runStatus.textContent = "Running on the host now. This is not a recording.";
    runProgress.hidden = false;
    updateProgress();
    const progressTimer = window.setInterval(updateProgress, 1000);
    runOutput.hidden = false;
    runOutput.textContent = "";
    try {
      const capabilityResponse = await fetch(
        `/capability?command=${encodeURIComponent(button.dataset.run)}`,
        {
          credentials: "same-origin",
          headers: { "X-Sidq-Demo": "capability" },
        },
      );
      const capability = await capabilityResponse.json();
      if (!capabilityResponse.ok) {
        throw new Error(capability.error || "Could not prepare the demo run.");
      }
      const response = await fetch(`/run/${button.dataset.run}`, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "X-Sidq-Demo": "run",
          "X-Sidq-Capability": capability.capability,
        },
      });
      const result = await response.json();
      if (!response.ok) {
        const retry = result.retry_after ? ` Retry in ${result.retry_after}s.` : "";
        runStatus.textContent = `${result.error || "The run could not start."}${retry}`;
        runOutput.hidden = true;
      } else {
        runOutput.textContent = `$ ${result.command}\n\n${result.output}`;
        runOutput.focus({ preventScroll: true });
        runProgress.textContent = `${result.elapsed_seconds}s elapsed · expected about ${result.expected_seconds}s`;
        runStatus.textContent =
          result.exit_code === 0
            ? `${result.description} Exit 0.`
            : `${result.description} Exit ${result.exit_code} — findings, not a failure.`;
      }
    } catch {
      runStatus.textContent = "Could not reach the run endpoint.";
      runOutput.hidden = true;
    } finally {
      window.clearInterval(progressTimer);
      for (const other of runButtons) other.disabled = false;
      button.textContent = label;
    }
  });
}
