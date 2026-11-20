(() => {
  const $ = id => document.getElementById(id);
  const modes = ["default", "styled"];
  const urls = new Map();

  function status(message, error = false) {
    $("status").textContent = message;
    $("status").className = error ? "error" : "";
  }

  async function request(path, options) {
    const response = await fetch(path, options);
    const data = await response.json();
    if (!response.ok) {
      throw new Error(typeof data.detail === "string" ? data.detail : "请求失败，请检查输入。");
    }
    return data;
  }

  function clearResult() {
    $("comparison").hidden = true;
    $("empty").hidden = false;
    $("sentences").replaceChildren();
    for (const [mode, url] of urls) {
      const audio = $(`${mode}-audio`);
      audio.removeAttribute("src");
      audio.load();
      URL.revokeObjectURL(url);
    }
    urls.clear();
  }

  $("text").addEventListener("input", clearResult);
  $("voice").addEventListener("change", clearResult);
  $("preview-form").addEventListener("submit", async event => {
    event.preventDefault();
    const input = {voice_id: $("voice").value, text: $("text").value.trim()};
    if (!input.voice_id || !input.text) return;
    clearResult();
    $("generate").disabled = $("voice").disabled = $("text").disabled = true;
    status("正在用同一个声音生成两版试听…");
    try {
      const result = await request("/api/tone/preview", {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(input),
      });
      for (const sentence of result.sentences) {
        const item = document.createElement("li");
        item.textContent = sentence.text;
        const tone = document.createElement("span");
        tone.textContent = sentence.instruction || "默认语气";
        item.append(tone);
        $("sentences").append(item);
      }
      for (const mode of modes) {
        const entry = result.audio[mode];
        const bytes = Uint8Array.from(atob(entry.wav), character => character.charCodeAt(0));
        const url = URL.createObjectURL(new Blob([bytes], {type: "audio/wav"}));
        urls.set(mode, url);
        $(`${mode}-audio`).src = url;
        $(`${mode}-duration`).textContent = `${entry.duration.toFixed(2)} 秒`;
        $(`${mode}-warning`).textContent = entry.warning || "";
      }
      $("empty").hidden = true;
      $("comparison").hidden = false;
      status(result.sentences.some(sentence => sentence.instruction) ? "已生成两版试听。" : "这段文本沿用默认语气。");
    } catch (error) {
      clearResult();
      status(error.message, true);
    } finally {
      $("generate").disabled = $("voice").disabled = $("text").disabled = false;
    }
  });

  request("/api/voices").then(voices => {
    if (!voices.length) {
      $("voice").replaceChildren(new Option("请先在 Home 添加声音", ""));
      return;
    }
    $("voice").replaceChildren(...voices.map(voice => new Option(voice.name, voice.id)));
    const selected = new URL(location.href).searchParams.get("voice_id");
    if (voices.some(voice => voice.id === selected)) $("voice").value = selected;
    $("voice").disabled = $("generate").disabled = false;
  }).catch(error => status(error.message, true));
})();
