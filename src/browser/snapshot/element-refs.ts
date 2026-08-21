import type { ElementRef } from "../types";

export class ElementRefMapper {
  private refs = new Map<string, ElementRef>();

  replace(elements: ElementRef[]) {
    this.refs = new Map(elements.map((element) => [element.ref, element]));
  }

  get(ref: string) {
    return this.refs.get(ref);
  }

}
