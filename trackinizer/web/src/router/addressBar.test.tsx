import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { currentRoute, goTo, onRouteChange, routeHref, routeOf, routeUrl } from "./addressBar";

const PAGE = { href: "https://trackinizer.example.com/trax/graph" };

beforeEach(() => {
  history.replaceState(null, "", "/");
});

afterEach(cleanup);

test("the route is the path under the app, with its query; the app's own address is the empty route", () => {
  history.replaceState(null, "", "/trax/graph?focus=Issue/9&hops=2");
  expect(currentRoute()).toBe("#/graph?focus=Issue/9&hops=2");
  history.replaceState(null, "", "/trax/search/a%2Fb");
  expect(currentRoute()).toBe("#/search/a%2Fb");
  history.replaceState(null, "", "/trax/");
  expect(currentRoute()).toBe("");
});

test("an old link's #/... address names its route, wherever it is", () => {
  history.replaceState(null, "", "/trax/#/ref/Issue/7");
  expect(currentRoute()).toBe("#/ref/Issue/7");
  history.replaceState(null, "", "/#/list/Belief");
  expect(currentRoute()).toBe("#/list/Belief");
  history.replaceState(null, "", "/trax/graph#section");
  expect(currentRoute()).toBe("#/graph");
});

test("a route's address is its path under the app", () => {
  expect(routeUrl("#/ref/Issue/7")).toBe("/trax/ref/Issue/7");
  expect(routeUrl("#/graph?focus=Issue/9&hops=2")).toBe("/trax/graph?focus=Issue/9&hops=2");
  expect(routeUrl("")).toBe("/trax/");
  expect(routeUrl("#/")).toBe("/trax/");
  expect(routeHref({ name: "search", q: "what?" })).toBe("/trax/search/what%3F");
});

test("going to a route adds one history entry and tells every listener; going where it is adds none", () => {
  const heard = vi.fn();
  const stop = onRouteChange(heard);
  const entries = history.length;
  goTo("#/ref/Issue/7");
  expect(location.pathname).toBe("/trax/ref/Issue/7");
  expect(history.length).toBe(entries + 1);
  goTo("#/ref/Issue/7");
  expect(history.length).toBe(entries + 1);
  goTo("#/list/Issue", { replace: true });
  expect(currentRoute()).toBe("#/list/Issue");
  expect(history.length).toBe(entries + 1);
  expect(heard).toHaveBeenCalledTimes(2);
  stop();
  goTo("#/activity");
  expect(heard).toHaveBeenCalledTimes(2);
});

test("a URL to a page of this app is its route", () => {
  expect(routeOf("https://trackinizer.example.com/trax/ref/Issue/9", PAGE)).toBe("#/ref/Issue/9");
  expect(routeOf("/trax/graph?focus=Issue/9&hops=2", PAGE)).toBe("#/graph?focus=Issue/9&hops=2");
  expect(routeOf("https://trackinizer.example.com/trax/#/ref/Issue/9", PAGE)).toBe("#/ref/Issue/9");
  expect(routeOf("https://trackinizer.example.com/#/ref/Issue/9", PAGE)).toBe("#/ref/Issue/9");
  expect(routeOf("#/ref/Issue/9", PAGE)).toBe("#/ref/Issue/9");
  expect(routeOf("/trax/", PAGE)).toBe("#/");
});

test("another host, port, path, a file of the build or a non-route hash is not this app", () => {
  for (const href of [
    "https://other.example.com/trax/ref/Issue/9",
    "http://trackinizer.example.com/trax/ref/Issue/9",
    "https://trackinizer.example.com:8443/trax/ref/Issue/9",
    "https://trackinizer.example.com/api/web/get/1",
    "https://trackinizer.example.com/api/web/get/1#/x",
    "https://trackinizer.example.com/trax/assets/index-abc.js",
    "https://trackinizer.example.com/trax/#section",
    "https://trackinizer.example.com/docs/#/ref/Issue/9",
    "http://[bad",
  ]) {
    expect(routeOf(href, PAGE), href).toBeNull();
  }
});

test("a plain click on an app link moves the router; a modified one, or one opening elsewhere, is the browser's", () => {
  history.replaceState(null, "", "/trax/graph");
  const view = render(
    <>
      <a href="/trax/ref/Issue/7">app</a>
      <a href="/trax/ref/Issue/8" target="_blank">tab</a>
      <a href="https://example.com/">away</a>
    </>,
  );
  // After the app's own listener, on the document: whether it took the click,
  // then the browser's load stopped, which jsdom cannot do.
  let taken = false;
  const after = (event: Event) => {
    taken = event.defaultPrevented;
    event.preventDefault();
  };
  addEventListener("click", after);
  const clicked = (name: string, init?: MouseEventInit) => {
    fireEvent.click(view.getByText(name), init);
    return taken;
  };
  try {
    expect(clicked("app", { metaKey: true })).toBe(false);
    expect(clicked("tab")).toBe(false);
    expect(clicked("away")).toBe(false);
    expect(currentRoute()).toBe("#/graph");
    expect(clicked("app")).toBe(true);
    expect(currentRoute()).toBe("#/ref/Issue/7");
  } finally {
    removeEventListener("click", after);
  }
});
