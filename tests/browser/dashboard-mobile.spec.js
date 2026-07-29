const { test, expect } = require("@playwright/test");
const fs = require("node:fs");
const path = require("node:path");

const viewports = [
  { width: 360, height: 800 },
  { width: 390, height: 844 },
  { width: 430, height: 932 },
  { width: 1440, height: 900 },
];

async function pageOverflow(page) {
  return page.evaluate(() => ({
    body: document.body.scrollWidth - window.innerWidth,
    html: document.documentElement.scrollWidth - window.innerWidth,
  }));
}

test("detail and recent events remain inside every required viewport", async ({ page }) => {
  for (const viewport of viewports) {
    await page.setViewportSize(viewport);
    await page.goto("/records/recFixtureLong1");
    await expect(page.locator(".detail-layout")).toBeVisible();
    const detailOverflow = await pageOverflow(page);
    expect(detailOverflow.body).toBeLessThanOrEqual(0);
    expect(detailOverflow.html).toBeLessThanOrEqual(0);
    const detailBounds = await page.locator(".detail-layout .panel").evaluateAll((panels) =>
      panels.map((panel) => {
        const bounds = panel.getBoundingClientRect();
        return { left: bounds.left, right: bounds.right };
      }),
    );
    for (const bounds of detailBounds) {
      expect(bounds.left).toBeGreaterThanOrEqual(-0.5);
      expect(bounds.right).toBeLessThanOrEqual(viewport.width + 0.5);
    }
    const formBounds = await page.locator(".edit-form textarea, .edit-form select").evaluateAll((controls) =>
      controls.map((control) => control.getBoundingClientRect().right),
    );
    for (const right of formBounds) expect(right).toBeLessThanOrEqual(viewport.width + 0.5);

    await page.goto("/");
    const recent = page.locator('.mini-record[href="/records/recFixtureLong1"]');
    await expect(recent).toBeVisible();
    const recentBounds = await recent.boundingBox();
    expect(recentBounds.x + recentBounds.width).toBeLessThanOrEqual(viewport.width + 0.5);
    const overviewOverflow = await pageOverflow(page);
    expect(overviewOverflow.body).toBeLessThanOrEqual(0);
    expect(overviewOverflow.html).toBeLessThanOrEqual(0);
    await recent.click();
    await expect(page).toHaveURL(/\/records\/recFixtureLong1$/);
  }
});

test("the empty-source sentinel filters records without a literal source value", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/records?source=__empty__&page_size=10");
  await expect(page.locator('.record-title[href="/records/recFixtureLong1"]')).toBeVisible();
  await expect(page.locator(".filter-chips")).toContainText("Источник не указан");
  await page.goto("/kanban?source=__empty__");
  await expect(page.locator('[data-record-id="recFixtureLong1"]')).toBeVisible();
});

test("captures fake-fixture detail and overview screenshots", async ({ page }) => {
  test.skip(process.env.DASHBOARD_SCREENSHOTS !== "1", "Local screenshot capture only");
  const output = path.resolve("output/playwright");
  fs.mkdirSync(output, { recursive: true });
  for (const viewport of viewports) {
    await page.setViewportSize(viewport);
    await page.goto("/records/recFixtureLong1");
    await page.screenshot({ path: path.join(output, `detail-${viewport.width}.png`), fullPage: true });
    await page.goto("/");
    await page.screenshot({ path: path.join(output, `overview-${viewport.width}.png`), fullPage: true });
  }
});
