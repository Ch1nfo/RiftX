import { Semaphore } from "./semaphore";

export class BashConcurrency extends Semaphore {
  constructor(limit: number) {
    super(limit, "Bash execution was aborted");
  }
}
