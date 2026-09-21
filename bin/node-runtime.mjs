/**
 * Node 24.19+ backported ObjectWrap cleanup hooks without the registry needed
 * by native addons such as better-sqlite3. Their GC cleanup can abort Node.
 */
export function hasNode24CleanupRegression(version = process.versions.node) {
  const match = /^(\d+)\.(\d+)\./.exec(version);
  if (!match) return false;
  return Number(match[1]) === 24 && Number(match[2]) >= 19;
}

export function reportUnsupportedNode(version = process.versions.node) {
  if (!hasNode24CleanupRegression(version)) return false;
  console.error("RiftX cannot start on Node.js 24.19+ because of an upstream native-addon cleanup regression.");
  console.error("Use Node.js 22 LTS (recommended), Node.js 20.18.1+, or Node.js 24.18.1 and reinstall dependencies.");
  return true;
}
