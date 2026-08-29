/**
 * Exponential price formatter for Solana shitcoin prices.
 *
 * Displays prices like 0.00000000456 as "4.56⁻⁹" (mantissa × 10^exponent).
 *
 * Rules:
 * - 2 decimal places for the mantissa
 * - Superscript exponent (no "10^" prefix — user knows the convention)
 * - For the same token, all prices share the same exponent (based on entry price)
 *   so magnitude changes are visible at a glance
 * - Prices >= 0.01 display normally (no exponent needed)
 */

const SUPERSCRIPT_DIGITS: Record<string, string> = {
  "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
  "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
  "-": "⁻",
};

function toSuperscript(num: number): string {
  return num
    .toString()
    .split("")
    .map((ch) => SUPERSCRIPT_DIGITS[ch] ?? ch)
    .join("");
}

/**
 * Get the base-10 exponent for a price (floor of log10).
 * e.g. 4.56e-9 → -9, 0.00123 → -3, 150 → 2
 */
export function getPriceExponent(price: number): number {
  if (price <= 0) return 0;
  return Math.floor(Math.log10(price));
}

/**
 * Format a single price with exponential notation.
 * Returns { mantissa: string, exponent: number } for composition.
 *
 * @param price - The raw price value
 * @param decimals - Decimal places for mantissa (default 2)
 */
export function formatPriceComponents(
  price: number,
  decimals: number = 2
): { mantissa: string; exponent: number; display: string } {
  if (price <= 0) {
    return { mantissa: "0", exponent: 0, display: "$0.00" };
  }

  // For prices >= 0.01, show normally
  if (price >= 0.01) {
    return {
      mantissa: price.toFixed(decimals),
      exponent: 0,
      display: `$${price.toFixed(decimals)}`,
    };
  }

  const exponent = getPriceExponent(price);
  const mantissa = price / Math.pow(10, exponent);
  const mantissaStr = mantissa.toFixed(decimals);
  const sup = toSuperscript(exponent);

  return {
    mantissa: mantissaStr,
    exponent,
    display: `$${mantissaStr}${sup}`,
  };
}

/**
 * Format a price for display. Returns the full formatted string.
 */
export function formatPrice(price: number, decimals: number = 2): string {
  return formatPriceComponents(price, decimals).display;
}

/**
 * Format two prices (entry + peak) for the same token, ensuring
 * they share the same exponent (based on entry price).
 *
 * Returns { entry: string, peak: string } with matching superscripts.
 */
export function formatPricePair(
  entryPrice: number,
  peakPrice: number | null,
  decimals: number = 2
): { entry: string; peak: string; sharedExponent: number } {
  if (entryPrice <= 0) {
    return {
      entry: "$0.00",
      peak: peakPrice ? formatPrice(peakPrice, decimals) : "—",
      sharedExponent: 0,
    };
  }

  // Determine exponent from entry price
  let exponent: number;
  if (entryPrice >= 0.01) {
    exponent = 0;
  } else {
    exponent = getPriceExponent(entryPrice);
  }

  const entryMantissa = entryPrice / Math.pow(10, exponent);
  const entryStr =
    exponent === 0
      ? `$${entryMantissa.toFixed(decimals)}`
      : `$${entryMantissa.toFixed(decimals)}${toSuperscript(exponent)}`;

  let peakStr = "—";
  if (peakPrice !== null && peakPrice > 0) {
    const peakMantissa = peakPrice / Math.pow(10, exponent);
    peakStr =
      exponent === 0
        ? `$${peakMantissa.toFixed(decimals)}`
        : `$${peakMantissa.toFixed(decimals)}${toSuperscript(exponent)}`;
  }

  return { entry: entryStr, peak: peakStr, sharedExponent: exponent };
}

/**
 * Format a multiplier (e.g. 11.1 → "11.1x").
 */
export function formatMultiplier(value: number | null, decimals: number = 1): string {
  if (value === null || value === undefined) return "—";
  return `${value.toFixed(decimals)}x`;
}

/**
 * Format a large number with K/M/B suffixes.
 */
export function formatCompact(value: number | null): string {
  if (value === null || value === undefined) return "—";
  if (value >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(2)}B`;
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return value.toFixed(2);
}

/**
 * Format a relative time (e.g. "5d ago", "11h ago", "36m ago").
 */
export function formatRelativeTime(isoTimestamp: string): string {
  const now = new Date();
  const then = new Date(isoTimestamp);
  const diffMs = now.getTime() - then.getTime();
  const diffSec = Math.floor(diffMs / 1000);

  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.floor(diffHr / 24);
  return `${diffDay}d ago`;
}
