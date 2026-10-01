/**
 * Local reading of the `pages` selection, e.g. `"1-3, 7"`.
 *
 * The API picks pages out of the whole submission laid end to end (an image is one
 * page, a PDF is its own page count). This module only answers what can be known
 * without the files: whether the spelling is valid, and how many pages it selects.
 *
 * **It must never refuse a selection the API would accept.** Every rule here is the
 * contract's, and every one is pinned identically in the Python client, so a
 * selection means the same thing to both. Digits are ASCII only.
 */

import { InvalidPagesError, PagesOutOfRangeError } from "./errors.js";

/**
 * Longest `pages` value the API accepts. Measured in UTF-16 units here and in
 * characters by the Python client; the two only differ for characters outside the
 * Basic Multilingual Plane, and any such character is a spelling error on both sides
 * anyway.
 */
export const MAX_PAGES_LENGTH = 1000;

const TOKEN = /^(\d+)(?:\s*-\s*(\d+))?$/;

export type PageRanges = Array<[number, number]>;

/** Whether `text` is empty once surrounding whitespace is removed. */
export function isBlank(text: string): boolean {
  return text.trim() === "";
}

/**
 * Return `pages` if it selects anything, or undefined when it counts as not given.
 *
 * `undefined`, `null`, an empty string, and a string of nothing but whitespace all
 * mean "no selection" to the service, so none of them is sent. Anything but a string
 * is a caller mistake: sent as a form field it would silently become text, and in a
 * JSON body it would be refused — the same call behaving two different ways.
 */
export function normalizePages(pages: unknown): string | undefined {
  if (pages == null) return undefined;
  if (typeof pages !== "string") {
    throw new TypeError(`pages must be a string like '1-3,7', not ${typeof pages}`);
  }
  return isBlank(pages) ? undefined : pages;
}

/**
 * Parse a non-empty selection into `[start, end]` ranges, as written.
 *
 * @throws InvalidPagesError The spelling is wrong (`INVALID_PAGES`). Every token is
 *   checked for spelling before any range is, so `"0,abc"` is a spelling error, as it
 *   is to the service.
 * @throws PagesOutOfRangeError A page number is 0 (`PAGES_OUT_OF_RANGE`).
 */
export function parsePages(pages: string): PageRanges {
  // Measured after trimming: surrounding whitespace is not part of the value.
  const length = pages.trim().length;
  if (length > MAX_PAGES_LENGTH) {
    throw new InvalidPagesError(
      `pages is ${length} characters long, over the ${MAX_PAGES_LENGTH} allowed`,
      { code: "INVALID_PAGES" },
    );
  }
  const ranges: PageRanges = [];
  for (const rawToken of pages.split(",")) {
    const match = TOKEN.exec(rawToken.trim());
    const start = match ? Number(match[1]) : 0;
    const end = match && match[2] !== undefined ? Number(match[2]) : start;
    // 2**53 - 1 is the largest page number the API accepts; past it the answer is
    // INVALID_PAGES, not an out-of-range page — so it is here too.
    if (!match || !Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start > end) {
      throw new InvalidPagesError(
        `pages ${JSON.stringify(pages)} is not a valid selection: write single pages or ` +
          "start-end ranges separated by commas, e.g. '1-3, 7'",
        { code: "INVALID_PAGES" },
      );
    }
    ranges.push([start, end]);
  }
  if (ranges.some(([start]) => start < 1)) {
    throw new PagesOutOfRangeError(
      `pages ${JSON.stringify(pages)} selects page 0; pages are numbered from 1`,
      { code: "PAGES_OUT_OF_RANGE" },
    );
  }
  return ranges;
}

/**
 * How many distinct pages the ranges select, overlaps counted once.
 *
 * Counted by merging, never by expanding: `"1-1000000000000"` is a valid spelling,
 * and listing it out would be a very long wait for the answer "too many".
 */
export function selectedPageCount(ranges: PageRanges): number {
  let count = 0;
  let coveredTo = 0;
  for (const [start, end] of [...ranges].sort((a, b) => a[0] - b[0] || a[1] - b[1])) {
    if (end <= coveredTo) continue;
    count += end - Math.max(start, coveredTo + 1) + 1;
    coveredTo = end;
  }
  return count;
}
