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
