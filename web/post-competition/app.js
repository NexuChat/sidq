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
    // The announcement is optional; the copy is not. Reading `textContent`
    // straight off a missing region threw before the clipboard was ever
    // touched, which left the page's only Copy button dead in every browser.
    const describedBy = button.getAttribute("aria-describedby");
    const status = describedBy ? document.getElementById(describedBy) : null;
    const announce = (message) => {
      if (status) status.textContent = message;
    };
    const originalLabel = button.textContent;
    const originalStatus = status ? status.textContent : "";
    try {
      await copyText(button.dataset.copy);
      button.textContent = "Copied";
      announce("Copied. Paste it into your terminal.");
    } catch (error) {
      announce(
        error instanceof Error ? `Copy failed: ${error.message}` : "Copy failed."
      );
    }
    window.setTimeout(() => {
      button.textContent = originalLabel;
      announce(originalStatus);
    }, 2400);
  });
}

// Each name is matched by the server against a closed table of fixed commands.
const expectedSeconds = {
  rederive: 1,
  attestation: 1,
  "gap-order": 1,
  "gate-demo": 1,
};
const runOutput = document.querySelector("#run-output");
const runProgress = document.querySelector("#run-progress");
const runStatus = document.querySelector("#run-status");
const runButtons = document.querySelectorAll("[data-run]");
// The busy region is presentational; the command is not. Reading a method off
// a missing element threw before the fetch was ever issued, which is the same
// shape that once left the page's only Copy button dead in every browser.
const runRegion = document.querySelector(".handoff-run");
const recordedProof = document.querySelector("#recorded-proof");

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

const releaseIdentity = document.querySelector("#release-identity");

async function renderReleaseIdentity() {
  if (!releaseIdentity) return;
  try {
    const response = await fetch("/healthz", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    const payload = await readJsonResponse(response);
    const release = payload?.release;
    if (
      response.ok &&
      release?.state === "deployed" &&
      typeof release.commit_sha === "string" &&
      /^[0-9a-f]{40}$/.test(release.commit_sha)
    ) {
      releaseIdentity.textContent =
        `Submission baseline: ${release.commit_sha} · live presentation copy`;
    } else if (response.ok && release?.state === "local/dev") {
      releaseIdentity.textContent = "Release: local/dev";
    } else {
      releaseIdentity.textContent = "Release: unavailable";
    }
  } catch {
    releaseIdentity.textContent = "Release: unavailable";
  }
}

renderReleaseIdentity();

for (const button of runButtons) {
  button.addEventListener("click", async () => {
    const label = button.textContent;
    const expected = expectedSeconds[button.dataset.run];
    const started = performance.now();
    const updateProgress = () => {
      const elapsed = Math.floor((performance.now() - started) / 1000);
      // A command with no recorded estimate is a normal state, not a number.
      // Printing "expected about undefineds" is worse than printing nothing.
      runProgress.textContent = expected
        ? `${elapsed}s elapsed · expected about ${expected}s`
        : `${elapsed}s elapsed`;
    };
    for (const other of runButtons) other.disabled = true;
    if (recordedProof) recordedProof.hidden = true;
    if (runRegion) runRegion.setAttribute("aria-busy", "true");
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
        runOutput.focus();
        runProgress.textContent = result.expected_seconds
          ? `${result.elapsed_seconds}s elapsed · expected about ${result.expected_seconds}s`
          : `${result.elapsed_seconds}s elapsed`;
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
      if (runRegion) runRegion.setAttribute("aria-busy", "false");
      for (const other of runButtons) other.disabled = false;
      button.textContent = label;
    }
  });
}
