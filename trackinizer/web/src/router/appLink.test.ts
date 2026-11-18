import { expect, test } from "vitest";
import { appHash, linkProps } from "./appLink";

const PAGE = { href: "https://trackinizer.example.com/app/#/graph" };

test("a URL to a route of this page's app is its hash", () => {
  expect(appHash("https://trackinizer.example.com/app/#/ref/Issue/9", PAGE)).toBe("#/ref/Issue/9");
  expect(appHash("https://trackinizer.example.com/app/#/graph?focus=Issue/9&hops=2", PAGE)).toBe("#/graph?focus=Issue/9&hops=2");
  expect(appHash("#/ref/Issue/9", PAGE)).toBe("#/ref/Issue/9");
});

test("another host, port, path or a non-route hash is not this app", () => {
  for (const href of [
    "https://other.example.com/app/#/ref/Issue/9",
    "http://trackinizer.example.com/app/#/ref/Issue/9",
    "https://trackinizer.example.com:8443/app/#/ref/Issue/9",
    "https://trackinizer.example.com/api/web/get/1#/x",
    "https://trackinizer.example.com/app/",
    "https://trackinizer.example.com/app/#section",
    "https://trackinizer.example.com/docs/#/ref/Issue/9",
    "http://[bad",
  ]) {
    expect(appHash(href, PAGE), href).toBeNull();
  }
});

test("an app link has no target; any other opens in a new tab", () => {
  expect(linkProps("https://trackinizer.example.com/app/#/ref/Issue/9", PAGE)).toEqual({ href: "#/ref/Issue/9" });
  expect(linkProps("https://example.com/", PAGE)).toEqual({ href: "https://example.com/", target: "_blank", rel: "noopener noreferrer" });
});
