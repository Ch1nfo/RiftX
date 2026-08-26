const LATEST_THRESHOLD = 24;
const SCROLL_DIRECTION_TOLERANCE = 1;

export function resolveConversationScroll(input: {
  wasFollowing: boolean;
  previousScrollTop: number;
  scrollTop: number;
  distanceFromBottom: number;
}) {
  const atLatest = input.distanceFromBottom <= LATEST_THRESHOLD;
  const movedUp = input.scrollTop < input.previousScrollTop - SCROLL_DIRECTION_TOLERANCE;
  // Collapsing a thinking/tool details block can reduce scrollHeight and make
  // the browser clamp scrollTop downward while the user is still at the end.
  // Being at the latest position is authoritative; only an upward move away
  // from the end represents an intentional pause.
  const shouldFollow = atLatest ? true : movedUp ? false : input.wasFollowing;
  return { atLatest, shouldFollow };
}

// Mirrors what scrollIntoView({ block: "center" }) will do to the container:
// align the target's center with the scrollport center, then clamp into the
// scrollable range. At either edge (or with no overflow) the clamped landing
// equals the current scrollTop and nothing scrolls — so callers must decide
// from this clamped landing, not from raw rects, whether a programmatic
// scroll is real before they act on it (e.g. before pausing auto-follow).
export function willCenterScrollMove(input: {
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
  targetTop: number;
  targetHeight: number;
}) {
  const desired = input.targetTop + input.targetHeight / 2 - input.clientHeight / 2;
  const max = Math.max(0, input.scrollHeight - input.clientHeight);
  const landed = Math.min(max, Math.max(0, desired));
  return Math.abs(landed - input.scrollTop) > SCROLL_DIRECTION_TOLERANCE;
}
