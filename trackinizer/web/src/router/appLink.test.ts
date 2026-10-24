import { expect, test } from "vitest";
import { linkProps } from "./appLink";

const PAGE = { href: "https://trackinizer.example.com/trax/graph" };

test("an app link is its route's own address, with no target; any other opens in a new tab", () => {
  expect(linkProps("https://trackinizer.example.com/trax/ref/Issue/9", PAGE)).toEqual({ href: "/trax/ref/Issue/9" });
  expect(linkProps("https://trackinizer.example.com/trax/#/ref/Issue/9", PAGE)).toEqual({ href: "/trax/ref/Issue/9" });
  expect(linkProps("#/graph?focus=Issue/9&hops=2", PAGE)).toEqual({ href: "/trax/graph?focus=Issue/9&hops=2" });
  expect(linkProps("https://example.com/", PAGE)).toEqual({ href: "https://example.com/", target: "_blank", rel: "noopener noreferrer" });
});
