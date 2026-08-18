import type { Page } from "playwright";
import { ElementRefMapper } from "./element-refs";
import type { ElementKind, ElementRef, FormRef, PageSnapshot } from "../types";

type SnapshotNode = { kind: ElementKind; name: string; selector: string; type?: string; formRef?: string };
type SnapshotData = { elements: SnapshotNode[]; forms: Array<{ ref: string; fields: Record<string, number> }>; visibleText: string };

function normalizeText(value: string) { return value.replace(/\s+/g, " ").trim(); }

// Keep the browser-side program as a string: tsx/esbuild helpers cannot leak into page.evaluate.
const SNAPSHOT_SCRIPT = `(() => {
  const interactive = Array.from(document.querySelectorAll('button, a[href], input, textarea, select')).filter(function (node) {
    const style = window.getComputedStyle(node); const rect = node.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  });
  function selectorFor(node) {
    const parts = []; let current = node;
    while (current && current !== document.body) {
      const parent = current.parentElement; if (!parent) break;
      const siblings = Array.from(parent.children).filter(function (child) { return child.tagName === current.tagName; });
      parts.unshift(current.tagName.toLowerCase() + ':nth-of-type(' + (siblings.indexOf(current) + 1) + ')'); current = parent;
    }
    return 'body > ' + parts.join(' > ');
  }
  function labelFor(node) {
    const id = node.getAttribute('id');
    const explicit = id ? document.querySelector('label[for="' + CSS.escape(id) + '"]')?.textContent : undefined;
    const aria = node.getAttribute('aria-label') || node.getAttribute('placeholder');
    const wrapped = node.closest('label')?.textContent;
    const text = explicit || aria || wrapped || node.textContent || node.name || node.type || node.tagName.toLowerCase();
    return text.replace(/\\s+/g, ' ').trim().slice(0, 160);
  }
  const formNames = new Map(); Array.from(document.forms).forEach(function (form, index) { formNames.set(form, 'f' + (index + 1)); });
  const elements = interactive.map(function (node) {
    const tag = node.tagName.toLowerCase(); const inputType = tag === 'input' ? (node.type || 'text').toLowerCase() : undefined;
    const kind = inputType === 'checkbox' ? 'checkbox' : inputType === 'radio' ? 'radio' : tag === 'a' ? 'link' : tag;
    const form = node.closest('form'); return { kind, name: labelFor(node), selector: selectorFor(node), type: inputType, formRef: form ? formNames.get(form) : undefined };
  });
  const indexByNode = new Map(); interactive.forEach(function (node, index) { indexByNode.set(node, index); });
  const forms = Array.from(document.forms).map(function (form) {
    const fields = {}; const formRef = formNames.get(form);
    Array.from(form.elements).forEach(function (field) {
      const index = indexByNode.get(field); if (index === undefined) return;
      let key = field.name || field.id || field.getAttribute('aria-label') || field.getAttribute('placeholder') || field.closest('label')?.textContent || field.tagName.toLowerCase();
      key = key.replace(/\\s+/g, ' ').trim().slice(0, 80) || field.tagName.toLowerCase();
      const base = key; let suffix = 2; while (fields[key] !== undefined) key = base + '_' + suffix++;
      fields[key] = index;
    }); return { ref: formRef, fields };
  });
  return { elements, forms, visibleText: document.body.innerText.replace(/\\s+/g, ' ').trim().slice(0, 12000) };
})()`;

export async function createSnapshot(page: Page, mapper: ElementRefMapper): Promise<PageSnapshot> {
  const data = await page.evaluate(SNAPSHOT_SCRIPT) as SnapshotData;
  const elements: ElementRef[] = data.elements.map((item, index) => ({ ...item, ref: `e${index + 1}` }));
  mapper.replace(elements);
  const forms: FormRef[] = data.forms.map((form) => ({
    ref: form.ref,
    fields: Object.fromEntries(Object.entries(form.fields).map(([name, index]) => [name, elements[index]?.ref ?? `e${index + 1}`]))
  }));
  const title = normalizeText(await page.title());
  const lines = [
    `URL: ${page.url()}`, `Title: ${title || "(untitled)"}`, "", "Interactive Elements:",
    ...elements.map((element) => `[${element.ref}] ${element.kind}${element.type ? ` type=${element.type}` : ""} \"${element.name || element.kind}\"`),
    "", "Forms:", ...(forms.length ? forms.flatMap((form) => [`[${form.ref}]`, ...Object.entries(form.fields).map(([name, ref]) => `  ${name} -> ${ref}`)]) : ["(none)"]),
    "", "Visible Text:", data.visibleText || "(none)"
  ];
  return { url: page.url(), title, elements, forms, visibleText: data.visibleText, text: lines.join("\n") };
}
