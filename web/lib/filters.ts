export interface Filters {
  universe: string;
  sector: string;
  cap: string;
  consensus: string;
  q: string;
  minScore: number;
  maxScore: number;
  analyzedOnly: boolean;
  watchlistOnly: boolean;
}

export const DEFAULT_FILTERS: Filters = {
  universe: "",
  sector: "",
  cap: "",
  consensus: "",
  q: "",
  minScore: 0,
  maxScore: 100,
  analyzedOnly: false,
  watchlistOnly: false,
};

export function activeFilterCount(f: Filters): number {
  let n = 0;
  if (f.universe) n++;
  if (f.sector) n++;
  if (f.cap) n++;
  if (f.consensus) n++;
  if (f.q.trim()) n++;
  if (f.minScore > 0 || f.maxScore < 100) n++;
  if (f.analyzedOnly) n++;
  if (f.watchlistOnly) n++;
  return n;
}
