import { expect, test } from "vitest";
import { APP } from "./appName";
import { DEFAULT_APP, fillName, identityOf, IDENTITIES, manifestOf } from "./identity";

const OTHER = { name: "Acme", short_name: "Acme", theme_color: "#111111", background_color: "#222222", icons: [] };

test("the default identity is Trackinizer", () => {
  expect(DEFAULT_APP).toBe("trackinizer");
  expect([IDENTITIES.trackinizer!.name, IDENTITIES.trackinizer!.short_name]).toEqual(["Trackinizer", "Trackinizer"]);
});

test("a build picks its identity by key; none picks the default, and a typo is refused", () => {
  expect(identityOf(undefined)).toBe(IDENTITIES.trackinizer);
  expect(identityOf("")).toBe(IDENTITIES.trackinizer);
  expect(() => identityOf("trackinizr")).toThrow("Unknown app identity trackinizr");
});

test("the manifest names the identity and installs as a standalone window under /trax/", () => {
  const manifest = manifestOf(OTHER);
  expect([manifest.name, manifest.short_name]).toEqual(["Acme", "Acme"]);
  expect([manifest.display, manifest.id, manifest.start_url, manifest.scope]).toEqual(["standalone", "/trax/", "/trax/", "/trax/"]);
  expect([manifest.theme_color, manifest.background_color]).toEqual(["#111111", "#222222"]);
  expect(manifestOf(IDENTITIES.trackinizer!).icons).toEqual(IDENTITIES.trackinizer!.icons);
});

test("every identity has the 192 and 512 pixel icons Chrome needs to offer install", () => {
  for (const each of Object.values(IDENTITIES)) {
    expect(each.icons.map((icon) => icon.sizes).toSorted()).toEqual(["192x192", "512x512"]);
    expect(each.icons.every((icon) => icon.type === "image/png" && icon.src.startsWith("/trax/"))).toBe(true);
  }
});

test("the page's name placeholders take the identity's name", () => {
  const page = "<title>%APP_NAME%</title><meta content=\"%APP_NAME%: a graph\">";
  expect(fillName(page, OTHER)).toBe("<title>Acme</title><meta content=\"Acme: a graph\">");
});

test("the page shows the name of the identity this build was made with", () => {
  expect(APP).toBe(IDENTITIES.trackinizer);
});
