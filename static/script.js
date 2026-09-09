const textarea = document.getElementById("inputText");
const charCount = document.getElementById("charCount");
const resultEl = document.getElementById("result");
const resultPanel = document.getElementById("resultPanel");
const resultLabel = document.getElementById("resultLabel");
const btn = document.getElementById("translateBtn");

const IDLE_TEXT = "Nothing translated yet.";

function setCount() {
  const n = textarea.value.length;
  charCount.textContent = `${n} character${n === 1 ? "" : "s"}`;
}

function setBusy(busy) {
  btn.disabled = busy;
  btn.replaceChildren();
  if (busy) {
    const spinner = document.createElement("span");
    spinner.className = "spinner";
    btn.append(spinner, document.createTextNode("Translating"));
  } else {
    btn.textContent = "Translate";
  }
}

/* Always rendered as text, never as markup: the backend response is treated as
   untrusted input. */
function show(text, { state = "idle", label = "Translation", empty = false } = {}) {
  resultEl.textContent = text;
  resultEl.dataset.empty = String(empty);
  resultPanel.dataset.state = state;
  resultLabel.textContent = label;
}

async function translate() {
  const text = textarea.value.trim();

  if (!text) {
    show("Add some text above and try again.", {
      state: "error",
      label: "Nothing to translate",
    });
    textarea.focus();
    return;
  }

  setBusy(true);
  show("Translating…", { empty: true });

  try {
    const response = await fetch("/translate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });

    let data = {};
    try {
      data = await response.json();
    } catch {
      data = {};
    }

    if (!response.ok) {
      throw new Error(data.error || data.detail || "The translation service did not respond.");
    }

    if (typeof data.translation !== "string" || !data.translation) {
      throw new Error("The translation service returned an empty result.");
    }

    show(data.translation, { label: "Translation" });
  } catch (error) {
    // Only our own message is shown. Nothing from an exception or a URL.
    const message =
      error instanceof TypeError
        ? "Could not reach the server. Check your connection and try again."
        : error.message;
    show(`${message}\n\nYour text has not been changed. You can try again.`, {
      state: "error",
      label: "We couldn't translate that",
    });
  } finally {
    setBusy(false);
  }
}

textarea.addEventListener("input", setCount);
btn.addEventListener("click", translate);

// Ctrl/Cmd+Enter submits, which is what people expect from a text workspace.
textarea.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
    event.preventDefault();
    translate();
  }
});

setCount();
show(IDLE_TEXT, { empty: true });
