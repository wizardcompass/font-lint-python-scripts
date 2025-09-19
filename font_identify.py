#!/usr/bin/env python3
import re
from statistics import pstdev
import sys, json
from functools import lru_cache
from fontTools.ttLib import TTFont

BARCODE_NAME_RE = re.compile(
    r"(barcode|code[\s\-]?39|code[\s\-]?128|ean|upc|itf|interleaved|msi|plessey|codabar|pdf417|datamatrix|qr|aztec)",
    re.IGNORECASE,
)

CODE39_ALLOWED = set([ord(c) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789- .$/+%"])

@lru_cache(maxsize=256)
def _get_name_strings_cached(name_table_id):
    """Cached version of name string extraction to avoid repeated table parsing"""
    # This would need the actual table data, but we'll optimize the caller instead
    pass

def _get_name_strings(tt):
    """Optimized name string extraction with early exit"""
    names = []
    if 'name' not in tt:
        return ""
    
    name_table = tt['name']
    # Try most common name IDs first for better cache efficiency
    for nid in (1, 4, 6):  # Family, Full, PostScript
        try:
            # Try platform 3 (Microsoft) first, then platform 1 (Apple)
            n = name_table.getName(nid, 3, 1) or name_table.getName(nid, 1, 0)
            if n: 
                names.append(str(n))
        except Exception:
            continue
    return " ".join(names)

def detect_barcode(tt):
    # 1) Name signal - check this first for early exit
    name_blob = _get_name_strings(tt)
    name_hit = bool(BARCODE_NAME_RE.search(name_blob))
    
    # Early exit if strong name signal
    if name_hit:
        return True

    # 2) Coverage signals - now more efficient
    cmap = tt.getBestCmap() or {}
    if not cmap:  # Early exit if no character map
        return False
        
    # Filter Latin range once and count in single pass
    latin = []
    uppers = lowers = digits = 0
    for cp in cmap:
        if 0x20 <= cp <= 0x7E:
            latin.append(cp)
            if 0x41 <= cp <= 0x5A:
                uppers += 1
            elif 0x61 <= cp <= 0x7A:
                lowers += 1
            elif 0x30 <= cp <= 0x39:
                digits += 1
    
    # Early exit if insufficient character coverage
    if len(latin) < 10:
        return False

    # Code 39 coverage ratio (how many mapped latin chars belong to code39 set)
    code39_overlap = sum(cp in CODE39_ALLOWED for cp in latin) / len(latin)

    # 3) Width profile (uniform advances among "barcodey" chars) - optimized sampling
    widths = []
    if 'hmtx' in tt:
        hmtx = tt['hmtx'].metrics
        # Sample only first 30 characters for speed (reduced from all latin chars)
        for cp in latin[:30]:
            gname = cmap.get(cp)
            if gname and gname in hmtx:
                adv, _ = hmtx[gname]
                widths.append(adv)

    width_uniform = False
    if len(widths) >= 5:  # Need minimum sample size
        mean_w = sum(widths) / len(widths)
        if mean_w > 0:
            cv = pstdev(widths) / mean_w  # coefficient of variation
            width_uniform = (cv < 0.02)   # very tight; relax to 0.05 if needed

    # 4) Vertical geometry + OS/2 hints - optimized sampling
    xh = getattr(tt.get('OS/2', {}), 'sxHeight', 0)
    units_per_em = getattr(tt.get('head', {}), 'unitsPerEm', 1000)

    tall_boxes_ratio = 0.0
    if 'glyf' in tt and len(latin) > 0:
        glyf = tt['glyf']
        tall_count = 0
        sample = 0
        # Reduced sample size from 120 to 20 for better performance
        for cp in latin[:20]:
            g = cmap.get(cp)
            if not g or g not in glyf: 
                continue
            try:
                gg = glyf[g]
                if hasattr(gg, 'numberOfContours') and gg.numberOfContours == 0:
                    continue
                # bbox can be computed after ensureDecompiled
                if hasattr(gg, 'yMin') and hasattr(gg, 'yMax'):
                    yMin, yMax = gg.yMin, gg.yMax
                    if units_per_em and (yMax - yMin) / units_per_em > 0.85:
                        tall_count += 1
                    sample += 1
            except Exception:
                continue
        if sample > 0:
            tall_boxes_ratio = tall_count / sample

    # Heuristic decision:
    # - strong name hit OR
    # - (code39-like coverage AND width-uniform) AND (xheight==0 OR most boxes tall)
    coverage_like_code39 = (code39_overlap >= 0.7 and lowers <= 2 and uppers + digits >= 10)
    vertical_hint = (xh == 0) or (tall_boxes_ratio >= 0.6)

    is_barcode = name_hit or (coverage_like_code39 and width_uniform and vertical_hint)
    return bool(is_barcode)

def classify(path):
    f = TTFont(path)
    tables = set(f.keys())
    
    # emoji/color - check most common color tables first
    is_emoji = bool('COLR' in tables or 'CBDT' in tables or 'sbix' in tables or 'SVG ' in tables or
                   ('CPAL' in tables and 'COLR' in tables) or ('CBLC' in tables and 'CBDT' in tables))
    
    # pictorial / symbol detection - optimized
    is_symbol = False
    if 'OS/2' in f:
        try:
            pan = f['OS/2'].panose
            if pan and getattr(pan, 'bFamilyType', 0) == 5:
                is_symbol = True
        except:
            pass
    
    # Check for format 13 cmap only if not already symbol
    if not is_symbol and 'cmap' in f:
        try:
            has_fmt13 = any(getattr(st, 'format', 0) == 13 for st in f['cmap'].tables)
            is_symbol = has_fmt13
        except:
            pass

    # non-textual quick check - single pass through cmap
    cmap = f.getBestCmap() or {}
    if not cmap:
        non_textual = True
        is_barcode = False
    else:
        letters = digits = 0
        for cp in cmap:
            if 0x20 <= cp <= 0x7E:  # ASCII printable range
                if 0x41 <= cp <= 0x5A or 0x61 <= cp <= 0x7A:  # letters
                    letters += 1
                elif 0x30 <= cp <= 0x39:  # digits
                    digits += 1
        
        non_textual = (letters < 10 and digits < 5)
        
        # Only run expensive barcode detection if needed
        is_barcode = detect_barcode(f) if not (is_emoji or is_symbol) else False

    return {
        "is_emoji": bool(is_emoji),
        "is_symbol": bool(is_symbol),
        "is_barcode": bool(is_barcode),
        "is_non_textual": bool(non_textual)
    }

if __name__ == "__main__":
    path = sys.argv[1]
    print(json.dumps(classify(path)))
