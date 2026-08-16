function requestErrorHost(element) {
  return element.closest(".paper-card, .job-panel, .settings-form") ||
    (element.closest(".page-heading") ? document.querySelector("#job-panel") : null);
}

const proseCommands = new Map([
  ["\\textit", { tag: "em" }],
  ["\\emph", { tag: "em" }],
  ["\\textsl", { tag: "em" }],
  ["\\textbf", { tag: "strong" }],
  ["\\texttt", { tag: "code", className: "tex-code" }],
  ["\\underline", { tag: "span", className: "tex-underline" }],
  ["\\textsc", { tag: "span", className: "tex-small-caps" }],
  ["\\textsuperscript", { tag: "sup" }],
  ["\\textsubscript", { tag: "sub" }],
]);

const proseLiterals = new Map([
  ["\\LaTeX", "LaTeX"],
  ["\\TeX", "TeX"],
  ["\\ldots", "…"],
  ["\\dots", "…"],
  ["\\textendash", "–"],
  ["\\textemdash", "—"],
  ["\\%", "%"],
  ["\\&", "&"],
  ["\\_", "_"],
  ["\\#", "#"],
  ["\\{", "{"],
  ["\\}", "}"],
  ["\\$", "$"],
]);

const displayEnvironments = [
  "equation", "equation*", "align", "align*", "alignat", "alignat*",
  "gather", "gather*", "multline", "multline*", "CD",
];

function isEscaped(text, index) {
  let slashes = 0;
  for (let position = index - 1; position >= 0 && text[position] === "\\"; position -= 1) {
    slashes += 1;
  }
  return slashes % 2 === 1;
}

function findClosingDelimiter(text, start, delimiter) {
  let position = start;
  while (position < text.length) {
    position = text.indexOf(delimiter, position);
    if (position < 0) return -1;
    if (!isEscaped(text, position)) return position;
    position += delimiter.length;
  }
  return -1;
}

function mathDelimiterAt(text, index) {
  if (isEscaped(text, index)) return null;
  if (text.startsWith("$$", index)) {
    return { left: "$$", right: "$$", display: true, includeDelimiters: false };
  }
  if (text[index] === "$") {
    return { left: "$", right: "$", display: false, includeDelimiters: false };
  }
  if (text.startsWith("\\[", index)) {
    return { left: "\\[", right: "\\]", display: true, includeDelimiters: false };
  }
  if (text.startsWith("\\(", index)) {
    return { left: "\\(", right: "\\)", display: false, includeDelimiters: false };
  }
  for (const environment of displayEnvironments) {
    const left = `\\begin{${environment}}`;
    if (text.startsWith(left, index)) {
      return {
        left,
        right: `\\end{${environment}}`,
        display: true,
        includeDelimiters: true,
      };
    }
  }
  return null;
}

function balancedGroup(text, openIndex) {
  let depth = 0;
  for (let position = openIndex; position < text.length; position += 1) {
    if (isEscaped(text, position)) continue;
    if (text[position] === "{") depth += 1;
    if (text[position] !== "}") continue;
    depth -= 1;
    if (depth === 0) {
      return { content: text.slice(openIndex + 1, position), end: position + 1 };
    }
  }
  return null;
}

function appendMath(parent, source, display, original) {
  const host = document.createElement("span");
  host.className = display ? "tex-display" : "tex-inline";
  try {
    if (!window.katex) throw new Error("KaTeX is unavailable");
    window.katex.render(source, host, {
      displayMode: display,
      throwOnError: true,
      trust: false,
      strict: "ignore",
      maxSize: 10,
      maxExpand: 1000,
      output: "htmlAndMathml",
    });
  } catch (_error) {
    host.className += " tex-fallback";
    host.textContent = original;
  }
  parent.append(host);
}

function appendArxivText(parent, text) {
  let buffer = "";
  let index = 0;

  const flush = () => {
    if (!buffer) return;
    parent.append(document.createTextNode(buffer));
    buffer = "";
  };

  while (index < text.length) {
    const delimiter = mathDelimiterAt(text, index);
    if (delimiter) {
      const contentStart = index + delimiter.left.length;
      const closing = findClosingDelimiter(text, contentStart, delimiter.right);
      if (closing >= 0) {
        flush();
        const end = closing + delimiter.right.length;
        const original = text.slice(index, end);
        const source = delimiter.includeDelimiters
          ? original
          : text.slice(contentStart, closing);
        appendMath(parent, source, delimiter.display, original);
        index = end;
        continue;
      }
    }

    let commandMatched = false;
    for (const [command, definition] of proseCommands) {
      const openIndex = index + command.length;
      if (!text.startsWith(command, index) || text[openIndex] !== "{") continue;
      const group = balancedGroup(text, openIndex);
      if (!group) continue;
      flush();
      const element = document.createElement(definition.tag);
      if (definition.className) element.className = definition.className;
      appendArxivText(element, group.content);
      parent.append(element);
      index = group.end;
      commandMatched = true;
      break;
    }
    if (commandMatched) continue;

    for (const [command, replacement] of proseLiterals) {
      if (!text.startsWith(command, index)) continue;
      buffer += replacement;
      index += command.length;
      commandMatched = true;
      break;
    }
    if (commandMatched) continue;

    if (text[index] === "~" && !isEscaped(text, index)) {
      buffer += "\u00a0";
    } else {
      buffer += text[index];
    }
    index += 1;
  }
  flush();
}

function renderArxivText(root = document) {
  root.querySelectorAll(".arxiv-text:not([data-tex-rendered])").forEach((element) => {
    const source = element.textContent;
    element.replaceChildren();
    appendArxivText(element, source);
    element.dataset.texRendered = "true";
  });
}

document.addEventListener("htmx:beforeRequest", (event) => {
  const host = requestErrorHost(event.detail.elt);
  host?.querySelector(".inline-request-error")?.remove();
});

document.addEventListener("htmx:responseError", (event) => {
  const host = requestErrorHost(event.detail.elt);
  if (!host) return;

  const parsed = new DOMParser().parseFromString(event.detail.xhr.responseText, "text/html");
  const message = parsed.body.textContent.trim() || "The request failed. Please try again.";
  const error = document.createElement("div");
  error.className = "inline-request-error";
  error.role = "alert";
  error.textContent = message;
  host.append(error);
});

function updateResearchArea(area) {
  const topics = [...area.querySelectorAll('input[name="categories"]')];
  const selected = topics.filter((input) => input.checked).length;
  const count = area.querySelector("[data-area-count]");

  area.classList.toggle("has-selection", selected > 0);
  if (count) count.textContent = selected ? `${selected} selected` : `${topics.length} topics`;
}

document.addEventListener("click", (event) => {
  const confirmation = event.target.closest("[data-confirm]");
  if (confirmation && !window.confirm(confirmation.dataset.confirm)) {
    event.preventDefault();
    return;
  }

  const action = event.target.closest("[data-area-action]");
  if (!action) return;

  const area = action.closest("[data-research-area]");
  const checked = action.dataset.areaAction === "all";
  area.querySelectorAll('input[name="categories"]').forEach((input) => {
    input.checked = checked;
  });
  updateResearchArea(area);
});

document.addEventListener("change", (event) => {
  if (!event.target.matches('[data-research-area] input[name="categories"]')) return;
  updateResearchArea(event.target.closest("[data-research-area]"));
});

document.addEventListener("DOMContentLoaded", () => renderArxivText());
document.addEventListener("htmx:afterSwap", (event) => renderArxivText(event.detail.target));
