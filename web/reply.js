"use strict";
// Keep legacy Markdown readable without interpreting provider HTML.
globalThis.VoiceReply = {
  // One paragraph block per rendered message; no length-based splitting.
  blocks(text) {
    return text.split(/\n+/).map(line => line.replace(/^\s*#{1,6}\s+/, "").trim()).filter(Boolean);
  },
  paragraphs(text) {
    const pieces = [];
    for (const line of text.split(/\n+/).map(line => line.replace(/^\s*#{1,6}\s+/, "").trim()).filter(Boolean)) {
      let part = "";
      for (const sentence of line.match(/[^。！？!?]+[。！？!?]*|[。！？!?]+/g) || [line]) {
        if (part.length + sentence.length > 90 && part) { pieces.push(part); part = ""; }
        let rest = sentence;
        while (rest.length > 90) { pieces.push(rest.slice(0, 90)); rest = rest.slice(90); }
        part += rest;
      }
      if (part) pieces.push(part);
    }
    return pieces;
  },
  tokens(text) {
    return text.split(/(\*\*[^*]+\*\*|__[^_]+__)/g).filter(Boolean).map(part =>
      /^(\*\*[^*]+\*\*|__[^_]+__)$/.test(part) ? {text: part.slice(2, -2), strong: true} : {text: part, strong: false});
  }
};
