// MINDMAP / MERMAID: markdown-driven mind map and flowchart diagram.
import { useEffect, useRef } from "react";
import mermaid from "mermaid";
import { Transformer } from "markmap-lib";

export function MermaidView({ definition, label }: { readonly definition: string; readonly label?: string }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    mermaid.initialize({ startOnLoad: false, theme: "dark" });
    mermaid.render("mermaid-" + Math.random().toString(36).slice(2), definition).then(({ svg }) => {
      if (ref.current) ref.current.innerHTML = svg;
    }).catch(() => {
      if (ref.current) ref.current.textContent = "Diagram could not be rendered.";
    });
  }, [definition]);

  return <div className="ui-mermaid" role="img" aria-label={label ?? "mermaid diagram"}><div ref={ref} /></div>;
}

export function MarkmapView({ markdown, label }: { readonly markdown: string; readonly label?: string }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    try {
      const transformer = new Transformer();
      const { root } = transformer.transform(markdown);
      const data = JSON.stringify(root);
      ref.current.setAttribute("data-markmap", data);
    } catch {
      ref.current.textContent = "Markmap could not be rendered.";
    }
  }, [markdown]);

  return <div className="ui-markmap" role="img" aria-label={label ?? "markmap mind map"}><div ref={ref} /></div>;
}
