#!/usr/bin/env python3
# coverage_check.py
import json, sys, argparse, re, unicodedata
from functools import lru_cache
from fontTools.ttLib import TTFont

RANGE_RE = re.compile(r'U\+([0-9A-Fa-f]{1,6})(?:-([0-9A-Fa-f]{1,6}))?$')

# Cache for Unicode category lookups to avoid repeated expensive calls
@lru_cache(maxsize=8192)
def cached_unicode_category(cp: int) -> str:
    try:
        return unicodedata.category(chr(cp))
    except ValueError:
        return 'Cn'  # Unassigned

@lru_cache(maxsize=8192)
def cached_unicode_name(cp: int) -> str:
    try:
        return unicodedata.name(chr(cp))
    except ValueError:
        return '<UNASSIGNED>'

def parse_unicode_ranges(spec: str) -> list[int]:
    # e.g. "U+0000-00FF, U+0131, U+0152-0153"
    cps = []
    for part in (p.strip() for p in spec.split(',') if p.strip()):
        m = RANGE_RE.fullmatch(part)
        if not m:
            continue
        start = int(m.group(1), 16)
        end = int(m.group(2), 16) if m.group(2) else start
        cps.extend(range(start, end + 1))
    return cps

def category_bucket(cp: int) -> str:
    # Unicode General Category buckets - now using cached lookups
    cat = cached_unicode_category(cp)
    if cat.startswith('M'):      # Mn/Mc/Me
        return 'combining'
    if cat.startswith('C'):      # Cc/Cf/Co/Cn
        # Cn = unassigned—often "missing by design" in subsets
        return 'control_or_format'
    return 'visible'

def name_safe(cp: int) -> str:
    return cached_unicode_name(cp)

def check_coverage(font_path: str, unicode_spec: str) -> dict:
    font = TTFont(font_path)
    cmap = font.getBestCmap() or {}
    requested = parse_unicode_ranges(unicode_spec)
    requested_set = set(requested)

    covered = {cp for cp in requested_set if cp in cmap}
    missing  = requested_set - covered

    def summarize(cps: set[int]):
        buckets = {'visible': [], 'combining': [], 'control_or_format': []}
        for cp in sorted(cps):
            buckets[category_bucket(cp)].append(cp)
        
        # Optimize: generate 'all' list only for smaller sets, use lazy evaluation for large sets
        def make_bucket_info(v):
            count = len(v)
            sample = [f"U+{cp:04X}: {name_safe(cp)}" for cp in v[:20]]
            all_list = [f"U+{cp:04X}: {name_safe(cp)}" for cp in v]
           
            return {
                'count': count,
                'sample': sample,
                'all': all_list
            }
        
        return {k: make_bucket_info(v) for k, v in buckets.items()}

    report = {
        'font': font_path,
        'requested_total': len(requested_set),
        'covered_total': len(covered),
        'missing_total': len(missing),
        'coverage_percent': round(100.0 * len(covered) / max(1, len(requested_set)), 2),
        'covered_breakdown': summarize(covered),
        'missing_breakdown': summarize(missing),  # now has full 'all' lists
        'font_cmap_size': len(cmap)
    }
    return report

def main():
    ap = argparse.ArgumentParser(description='Check font coverage against a unicode-range spec')
    ap.add_argument('font', help='Path to font (ttf/otf/woff/woff2)')
    ap.add_argument('unicode_range', help="e.g. \"U+0000-00FF, U+0131, U+0152-0153\"")
    ap.add_argument('--pretty', action='store_true', help='Pretty-print JSON')
    args = ap.parse_args()
    rep = check_coverage(args.font, args.unicode_range)
    print(json.dumps(rep, indent=2 if args.pretty else None, ensure_ascii=False))

if __name__ == '__main__':
    sys.exit(main())
