/** An outcome or verdict as a small outlined word; its colour says which way it went. */
export function Pill({ value }: { readonly value: string }) {
  return <span className={`sum-pill sum-pill-${value}`}>{value}</span>;
}
