/** Size a canvas's backing store for the device pixel ratio, set its CSS
 *  size, scale the context so drawing is in CSS pixels, and clear it (R08.12;
 *  was eight lines repeated in Sparkline, Curve, Heatmap and
 *  MultiSeriesChart). Null when the canvas has no 2D context. */
export function prepareCanvas(
  canvas: HTMLCanvasElement,
  width: number,
  height: number,
): CanvasRenderingContext2D | null {
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = width * dpr;
  canvas.height = height * dpr;
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return ctx;
}
