const textarea = document.getElementById("inputText");
const charCount = document.getElementById("charCount");
const resultEl = document.getElementById("result");
const outputDot = document.getElementById("outputDot");
const btn = document.getElementById("translateBtn");

textarea.addEventListener("input", () => {
  const len = textarea.value.length;
  charCount.textContent = `${len} character${len !== 1 ? "s" : ""}`;
});

async function translateText() {
  const text = textarea.value.trim();

  if (!text) {
    resultEl.innerHTML = '<span class="placeholder-text">Please enter some text first.</span>';
    return;
  }

  btn.disabled = true;
  btn.textContent = "Translating...";
  resultEl.innerHTML = '<span class="loading-text">Translating...</span>';
  outputDot.classList.remove("active");

  try {
    const response = await fetch("/translate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text })
    });

    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.detail || "Request failed");
    }

    resultEl.textContent = data.translation;
    outputDot.classList.add("active");

  } catch (error) {
    resultEl.innerHTML = `<span class="placeholder-text">Error: ${error.message}</span>`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Translate →";
  }
}