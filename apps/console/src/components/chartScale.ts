/** Axis helpers for the telemetry chart (pure, unit-tested). */

/** "Nice" tick step (1, 2, 2.5, 3, 4, 5, 6, 8 x 10^n) for roughly `count` intervals. */
export function niceStep(span: number, count: number): number {
  if (!(span > 0) || !(count > 0)) return 1;
  const raw = span / count;
  const pow = 10 ** Math.floor(Math.log10(raw));
  const f = raw / pow;
  const nice = [1, 2, 2.5, 3, 4, 5, 6, 8, 10].find((s) => f <= s) ?? 10;
  return nice * pow;
}

export interface Scale {
  readonly min: number;
  readonly max: number;
  readonly ticks: readonly number[];
}

/**
 * Y scale that includes the data, the optional reference (baseline), and zero when
 * the data sit far above it (so a speed drop is not visually exaggerated).
 */
export function yScale(values: readonly number[], reference?: number, intervals = 4): Scale {
  const finite = values.filter(Number.isFinite);
  if (reference !== undefined && Number.isFinite(reference)) finite.push(reference);
  if (finite.length === 0) return { min: 0, max: 1, ticks: [0, 1] };
  let lo = Math.min(...finite);
  let hi = Math.max(...finite);
  if (lo > 0 && lo < hi * 0.6) lo = 0;
  if (reference !== undefined && lo > 0) lo = 0;
  if (hi - lo < 1e-9) { hi += Math.abs(hi) * 0.1 || 1; lo -= Math.abs(lo) * 0.1 || 1; }
  const step = niceStep((hi - lo) * 1.15, intervals);
  const min = Math.floor(lo / step) * step;
  const max = Math.ceil((hi + (hi - lo) * 0.12) / step) * step;
  const ticks: number[] = [];
  for (let v = min; v <= max + step / 2; v += step) ticks.push(Number(v.toFixed(10)));
  return { min, max, ticks };
}
