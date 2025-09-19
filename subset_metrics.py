#!/usr/bin/env python3
# subset_metrics.py
import sys, json, argparse, statistics, bisect
from functools import lru_cache
from fontTools.ttLib import TTFont

@lru_cache(maxsize=512)
def parse_unicode_ranges(css: str):
    """
    Parse 'U+0000-00FF, U+0131, U+0152-0153' into sorted non-overlapping intervals [(start,end),...]
    
    Cached to avoid repeated parsing of common Unicode ranges.
    """
    if not css:
        return tuple()  # Return tuple for hashability in cache
    
    parts = [p.strip() for p in css.replace('U+', '').split(',') if p.strip()]
    intervals = []
    for p in parts:
        if '-' in p:
            a, b = p.split('-', 1)
            try:
                intervals.append((int(a, 16), int(b, 16)))
            except ValueError:
                continue
        else:
            try:
                v = int(p, 16)
                intervals.append((v, v))
            except ValueError:
                continue
    
    if not intervals:
        return tuple()

    # merge overlaps - optimized
    intervals.sort()
    merged = []
    cur_s, cur_e = intervals[0]
    for s, e in intervals[1:]:
        if s <= cur_e + 1:
            cur_e = max(cur_e, e)
        else:
            merged.append((cur_s, cur_e))
            cur_s, cur_e = s, e
    merged.append((cur_s, cur_e))
    return tuple(merged)  # Return tuple for hashability

def codepoint_in_intervals(cp: int, ivals):
    # binary search over starts
    idx = bisect.bisect_right(ivals, (cp, 1 << 31)) - 1
    if idx < 0: return False
    s,e = ivals[idx]
    return s <= cp <= e

def subset_xavg(font_path: str, css_range: str, quiet=False):
    try:
        font = TTFont(font_path)
        upem = font['head'].unitsPerEm
        hmtx = font['hmtx'].metrics
        name_table = font['name']

        # Extract font names
        family_name = "Unknown"
        postscript_name = "Unknown"

        for record in name_table.names:
            try:
                if record.nameID == 1:  # Family name
                    family_name = record.toUnicode()
                elif record.nameID == 6:  # PostScript name
                    postscript_name = record.toUnicode()
            except:
                continue

        cmap = font.getBestCmap() or {}
        intervals = parse_unicode_ranges(css_range)

        if not intervals:
            return {"error":"empty_or_invalid_unicode_range"}

        # Optimized filtering: pre-filter cmap entries instead of checking each one
        widths = []
        attempted = 0
        
        # Create set of all codepoints in intervals for faster lookup
        target_cps = set()
        for start, end in intervals:
            target_cps.update(range(start, end + 1))
        
        # Filter cmap more efficiently
        for cp, gname in cmap.items():
            if cp in target_cps:
                attempted += 1
                m = hmtx.get(gname)
                if m:
                    adv, _ = m
                    widths.append(adv)

        result = {
            "postscriptName": postscript_name,
            "familyName": family_name,
            "unitsPerEm": upem,
            "processed": len(widths),
            "attempted": attempted,
            "coverage_ratio": (len(widths) / attempted) if attempted else 0.0,
        }

        if widths:
            mean_w = statistics.fmean(widths)
            med_w  = statistics.median(widths)
            std_w  = statistics.pstdev(widths) if len(widths) > 1 else 0.0
            result.update({
                "xAvgCharWidth": mean_w,
                "xMedianCharWidth": med_w,
                "xStdCharWidth": std_w,
                "method": "cmap+hmtx_mean",
            })
        else:
            result.update({
                "xAvgCharWidth": 0,
                "xMedianCharWidth": 0,
                "xStdCharWidth": 0,
                "method": "no_glyphs_in_subset",
            })
        return result

    except Exception as e:
        return {"error": str(e)}

def main():
    ap = argparse.ArgumentParser(description="Subset xAvgCharWidth calculator")
    ap.add_argument("font_path")
    ap.add_argument("unicode_range")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    out = subset_xavg(args.font_path, args.unicode_range, args.quiet)
    print(json.dumps(out, indent=None if args.quiet else 2))
    sys.exit(0 if "error" not in out else 1)

if __name__ == "__main__":
    main()
