import { expect, test } from "vitest";
import { APP } from "./appName";
import { fillName, manifestOf, TRACKINIZER } from "./identity";

const OTHER = { name: "Acme", short_name: "Acme", theme_color: "#111111", background_color: "#222222", icons: [] };

test("the app is Trackinizer", () => {
  expect([TRACKINIZER.name, TRACKINIZER.short_name]).toEqual(["Trackinizer", "Trackinizer"]);
});

test("the manifest names the identity and installs as a standalone window under /trax/", () => {
  const manifest = manifestOf(OTHER);
  expect([manifest.name, manifest.short_name]).toEqual(["Acme", "Acme"]);
  expect([manifest.display, manifest.id, manifest.start_url, manifest.scope]).toEqual(["standalone", "/trax/", "/trax/", "/trax/"]);
  expect([manifest.theme_color, manifest.background_color]).toEqual(["#111111", "#222222"]);
  expect(manifestOf(TRACKINIZER).icons).toEqual(TRACKINIZER.icons);
});

test("the app has the 192 and 512 pixel icons Chrome needs to offer install", () => {
  expect(TRACKINIZER.icons.map((icon) => icon.sizes).toSorted()).toEqual(["192x192", "512x512"]);
  expect(TRACKINIZER.icons.every((icon) => icon.type === "image/png" && icon.src.startsWith("/trax/"))).toBe(true);
});

test("the page's name placeholders take the identity's name", () => {
  const page = "<title>%APP_NAME%</title><meta content=\"%APP_NAME%: a graph\">";
  expect(fillName(page, OTHER)).toBe("<title>Acme</title><meta content=\"Acme: a graph\">");
});

test("the page shows the app's name", () => {
  expect(APP).toBe(TRACKINIZER);
});
