function finite(value) {
  return Number.isFinite(Number(value)) ? Number(value) : 0;
}

export function normalizedRect(rect) {
  const left = Math.min(finite(rect.left ?? rect.x), finite(rect.right ?? (rect.x + rect.width)));
  const right = Math.max(finite(rect.left ?? rect.x), finite(rect.right ?? (rect.x + rect.width)));
  const top = Math.min(finite(rect.top ?? rect.y), finite(rect.bottom ?? (rect.y + rect.height)));
  const bottom = Math.max(finite(rect.top ?? rect.y), finite(rect.bottom ?? (rect.y + rect.height)));
  return { left, top, right, bottom, width: right - left, height: bottom - top };
}

export function intersectionArea(first, second) {
  const a = normalizedRect(first);
  const b = normalizedRect(second);
  return Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left)) *
    Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top));
}

export function mergeLineRects(rects, tolerance = 2) {
  const sorted = rects
    .map(normalizedRect)
    .filter((rect) => rect.width > 0.5 && rect.height > 0.5)
    .sort((a, b) => a.top - b.top || a.left - b.left);
  const merged = [];
  for (const rect of sorted) {
    const previous = merged.at(-1);
    const sameLine = previous &&
      Math.abs(previous.top - rect.top) <= tolerance &&
      Math.abs(previous.bottom - rect.bottom) <= tolerance &&
      rect.left <= previous.right + tolerance * 2;
    if (sameLine) {
      previous.left = Math.min(previous.left, rect.left);
      previous.right = Math.max(previous.right, rect.right);
      previous.top = Math.min(previous.top, rect.top);
      previous.bottom = Math.max(previous.bottom, rect.bottom);
      previous.width = previous.right - previous.left;
      previous.height = previous.bottom - previous.top;
    } else {
      merged.push({ ...rect });
    }
  }
  return merged;
}

export function viewportRectToPdfQuad(viewport, rect) {
  const normalized = normalizedRect(rect);
  const first = viewport.convertToPdfPoint(normalized.left, normalized.top);
  const second = viewport.convertToPdfPoint(normalized.right, normalized.bottom);
  return [
    Math.min(first[0], second[0]),
    Math.min(first[1], second[1]),
    Math.max(first[0], second[0]),
    Math.max(first[1], second[1]),
  ].map((value) => Math.round(value * 10_000) / 10_000);
}

export function pdfQuadToViewportRect(viewport, quad) {
  const first = viewport.convertToViewportPoint(quad[0], quad[1]);
  const second = viewport.convertToViewportPoint(quad[2], quad[3]);
  return normalizedRect({
    left: first[0],
    top: first[1],
    right: second[0],
    bottom: second[1],
  });
}

export function groupSelectionRects(selectionRects, pages) {
  const groups = new Map();
  for (const rawRect of selectionRects) {
    const rect = normalizedRect(rawRect);
    if (rect.width <= 0.5 || rect.height <= 0.5) continue;
    const page = pages
      .map((candidate) => ({ candidate, area: intersectionArea(rect, candidate.bounds) }))
      .sort((a, b) => b.area - a.area)[0];
    if (!page || page.area <= 0) continue;
    const bounds = normalizedRect(page.candidate.bounds);
    const clipped = {
      left: Math.max(rect.left, bounds.left) - bounds.left,
      top: Math.max(rect.top, bounds.top) - bounds.top,
      right: Math.min(rect.right, bounds.right) - bounds.left,
      bottom: Math.min(rect.bottom, bounds.bottom) - bounds.top,
    };
    const list = groups.get(page.candidate.pageNumber) || {
      page_number: page.candidate.pageNumber,
      page_rotation: page.candidate.rotation || 0,
      viewport: page.candidate.viewport,
      rects: [],
    };
    list.rects.push(clipped);
    groups.set(page.candidate.pageNumber, list);
  }
  return [...groups.values()]
    .sort((a, b) => a.page_number - b.page_number)
    .map((group) => ({
      page_number: group.page_number,
      page_rotation: group.page_rotation,
      quads: mergeLineRects(group.rects).map((rect) =>
        viewportRectToPdfQuad(group.viewport, rect)
      ),
    }))
    .filter((group) => group.quads.length > 0);
}
