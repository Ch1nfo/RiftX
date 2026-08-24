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
  const shouldFollow = movedUp ? false : atLatest ? true : input.wasFollowing;
  return { atLatest, shouldFollow };
}
