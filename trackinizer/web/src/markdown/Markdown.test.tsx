import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, test } from "vitest";
import { ToastProvider } from "../ui/toast";
import { Markdown } from "./Markdown";
import { safeUrl } from "./safeUrl";

const KINDS = ["Issue", "Belief", "Experiment"];
const U = "3f2504e0-4f89-11d3-9a0c-0305e82c3301";

afterEach(cleanup);

function renderMd(source: string, kinds: readonly string[] = KINDS): HTMLElement {
  // A code block's Copy button says how it went in a toast.
  return render(
    <ToastProvider>
      <Markdown source={source} kinds={kinds} />
    </ToastProvider>,
  ).container;
}

/**
 * What makes rendered Markdown unsafe: executable elements, handlers, script URLs.
 *
 * The app's own icons (`svg.ic`, constant markup) are the one SVG allowed.
 */
function unsafe(root: Element): string[] {
  const found: string[] = [];
  for (const node of root.querySelectorAll("*")) {
    if (node.closest("svg.ic")) continue;
    if (/^(script|iframe|object|embed|style|form|svg|math)$/i.test(node.tagName)) {
      found.push(`<${node.tagName.toLowerCase()}>`);
    }
    for (const attribute of node.attributes) {
      if (/^on/i.test(attribute.name)) found.push(`${attribute.name}=`);
      if (
        /^(href|src|xlink:href|action|formaction)$/i.test(attribute.name) &&
        /^\s*(javascript|data|vbscript):/i.test(attribute.value.replace(/[\u0000-\u001f]/g, ""))
      ) {
        found.push(`${attribute.name}=${attribute.value.slice(0, 24)}`);
      }
    }
  }
  if (root.querySelector("a a")) found.push("nested <a>");
  return found;
}

/** The in-app links, in order. */
function refs(root: Element): string {
  return [...root.querySelectorAll("a")]
    .map((a) => a.getAttribute("href"))
    .filter((href) => href?.startsWith("#/"))
    .join(" ");
}

// The library survey's 21 cases (/opt/scratch/artifacts/trackinizer-web/lib-survey/
// bundle/test/markdown-safety.test.tsx), which the old UI's marked, DOMPurify and
// linkify failed four of.
const SAFETY_CASES: { name: string; md: string; wantRefs?: string }[] = [
  { name: "raw <script>", md: "hi <script>alert(1)</script>" },
  { name: "raw <img onerror>", md: "<img src=x onerror=alert(1)>" },
  { name: "javascript: link", md: "[x](javascript:alert(1))" },
  { name: "JaVaScRiPt: link", md: "[x](JaVaScRiPt:alert(1))" },
  { name: "entity-obfuscated js link", md: "[x](jav&#x09;ascript:alert(1))" },
  { name: "autolink <javascript:>", md: "<javascript:alert(1)>" },
  { name: "data: link", md: "[x](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)" },
  { name: "javascript: image", md: "![x](javascript:alert(1))" },
  { name: "raw <a href=javascript>", md: '<a href="javascript:alert(1)">x</a>' },
  { name: "refs in text", md: "See Issue#4 and Belief#97.", wantRefs: "#/ref/Issue/4 #/ref/Belief/97" },
  { name: "ref inside link text (bold)", md: "[see **Issue#4**](https://example.com/a)", wantRefs: "" },
  { name: "ref inside link text (code)", md: "[see `Issue#4`](https://example.com/a)", wantRefs: "" },
  { name: "ref inside URL (autolink)", md: "https://example.com/x/Issue#4", wantRefs: "" },
  { name: "ref inside inline code", md: "run `trax show Issue#4`", wantRefs: "" },
  { name: "ref inside code block", md: "```\nIssue#4\n```", wantRefs: "" },
  { name: "ref inside raw HTML attr", md: '<span title="Issue#4">x</span>', wantRefs: "" },
  { name: "ref in link title attr", md: '[x](https://e.com "Issue#4")', wantRefs: "" },
  { name: "uuid in text", md: `id ${U} here`, wantRefs: `#/lookup/${U}` },
  { name: "guarded ##Issue#4 / x#Issue#4", md: "a x#Issue#4 b", wantRefs: "" },
  { name: "ref in GFM table cell", md: "| a | b |\n|---|---|\n| Issue#4 | x |", wantRefs: "#/ref/Issue/4" },
  { name: "unknown kind Foo#4", md: "Foo#4", wantRefs: "" },
];

describe("the 21 safety cases", () => {
  test.each(SAFETY_CASES)("$name", ({ md, wantRefs }) => {
    const root = renderMd(md);
    expect(unsafe(root)).toEqual([]);
    if (wantRefs !== undefined) expect(refs(root)).toBe(wantRefs);
  });
});

test("raw HTML shows as its text and is never parsed into elements", () => {
  const root = renderMd("hi <script>alert(1)</script> and <img src=x onerror=alert(1)>");
  expect(root.textContent).toBe("hi <script>alert(1)</script> and <img src=x onerror=alert(1)>");
  expect(root.querySelector("script, img")).toBeNull();
});

test("a refused link keeps its text but links nowhere (COLD-12)", () => {
  const root = renderMd("[click](javascript:alert(1)) and <mailto:a@b.c> and [up](/admin)");
  expect(root.querySelectorAll("a")).toHaveLength(0);
  expect(root.textContent).toBe("click and mailto:a@b.c and up");
});

test("text that looks like markup never reaches an attribute (COLD-12)", () => {
  const source = 'x" onmouseover="window.__injected=1';
  const root = renderMd(`[a](<https://e.com/${source}> "${source.replaceAll('"', "&quot;")}")`);
  const anchor = root.querySelector("a")!;
  expect(unsafe(root)).toEqual([]);
  expect([...anchor.attributes].map((attribute) => attribute.name).sort()).toEqual([
    "href",
    "rel",
    "target",
    "title",
  ]);
  expect(anchor.getAttribute("title")).toBe(source);
});

test("a link whose URL or text holds a ref keeps its href and label (COLD-13)", () => {
  let root = renderMd("[source](https://example.com/Issue#412)");
  let anchors = root.querySelectorAll("a");
  expect(anchors).toHaveLength(1);
  expect(anchors[0].getAttribute("href")).toBe("https://example.com/Issue#412");
  expect(anchors[0].textContent).toBe("source");
  cleanup();

  root = renderMd("[Issue#412](https://example.com)");
  anchors = root.querySelectorAll("a");
  expect(anchors).toHaveLength(1);
  expect(anchors[0].getAttribute("href")).toBe("https://example.com");
  expect(anchors[0].textContent).toBe("Issue#412");
});

test("a ref is an in-app chip with its kind; a UUID shows eight characters", () => {
  const root = renderMd(`Issue#4 and ${U.toUpperCase()}`);
  const [ref, lookup] = root.querySelectorAll("a");
  expect(ref.getAttribute("href")).toBe("#/ref/Issue/4");
  expect(ref.className).toBe("ref");
  expect(ref.textContent).toBe("Issue#4");
  expect(ref.getAttribute("target")).toBeNull();
  expect(lookup.getAttribute("href")).toBe(`#/lookup/${U}`);
  expect(lookup.textContent).toBe(U.slice(0, 8).toUpperCase());
  expect(lookup.getAttribute("title")).toBe(U);
});

test("an external link opens in a new tab; a written in-app link stays a plain link", () => {
  const root = renderMd("[out](https://example.com) and [in](#/ref/Belief/2)");
  const [out, inApp] = root.querySelectorAll("a");
  expect(out.getAttribute("target")).toBe("_blank");
  expect(out.getAttribute("rel")).toBe("noopener noreferrer");
  expect(inApp.getAttribute("href")).toBe("#/ref/Belief/2");
  expect(inApp.className).toBe("");
  expect(inApp.getAttribute("target")).toBeNull();
});

test("a link whose text is a ref and whose target is that ref in the app, on any of its hosts, is the ref's chip", () => {
  const root = renderMd(
    [
      "[Issue#42](https://trackinizer.example.com/app/#/ref/Issue/42)",
      "[Experiment#5](https://trackinizer.example.com/#/ref/Experiment/5)",
      "[Belief#7](#/ref/Belief/7)",
      // Not the ref it names, not the app's route, or not a kind: each stays the link written.
      "[Issue#8](https://trackinizer.example.com/app/#/ref/Issue/9)",
      "[Issue#10](https://example.com/docs/#/ref/Issue/10)",
      "[the brief](https://trackinizer.example.com/app/#/ref/Issue/11)",
      "[Foo#12](https://trackinizer.example.com/app/#/ref/Foo/12)",
    ].join(" "),
  );
  const links = [...root.querySelectorAll("a")].map((a) => [a.textContent, a.getAttribute("href"), a.className, a.getAttribute("target")]);
  expect(links).toEqual([
    ["Issue#42", "#/ref/Issue/42", "ref", null],
    ["Experiment#5", "#/ref/Experiment/5", "ref", null],
    ["Belief#7", "#/ref/Belief/7", "ref", null],
    ["Issue#8", "https://trackinizer.example.com/app/#/ref/Issue/9", "", "_blank"],
    ["Issue#10", "https://example.com/docs/#/ref/Issue/10", "", "_blank"],
    ["the brief", "https://trackinizer.example.com/app/#/ref/Issue/11", "", "_blank"],
    ["Foo#12", "https://trackinizer.example.com/app/#/ref/Foo/12", "", "_blank"],
  ]);
  // The chip has its kind's icon, as a ref in text does.
  expect(root.querySelector("a.ref svg")).not.toBeNull();
});

test("kinds match as the server spells them, and with no kinds no # is linked", () => {
  expect(refs(renderMd("issue#4 Issue#4x Issue#4"))).toBe("#/ref/Issue/4");
  cleanup();
  expect(refs(renderMd("#4 and Issue#4", []))).toBe("");
  cleanup();
  // A seq past 2^53 would round to another inquiry's, so it stays text (WEB-05).
  const [past, last] = [BigInt(Number.MAX_SAFE_INTEGER) + 2n, Number.MAX_SAFE_INTEGER];
  expect(refs(renderMd(`Issue#${past} and Issue#${last}`))).toBe(`#/ref/Issue/${last}`);
});

test("headings sit below the page's title and sections", () => {
  const root = renderMd("# One\n\n## Two");
  expect([...root.querySelectorAll("h1, h2, h3, h4")].map((node) => node.tagName)).toEqual([
    "H3",
    "H4",
  ]);
});

test("safeUrl passes exactly http://, https:// and #/", () => {
  const allowed = ["http://a.b", "HTTPS://a.b/c?d#e", "#/ref/Issue/4", "#/lookup/x"];
  const refused = [
    "javascript:alert(1)",
    "mailto:a@b.c",
    "data:text/html,x",
    "//evil.example",
    "/admin",
    "page.html",
    "#section",
    " https://a.b",
    "vbscript:x",
    // A scheme with no `//` is relative to the app's own origin (WEB-25).
    "https:foo",
    "http:/auth/logout",
    "https:\\\\evil.example",
  ];
  expect(allowed.map(safeUrl)).toEqual(allowed);
  expect(refused.map(safeUrl)).toEqual(refused.map(() => ""));
});

test("images={false} shows an image as its alt and URL, never an img", () => {
  const md = "![x](https://evil.example/p.png)";
  expect(renderMd(md).querySelector("img")).not.toBeNull();
  const root = render(<Markdown source={md} kinds={KINDS} images={false} />).container;
  expect(root.querySelector("img")).toBeNull();
  expect(root.textContent).toBe("x (https://evil.example/p.png)");
});

test("breaks keeps each line of a paragraph, as a chat keeps a message's lines; a description reflows them", () => {
  const source = "Ada: New in the thread:\n@helperbot: Launched.\n@helperbot: Done.\n\n```\nkept\nas is\n```";
  const lines = (root: Element) => [...root.querySelectorAll("p")].map((p) => p.innerHTML);
  expect(lines(renderMd(source))).toEqual(["Ada: New in the thread:\n@helperbot: Launched.\n@helperbot: Done."]);
  cleanup();
  const root = render(
    <ToastProvider>
      <Markdown source={source} kinds={KINDS} breaks />
    </ToastProvider>,
  ).container;
  expect(lines(root)).toEqual(["Ada: New in the thread:<br>\n@helperbot: Launched.<br>\n@helperbot: Done."]);
  expect(root.querySelector("pre code")!.textContent).toBe("kept\nas is\n");
});
