import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11.16.0/dist/mermaid.esm.min.mjs";

const diagram = document.querySelector("[data-architecture-diagram]");
const status = document.querySelector("[data-diagram-status]");

if (diagram) {
  const source = diagram.textContent.trim();
  let renderSequence = Promise.resolve();

  function themeVariables() {
    const styles = getComputedStyle(document.documentElement);
    const value = (name) => styles.getPropertyValue(name).trim();

    return {
      background: value("--compact-bg"),
      primaryColor: value("--compact-panel"),
      primaryTextColor: value("--compact-text"),
      primaryBorderColor: value("--compact-rule"),
      lineColor: value("--compact-muted"),
      secondaryColor: value("--compact-accent-soft"),
      secondaryTextColor: value("--compact-text"),
      secondaryBorderColor: value("--compact-accent"),
      tertiaryColor: value("--compact-bg"),
      tertiaryTextColor: value("--compact-text"),
      tertiaryBorderColor: value("--compact-rule"),
      edgeLabelBackground: value("--compact-bg"),
      fontFamily: "IBM Plex Sans, sans-serif"
    };
  }

  async function renderDiagram() {
    diagram.removeAttribute("data-processed");
    diagram.classList.remove("diagram-error");
    diagram.textContent = source;

    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      theme: "base",
      themeVariables: themeVariables(),
      flowchart: {
        curve: "basis",
        htmlLabels: false,
        useMaxWidth: true
      }
    });

    try {
      await mermaid.run({ nodes: [diagram] });
      if (status) status.textContent = "Diagram rendered from validated Mermaid source.";
    } catch (_error) {
      diagram.textContent = source;
      diagram.classList.add("diagram-error");
      if (status) status.textContent = "Diagram rendering failed. The text flow remains available.";
    }
  }

  function queueRender() {
    renderSequence = renderSequence.then(renderDiagram, renderDiagram);
  }

  queueRender();
  document.addEventListener("compact-theme-change", queueRender);
}
