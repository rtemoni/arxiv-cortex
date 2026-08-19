import assert from "node:assert/strict";
import test from "node:test";

import {
  groupSelectionRects,
  mergeLineRects,
  pdfQuadToViewportRect,
  viewportRectToPdfQuad,
} from "../../src/arxiv_cortex/static/reader-geometry.mjs";

const viewport = {
  convertToPdfPoint(x, y) { return [x, 100 - y]; },
  convertToViewportPoint(x, y) { return [x, 100 - y]; },
};

test("merges adjacent rectangles on the same text line", () => {
  assert.deepEqual(
    mergeLineRects([
      { left: 10, top: 20, right: 40, bottom: 31 },
      { left: 41, top: 20.5, right: 80, bottom: 31.5 },
      { left: 10, top: 40, right: 35, bottom: 51 },
    ]),
    [
      { left: 10, top: 20, right: 80, bottom: 31.5, width: 70, height: 11.5 },
      { left: 10, top: 40, right: 35, bottom: 51, width: 25, height: 11 },
    ],
  );
});

test("round-trips a viewport rectangle through PDF coordinates", () => {
  const quad = viewportRectToPdfQuad(viewport, { left: 12, top: 20, right: 90, bottom: 34 });
  assert.deepEqual(quad, [12, 66, 90, 80]);
  assert.deepEqual(
    pdfQuadToViewportRect(viewport, quad),
    { left: 12, top: 20, right: 90, bottom: 34, width: 78, height: 14 },
  );
});

test("keeps PDF geometry stable across zoom changes", () => {
  const zoomedViewport = {
    convertToPdfPoint(x, y) { return [x / 2, 100 - y / 2]; },
    convertToViewportPoint(x, y) { return [x * 2, (100 - y) * 2]; },
  };
  const quad = viewportRectToPdfQuad(
    zoomedViewport,
    { left: 20, top: 40, right: 100, bottom: 60 },
  );

  assert.deepEqual(quad, [10, 70, 50, 80]);
  assert.deepEqual(
    pdfQuadToViewportRect(zoomedViewport, quad),
    { left: 20, top: 40, right: 100, bottom: 60, width: 80, height: 20 },
  );
});

test("normalizes rectangle corners for a rotated viewport", () => {
  const rotatedViewport = {
    convertToPdfPoint(x, y) { return [y, x]; },
    convertToViewportPoint(x, y) { return [y, x]; },
  };
  const quad = viewportRectToPdfQuad(
    rotatedViewport,
    { left: 12, top: 30, right: 48, bottom: 44 },
  );

  assert.deepEqual(quad, [30, 12, 44, 48]);
  assert.deepEqual(
    pdfQuadToViewportRect(rotatedViewport, quad),
    { left: 12, top: 30, right: 48, bottom: 44, width: 36, height: 14 },
  );
});

test("groups a multi-page selection and clips browser rectangles to each page", () => {
  const fragments = groupSelectionRects(
    [
      { left: 20, top: 15, right: 90, bottom: 26 },
      { left: 20, top: 135, right: 110, bottom: 147 },
    ],
    [
      { pageNumber: 1, rotation: 0, viewport, bounds: { left: 10, top: 10, right: 120, bottom: 110 } },
      { pageNumber: 2, rotation: 90, viewport, bounds: { left: 10, top: 130, right: 120, bottom: 230 } },
    ],
  );

  assert.deepEqual(fragments, [
    { page_number: 1, page_rotation: 0, quads: [[10, 84, 80, 95]] },
    { page_number: 2, page_rotation: 90, quads: [[10, 83, 100, 95]] },
  ]);
});
