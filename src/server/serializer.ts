/**
 * Minimal tail-chain mutex: runs operations one at a time in submission
 * order, swallowing the previous failure so one rejected operation never
 * poisons the chain. Shared by config writes, evidence persistence, subagent
 * persistence/naming, and browser operation serialization.
 */
export function createSerializer() {
  let tail: Promise<unknown> = Promise.resolve();
  return <T>(operation: () => Promise<T>): Promise<T> => {
    const result = tail.then(operation);
    tail = result.then(() => undefined, () => undefined);
    return result;
  };
}
