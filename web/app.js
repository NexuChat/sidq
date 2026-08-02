const copyButtons = document.querySelectorAll("[data-copy]");

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }

  const input = document.createElement("textarea");
  input.value = text;
  input.setAttribute("readonly", "");
  input.className = "clipboard-proxy";
  document.body.appendChild(input);
  input.select();
  try {
    if (!document.execCommand("copy")) {
      throw new Error("The browser refused clipboard access.");
    }
  } finally {
    input.remove();
  }
}

for (const button of copyButtons) {
  button.addEventListener("click", async () => {
    const status = document.querySelector(`#${button.getAttribute("aria-describedby")}`);
    const originalLabel = button.textContent;
    const originalStatus = status.textContent;
    try {
      await copyText(button.dataset.copy);
      button.textContent = "Copied";
      status.textContent = "Copied. Paste it into your terminal.";
    } catch (error) {
      status.textContent =
        error instanceof Error ? `Copy failed: ${error.message}` : "Copy failed.";
    }
    window.setTimeout(() => {
      button.textContent = originalLabel;
      status.textContent = originalStatus;
    }, 2400);
  });
}

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

async function readJsonResponse(response) {
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.toLowerCase().includes("application/json")) return null;
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function responseError(response, payload, fallback) {
  const detail =
    payload && typeof payload.error === "string" && payload.error
      ? payload.error
      : fallback;
  return `${detail} (HTTP ${response.status}).`;
}

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
      const capability = await readJsonResponse(capabilityResponse);
      if (!capabilityResponse.ok) {
        throw new Error(
          responseError(capabilityResponse, capability, "Could not prepare the demo run"),
        );
      }
      if (!capability || typeof capability.capability !== "string") {
        throw new Error(
          `Server returned an invalid capability response (HTTP ${capabilityResponse.status}).`,
        );
      }
      const response = await fetch(`/run/${button.dataset.run}`, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "X-Sidq-Demo": "run",
          "X-Sidq-Capability": capability.capability,
        },
      });
      const result = await readJsonResponse(response);
      if (!response.ok) {
        const retry = result?.retry_after ? ` Retry in ${result.retry_after}s.` : "";
        runStatus.textContent = `${responseError(response, result, "The run could not start")}${retry}`;
        runOutput.hidden = true;
      } else if (!result) {
        throw new Error(`Server returned an invalid run response (HTTP ${response.status}).`);
      } else {
        runOutput.textContent = `$ ${result.command}\n\n${result.output}`;
        runOutput.focus({ preventScroll: true });
        runProgress.textContent = `${result.elapsed_seconds}s elapsed · expected about ${result.expected_seconds}s`;
        if (result.exit_code === 0) {
          runStatus.textContent = `${result.description} Exit 0.`;
        } else if (result.exit_code === 1) {
          runStatus.textContent =
            `${result.description} Exit 1 — findings, not an operational failure.`;
        } else {
          const exitDetail =
            result.exit_code == null ? "no exit code" : `exit ${result.exit_code}`;
          runStatus.textContent = `${result.description} Operational failure (${exitDetail}).`;
        }
      }
    } catch (error) {
      runStatus.textContent =
        error instanceof Error ? error.message : "Could not reach the run endpoint.";
      runOutput.hidden = true;
    } finally {
      window.clearInterval(progressTimer);
      for (const other of runButtons) other.disabled = false;
      button.textContent = label;
    }
  });
}
