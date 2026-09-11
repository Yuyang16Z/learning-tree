export function readSaved<T>(key: string, fallback: T): T {
  try { return JSON.parse(localStorage.getItem(key) ?? 'null') ?? fallback; } catch { return fallback; }
}
export function saveValue(key: string, value: unknown) {
  try { localStorage.setItem(key, JSON.stringify(value)); return true; } catch { return false; }
}
/** A response may commit only while its navigation ticket is still current. */
export class NavigationGate {
  private revision = 0;
  next() { return ++this.revision; }
  current(ticket: number) { return ticket === this.revision; }
}
