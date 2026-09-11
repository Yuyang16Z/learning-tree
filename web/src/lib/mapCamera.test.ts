import { describe, expect, it } from "vitest";
import { navigateMapView } from "./mapCamera";

describe("reversible map positioning", () => {
  const original = { x: 76, y: -640, zoom: 0.72 };
  const focused = { x: 30, y: -180, zoom: 1 };
  const overview = { x: 50, y: 10, zoom: 0.25 };

  it("restores the exact previous position and scale when focus is pressed again", () => {
    const jump = navigateMapView(original, focused, "focus", null);
    const restored = navigateMapView(jump.camera, focused, "focus", jump.returnView);
    expect(restored).toEqual({ camera: original, returnView: null });
  });

  it("keeps the return position after free panning and zooming", () => {
    const jump = navigateMapView(original, focused, "focus", null);
    const manuallyMoved = { x: 480, y: 310, zoom: 1.4 };
    expect(navigateMapView(manuallyMoved, focused, "focus", jump.returnView).camera).toEqual(original);
  });

  it("retains the original place when switching between focus and overview", () => {
    const focus = navigateMapView(original, focused, "focus", null);
    const full = navigateMapView(focus.camera, overview, "overview", focus.returnView);
    expect(full.camera).toEqual(overview);
    expect(full.returnView?.camera).toEqual(original);
    expect(navigateMapView(full.camera, overview, "overview", full.returnView)).toEqual({ camera: original, returnView: null });
  });
});
