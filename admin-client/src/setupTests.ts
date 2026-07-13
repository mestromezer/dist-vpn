import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

// react-testing-library auto-detects Jest's global `afterEach` to run its
// DOM cleanup between tests; this project's vitest.config doesn't set
// `test.globals: true`, so that auto-detection doesn't fire and multiple
// `it()`/`it.each` cases in one file would otherwise leave every previous
// render's DOM mounted (e.g. several "Test connection" buttons at once).
// Registering it explicitly here is the standard fix for non-`globals`
// vitest setups.
afterEach(() => {
  cleanup();
});

// jsdom doesn't implement ResizeObserver, which reactflow's <ReactFlow>
// needs to measure node/pane dimensions before it will reveal nodes or
// compute edge paths (nodes otherwise stay `visibility: hidden` and edges
// never render at all). The stub below fires the callback once per observed
// target with a fixed, plausible size so reactflow's measurement step
// actually completes in tests — real pixel values don't matter here, only
// that measurement resolves so nodes/edges show up in the DOM for
// topology/TopologyPage.test.tsx to assert against.
class ResizeObserverStub {
  #callback: ResizeObserverCallback;

  constructor(callback: ResizeObserverCallback) {
    this.#callback = callback;
  }

  observe(target: Element) {
    const rect: DOMRectReadOnly = {
      width: 150,
      height: 40,
      top: 0,
      left: 0,
      bottom: 40,
      right: 150,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    };
    const entry: ResizeObserverEntry = {
      target,
      contentRect: rect,
      borderBoxSize: [{ inlineSize: 150, blockSize: 40 }] as unknown as ReadonlyArray<ResizeObserverSize>,
      contentBoxSize: [{ inlineSize: 150, blockSize: 40 }] as unknown as ReadonlyArray<ResizeObserverSize>,
      devicePixelContentBoxSize: [
        { inlineSize: 150, blockSize: 40 },
      ] as unknown as ReadonlyArray<ResizeObserverSize>,
    };
    // Async (microtask), like a real ResizeObserver callback, so it never
    // fires synchronously inside a React render/commit.
    queueMicrotask(() => this.#callback([entry], this as unknown as ResizeObserver));
  }

  unobserve() {}
  disconnect() {}
}

if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver;
}

// jsdom also doesn't implement DOMMatrixReadOnly, which reactflow's
// `updateNodeDimensions` reads (to derive the current zoom/scale) every time
// the ResizeObserver stub above fires. Without this stub the callback throws
// a raw exception ("window.DOMMatrixReadOnly is not a constructor") that
// vitest reports as an *unhandled* error outside the test's own try/catch —
// which leaves reactflow's internal "measured" flag stuck false, so nodes
// stay `visibility: hidden` and edges (which reactflow only draws once both
// endpoints are measured) never appear in the DOM. This is the same minimal
// stub reactflow's own testing docs recommend for jsdom environments — only
// `m22` (vertical scale) is read by the code path exercised here, so that's
// the only field implemented.
class DOMMatrixReadOnlyStub {
  m22 = 1;

  constructor(transform?: string) {
    const scale = transform?.match(/scale\(([\d.]+)\)/)?.[1];
    this.m22 = scale !== undefined ? Number(scale) : 1;
  }
}

if (typeof globalThis.DOMMatrixReadOnly === 'undefined') {
  globalThis.DOMMatrixReadOnly = DOMMatrixReadOnlyStub as unknown as typeof DOMMatrixReadOnly;
}

// jsdom has no layout engine, so every element's offsetWidth/offsetHeight
// and getBoundingClientRect() are always 0 — but reactflow's dimension
// measurement (updateNodeDimensions -> getDimensions/getHandleBounds) reads
// exactly those APIs to decide when a node is "initialized" (and therefore
// switches from `visibility: hidden` to visible) and where each handle sits,
// which in turn is what lets it compute and draw an edge's path/label at
// all. Without non-zero values here, nodes and edges are added to the DOM
// but never become visible/connected, so `TopologyPage.test.tsx` can't find
// the edge label it asserts on even though the graph "renders". Fixed pixel
// sizes (not real measurement) are fine — only non-zero-ness matters for
// reactflow's own initialization checks.
Object.defineProperties(HTMLElement.prototype, {
  offsetWidth: { configurable: true, get: () => 150 },
  offsetHeight: { configurable: true, get: () => 40 },
});

const boundingClientRectStub = (): DOMRect => ({
  width: 150,
  height: 40,
  top: 0,
  left: 0,
  bottom: 40,
  right: 150,
  x: 0,
  y: 0,
  toJSON: () => ({}),
});
Element.prototype.getBoundingClientRect = boundingClientRectStub;

// jsdom doesn't implement getBBox() on SVG elements either (used by
// reactflow's <EdgeText> to size the label background rect behind an edge's
// status text) — without this stub, mounting an edge with a label throws
// synchronously and React Router's error boundary swallows the whole page,
// which is why the graph disappeared entirely rather than just missing a
// label.
//
// This has to be patched on SVGElement.prototype, not SVGGraphicsElement.prototype:
// lib.dom's *types* only declare getBBox on SVGGraphicsElement (hence the
// cast below), but jsdom's actual SVG element instances (verified: an
// element created via `document.createElementNS(SVG_NS, 'text')`) inherit
// directly from SVGElement — jsdom exposes an `SVGGraphicsElement` global
// constructor, but doesn't actually wire it into the prototype chain it
// builds for real SVG elements. Patching SVGGraphicsElement.prototype (as a
// first attempt here did) silently patches a prototype no instance actually
// uses, so the fix has to target the class jsdom instances really inherit
// from.
type SVGElementWithBBox = SVGElement & { getBBox(): DOMRect };
if (
  typeof SVGElement !== 'undefined' &&
  !(SVGElement.prototype as SVGElementWithBBox).getBBox
) {
  (SVGElement.prototype as SVGElementWithBBox).getBBox = (): DOMRect => ({
    x: 0,
    y: 0,
    width: 150,
    height: 40,
    top: 0,
    left: 0,
    bottom: 40,
    right: 150,
    toJSON: () => ({}),
  });
}
