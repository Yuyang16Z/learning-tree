export type Camera = { x: number; y: number; zoom: number };
export type MapViewAction = "focus" | "overview";
export type ReturnView = { action: MapViewAction; camera: Camera };

/** A positioning shortcut can be undone even after the user pans or zooms. */
export function navigateMapView(
  current: Camera,
  target: Camera,
  action: MapViewAction,
  returnView: ReturnView | null,
): { camera: Camera; returnView: ReturnView | null } {
  if (returnView?.action === action) {
    return { camera: { ...returnView.camera }, returnView: null };
  }
  return {
    camera: target,
    // Switching from focus to overview should not lose the original place.
    returnView: { action, camera: { ...(returnView?.camera ?? current) } },
  };
}
