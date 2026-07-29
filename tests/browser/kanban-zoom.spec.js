const { test, expect } = require("@playwright/test");
const fs = require("node:fs");
const path = require("node:path");

const mobileSizes = [
  { width: 360, height: 800 },
  { width: 390, height: 844 },
  { width: 430, height: 932 },
];

async function scaleText(page) {
  return page.locator("[data-kanban-scale-output]").textContent();
}

async function dispatchTouch(page, selector, type, pointerId, x, y, isPrimary = true) {
  await page.dispatchEvent(selector, type, {
    pointerId,
    pointerType: "touch",
    isPrimary,
    button: 0,
    buttons: type === "pointerup" || type === "pointercancel" ? 0 : 1,
    clientX: x,
    clientY: y,
  });
}

test("Kanban zoom is responsive at 360, 390, 430, landscape, tablet, and desktop", async ({ page }) => {
  const consoleErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => consoleErrors.push(error.message));

  const sizes = [
    ...mobileSizes,
    { width: 844, height: 390 },
    { width: 768, height: 1024 },
    { width: 1440, height: 900 },
  ];
  for (const size of sizes) {
    await page.setViewportSize(size);
    await page.goto("/kanban");
    await page.evaluate(() => localStorage.clear());
    await page.reload();
    await expect(page.locator("[data-kanban-root]")).toHaveAttribute("data-kanban-ready", "true");
    await expect(page.locator("[data-kanban-scale-output]")).toHaveText("100%");
    const overflow = await page.evaluate(() => ({
      body: document.body.scrollWidth - document.body.clientWidth,
      html: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    }));
    expect(overflow.body).toBeLessThanOrEqual(0);
    expect(overflow.html).toBeLessThanOrEqual(0);
    await expect(page.locator("[data-kanban-zoom-fit]")).toBeVisible();
  }
  expect(consoleErrors).toEqual([]);
});

test("buttons, fit, persistence, pinch, pan, card open, and scale-aware handle drag work", async ({ page }) => {
  const consoleErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => consoleErrors.push(error.message));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/kanban");
  await page.evaluate(() => localStorage.clear());
  await page.reload();

  await page.locator("[data-kanban-zoom-out]").click();
  await expect(page.locator("[data-kanban-scale-output]")).toHaveText("95%");
  await page.locator("[data-kanban-zoom-in]").click();
  await expect(page.locator("[data-kanban-scale-output]")).toHaveText("100%");
  await page.locator("[data-kanban-zoom-out]").click();
  await page.reload();
  await expect(page.locator("[data-kanban-scale-output]")).toHaveText("95%");
  await page.locator("[data-kanban-zoom-reset]").click();
  await expect(page.locator("[data-kanban-scale-output]")).toHaveText("100%");
  await page.locator("[data-kanban-zoom-fit]").click();
  const fitPercent = Number((await scaleText(page)).replace("%", ""));
  expect(fitPercent).toBeGreaterThanOrEqual(40);
  expect(fitPercent).toBeLessThanOrEqual(100);

  await page.locator("[data-kanban-zoom-reset]").click();
  const viewportBox = await page.locator("[data-kanban-viewport]").boundingBox();
  const centerX = viewportBox.x + viewportBox.width / 2;
  const centerY = viewportBox.y + Math.min(160, viewportBox.height / 2);
  await dispatchTouch(page, "[data-kanban-viewport]", "pointerdown", 11, centerX - 32, centerY, true);
  await dispatchTouch(page, "[data-kanban-viewport]", "pointerdown", 12, centerX + 32, centerY, false);
  await dispatchTouch(page, "[data-kanban-viewport]", "pointermove", 11, centerX - 48, centerY, true);
  await dispatchTouch(page, "[data-kanban-viewport]", "pointermove", 12, centerX + 48, centerY, false);
  await dispatchTouch(page, "[data-kanban-viewport]", "pointerup", 11, centerX - 48, centerY, true);
  await dispatchTouch(page, "[data-kanban-viewport]", "pointerup", 12, centerX + 48, centerY, false);
  expect(Number((await scaleText(page)).replace("%", ""))).toBeGreaterThan(100);

  for (let index = 0; index < 20; index += 1) {
    const button = page.locator("[data-kanban-zoom-out]");
    if (await button.isDisabled()) break;
    await button.click();
  }
  await expect(page.locator("[data-kanban-scale-output]")).toHaveText("40%");
  await page.locator("[data-kanban-viewport]").evaluate((element) => {
    element.scrollLeft = 80;
    element.scrollTop = 30;
  });
  const beforePan = await page.locator("[data-kanban-viewport]").evaluate((element) => element.scrollLeft);
  await dispatchTouch(page, "[data-kanban-viewport]", "pointerdown", 21, centerX + 40, centerY, true);
  await dispatchTouch(page, "[data-kanban-viewport]", "pointermove", 21, centerX - 60, centerY, true);
  await dispatchTouch(page, "[data-kanban-viewport]", "pointerup", 21, centerX - 60, centerY, true);
  const afterPan = await page.locator("[data-kanban-viewport]").evaluate((element) => element.scrollLeft);
  expect(afterPan).toBeGreaterThan(beforePan);

  await page.waitForTimeout(400);
  await page.locator('[data-record-id="recFixtureSub1"] .kanban-card-link').click();
  await expect(page).toHaveURL(/\/records\/recFixtureSub1$/);
  await page.goBack();
  await page.locator("[data-kanban-zoom-reset]").click();
  for (let index = 0; index < 7; index += 1) await page.locator("[data-kanban-zoom-out]").click();
  await expect(page.locator("[data-kanban-scale-output]")).toHaveText("65%");
  await page.locator("[data-kanban-viewport]").evaluate((element) => {
    const root = element.closest("[data-kanban-root]");
    const scale = Number(root.getAttribute("data-scale")) / 100;
    const review = root.querySelector('[data-column-key="review"]');
    element.scrollLeft = Math.max(0, review.offsetLeft * scale - 12);
  });
  const handle = page.locator('[data-record-id="recFixtureReview1"] [data-kanban-drag-handle]');
  const handleBox = await handle.boundingBox();
  const targetBox = await page.locator('[data-column-key="done"]').boundingBox();
  const moveResponse = page.waitForResponse(
    (response) => response.url().includes("/kanban/records/recFixtureReview1/move") && response.request().method() === "POST",
  );
  await dispatchTouch(page, '[data-record-id="recFixtureReview1"] [data-kanban-drag-handle]', "pointerdown", 31, handleBox.x + handleBox.width / 2, handleBox.y + handleBox.height / 2, true);
  await page.waitForTimeout(380);
  await dispatchTouch(page, "[data-kanban-viewport]", "pointermove", 31, targetBox.x + targetBox.width / 2, targetBox.y + 120, true);
  await dispatchTouch(page, "[data-kanban-viewport]", "pointerup", 31, targetBox.x + targetBox.width / 2, targetBox.y + 120, true);
  expect((await moveResponse).status()).toBe(200);
  await expect(page.locator('[data-column-key="done"] [data-record-id="recFixtureReview1"]')).toHaveCount(1);
  expect(consoleErrors).toEqual([]);
});

test("captures fake-fixture screenshots", async ({ page }) => {
  test.skip(process.env.KANBAN_SCREENSHOTS !== "1", "Local screenshot capture only");
  const output = path.resolve("output/playwright");
  fs.mkdirSync(output, { recursive: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/kanban");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await page.locator("[data-kanban-root]").scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(output, "kanban-mobile-100.png") });
  await page.locator("[data-kanban-zoom-fit]").click();
  await page.screenshot({ path: path.join(output, "kanban-mobile-fit.png") });
  for (let index = 0; index < 20; index += 1) {
    const button = page.locator("[data-kanban-zoom-out]");
    if (await button.isDisabled()) break;
    await button.click();
  }
  await page.screenshot({ path: path.join(output, "kanban-mobile-min.png") });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.locator("[data-kanban-zoom-reset]").click();
  await page.locator("[data-kanban-root]").scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(output, "kanban-desktop-100.png") });
  await page.locator("[data-kanban-zoom-fit]").click();
  await page.screenshot({ path: path.join(output, "kanban-desktop-overview.png") });
});
