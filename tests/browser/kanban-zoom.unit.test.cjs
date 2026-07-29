const test = require("node:test");
const assert = require("node:assert/strict");

const zoom = require("../../src/app/dashboard/static/dashboard.js");

test("normalizes and clamps Kanban scale", () => {
  assert.equal(zoom.normalizeScale(0.2), 0.4);
  assert.equal(zoom.normalizeScale(0.65), 0.65);
  assert.equal(zoom.normalizeScale(1.8), 1.2);
  assert.equal(zoom.normalizeScale("broken"), 1);
});

test("restores valid localStorage state", () => {
  const storage = {
    getItem: () => JSON.stringify({ scale: 0.7, mode: "fit" }),
    setItem: () => assert.fail("valid state must not be rewritten"),
  };
  assert.deepEqual(zoom.loadScaleState(storage), { scale: 0.7, mode: "fit" });
});

test("resets corrupted localStorage state to 100 percent", () => {
  let reset = "";
  const storage = {
    getItem: () => "not-json",
    setItem: (key, value) => {
      assert.equal(key, zoom.STORAGE_KEY);
      reset = value;
    },
  };
  assert.deepEqual(zoom.loadScaleState(storage), { scale: 1, mode: "manual" });
  assert.deepEqual(JSON.parse(reset), { scale: 1, mode: "manual" });
});

test("fit scale uses both axes, never exceeds 100 percent, and respects minimum", () => {
  assert.equal(zoom.calculateFitScale(1200, 800, 600, 400), 1);
  assert.equal(zoom.calculateFitScale(600, 400, 1200, 800), 0.5);
  assert.equal(zoom.calculateFitScale(360, 500, 1800, 900), 0.4);
});
