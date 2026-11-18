import { expect, test } from "./fixtures";

// The built app offers Chrome what Install needs: the entry page links a manifest, which is served as one with
// its 192 and 512 pixel icons. (The server's own test covers the signed-out fetch; this suite signs nobody in.)

test("the built manifest is served as a manifest, with icons Chrome accepts", async ({ page, request }) => {
  await page.goto("/app/");
  await expect(page.locator('link[rel="manifest"]')).toHaveAttribute("href", "/app/manifest.webmanifest");
  const response = await request.get("/app/manifest.webmanifest");
  expect(response.status()).toBe(200);
  expect(response.headers()["content-type"]).toContain("application/manifest+json");
  const manifest = await response.json() as { name: string; display: string; start_url: string; scope: string; icons: { src: string; sizes: string }[] };
  expect(manifest.name).toBe("Trackinizer");
  expect([manifest.display, manifest.start_url, manifest.scope]).toEqual(["standalone", "/app/", "/app/"]);
  expect(manifest.icons.map((icon) => icon.sizes).toSorted()).toEqual(["192x192", "512x512"]);
  for (const icon of manifest.icons) {
    const served = await request.get(icon.src);
    expect([served.status(), served.headers()["content-type"]]).toEqual([200, "image/png"]);
  }
});
