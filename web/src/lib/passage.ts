/** Where an anchored passage starts and ends in an element's text content. */
export interface Passage { start: number; end: number }

/** A stored source position, quoted text, or both. */
export interface PassageAnchor { start?: number; end?: number; text?: string }

/** Lower-cased text with each whitespace run reduced to one space, mapped back to source offsets. */
function fold(text: string): { text: string; starts: number[]; ends: number[] } {
  let folded = "";
  const starts: number[] = [];
  const ends: number[] = [];
  let offset = 0;
  let gap = -1;
  for (const char of text) {
    if (/\s/u.test(char)) {
      if (gap < 0) gap = offset;
    } else {
      if (gap >= 0 && folded) { folded += " "; starts.push(gap); ends.push(offset); }
      gap = -1;
      const lower = char.toLowerCase();
      for (let unit = 0; unit < lower.length; unit++) { starts.push(offset); ends.push(offset + char.length); }
      folded += lower;
    }
    offset += char.length;
  }
  return { text: folded, starts, ends };
}

/**
 * Find an anchored passage in rendered text: stored offsets while they still hold the quoted text,
 * else the quoted text itself, else the same text ignoring case and differences in whitespace
 * (rendering joins paragraph breaks and repeated spaces).
 */
export function locatePassage(content: string, anchor: PassageAnchor): Passage | null {
  const { start = -1, end = -1, text } = anchor;
  if (start >= 0 && end > start && end <= content.length && (!text || content.slice(start, end) === text)) return { start, end };
  if (!text) return null;
  const exact = content.indexOf(text);
  if (exact >= 0) return { start: exact, end: exact + text.length };
  const needle = fold(text).text;
  if (!needle) return null;
  const haystack = fold(content);
  const found = haystack.text.indexOf(needle);
  return found < 0 ? null : { start: haystack.starts[found], end: haystack.ends[found + needle.length - 1] };
}
