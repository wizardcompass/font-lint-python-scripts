#!/usr/bin/env python3
# font_subsetter.py
import sys, json, argparse, os, subprocess
from typing import List, Tuple
from functools import lru_cache
from fontTools.ttLib import TTFont
from fontTools import subset

def _normalize_newlines(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")

@lru_cache(maxsize=512)
def parse_unicode_ranges(css: str) -> Tuple[List[int], List[str]]:
    """
    Parse 'U+0000-00FF, U+0131, U+0152-0153' into:
      - list of integer codepoints
      - normalized compact range strings like ['U+0000-00FF','U+0131','U+0152-0153']
    
    Cached to avoid repeated parsing of common Unicode ranges.
    """
    if not css:
        return [], []

    # Optimize string cleaning with fewer operations
    cleaned = css.strip().replace('u+', 'U+').replace('U+ ', 'U+')
    parts = [p.strip() for p in cleaned.split(',') if p.strip()]
    unicodes = []
    normalized_ranges = []

    for part in parts:
        p = part.upper()
        if p.startswith('U+'):
            p = p[2:]
        if '-' in p:
            try:
                start, end = p.split('-', 1)
                s_i, e_i = int(start, 16), int(end, 16)
                if e_i < s_i:
                    s_i, e_i = e_i, s_i
                # Use extend for better performance on large ranges
                unicodes.extend(range(s_i, e_i + 1))
                normalized_ranges.append(f"U+{s_i:04X}-{e_i:04X}")
            except ValueError:
                continue
        else:
            try:
                cp = int(p, 16)
                unicodes.append(cp)
                normalized_ranges.append(f"U+{cp:04X}")
            except ValueError:
                continue

    # More efficient deduplication using dict.fromkeys (preserves order in Python 3.7+)
    uniq_unicodes = list(dict.fromkeys(unicodes))

    return uniq_unicodes, normalized_ranges

def subset_font(input_path: str, output_path: str, unicode_range: str,
                preserve_names: bool = True, allow_direct_woff2: bool = True, quiet=False):
    """
    Subset font and optionally convert to WOFF2.
    Returns a JSON-serializable dict with ops report.
    """
    # Pre-validate paths
    if not os.path.exists(input_path):
        return {"error": f"Input font not found: {input_path}"}

    # Ensure output dir
    out_dir = os.path.dirname(output_path) or "."
    os.makedirs(out_dir, exist_ok=True)

    # Parse unicode range
    unicodes, normalized_ranges = parse_unicode_ranges(unicode_range)
    if not unicodes:
        return {"error": "No valid unicode codepoints found"}

    font = None
    temp_ttf = None
    try:
        font = TTFont(input_path)
        # More efficient glyph counting
        glyph_order = font.getGlyphOrder()
        glyphs_before = len(glyph_order)
        tables_before = set(font.keys())

        # Subsetter options (FE-friendly by default)
        options = subset.Options()
        options.layout_features = ['*']            # keep all OT features
        options.layout_scripts = ['*']             # keep all scripts
        options.layout_languages = ['*']           # keep all languages
        options.name_IDs = ['*'] if preserve_names else []
        options.glyph_names = True                 # keep glyph names
        options.notdef_glyph = True                # keep .notdef glyph
        options.notdef_outline = True
        options.recommended_glyphs = True          # keep space, CR, etc
        options.symbol_cmap = True
        options.legacy_cmap = True
        options.passthrough_tables = True          # don’t drop unknown/extra tables
        options.hinting = True                     # do not strip hinting
        # options.desubroutinize = False           # leave CFF subroutines as-is

        # Populate and subset
        subsetter = subset.Subsetter(options=options)
        subsetter.populate(unicodes=unicodes)
        subsetter.subset(font)

        # More efficient post-subset analysis
        glyphs_after = len(font.getGlyphOrder())
        tables_after = set(font.keys())
        kept_tables = sorted(tables_after)
        dropped_tables = sorted(tables_before - tables_after)

        # Try direct WOFF2 if requested and extension is .woff2
        lossless_ops = []
        if output_path.lower().endswith('.woff2'):
            if allow_direct_woff2:
                try:
                    # Requires 'brotli' python module available to fontTools
                    font.flavor = "woff2"
                    font.save(output_path)
                    lossless_ops.append("repack:woff2")
                    out_format = "woff2"
                    file_size = os.path.getsize(output_path)
                except Exception as e:
                    # Fallback to external compressor if available
                    temp_ttf = os.path.join(out_dir, os.path.basename(output_path).replace('.woff2', '_temp.ttf'))
                    font.flavor = None
                    font.save(temp_ttf)
                    try:
                        subprocess.run(['woff2_compress', temp_ttf], check=True, capture_output=True)
                        generated_woff2 = temp_ttf.replace('.ttf', '.woff2')
                        if os.path.exists(generated_woff2):
                            os.replace(generated_woff2, output_path)
                            out_format = "woff2"
                            file_size = os.path.getsize(output_path)
                            lossless_ops.append("repack:woff2(cli)")
                        else:
                            return {"error": "WOFF2 file was not generated by woff2_compress"}
                    except FileNotFoundError:
                        return {"error": "Direct WOFF2 failed and woff2_compress not found in PATH"}
            else:
                # Explicitly use external tool path
                temp_ttf = os.path.join(out_dir, os.path.basename(output_path).replace('.woff2', '_temp.ttf'))
                font.flavor = None
                font.save(temp_ttf)
                try:
                    subprocess.run(['woff2_compress', temp_ttf], check=True, capture_output=True)
                    generated_woff2 = temp_ttf.replace('.ttf', '.woff2')
                    if os.path.exists(generated_woff2):
                        os.replace(generated_woff2, output_path)
                        out_format = "woff2"
                        file_size = os.path.getsize(output_path)
                        lossless_ops.append("repack:woff2(cli)")
                    else:
                        return {"error": "WOFF2 file was not generated by woff2_compress"}
                except FileNotFoundError:
                    return {"error": "woff2_compress not found in PATH"}
        else:
            # Save as TTF/OTF (same flavor as input)
            font.flavor = None
            font.save(output_path)
            out_format = "ttf" if output_path.lower().endswith('.ttf') else "otf"
            file_size = os.path.getsize(output_path)

        # Compute a conservative FE flag for the *artifact*
        # (no metadata/shape stripping + format ∈ allowed)
        fe_safe = (
            out_format in {"ttf", "otf", "woff2", "woff"} and
            preserve_names and
            # If we ever add options that strip shaping/metadata, flip these:
            True  # placeholder to keep the condition readable
        )

        kept = sorted(font.keys())  # or use your existing kept_tables list

        removed_metadata = not ("name" in kept)
        removed_shaping  = not (("GSUB" in kept) or ("GPOS" in kept) or ("GDEF" in kept))


        result = {
            "success": True,
            "output_path": output_path,
            "format": out_format,
            "unicodes_requested": len(unicodes),
            "unicodes_kept": len({cp for cp in unicodes}),  # after dedupe
            "normalized_ranges": normalized_ranges,
            "glyphs_before": glyphs_before,
            "glyphs_after": glyphs_after,
            "kept_tables": kept_tables,
            "removed_metadata": removed_metadata,
             "removed_shaping": removed_shaping,
            "dropped_tables": dropped_tables,
            "lossless_ops": lossless_ops,
            "fe_safe": fe_safe,
            "file_size": file_size
        }
        return result

    except Exception as e:
        return {"error": f"Subsetting failed: {str(e)}"}
    finally:
        try:
            if font is not None:
                font.close()
        except Exception:
            pass
        if temp_ttf and os.path.exists(temp_ttf):
            try:
                os.remove(temp_ttf)
            except Exception:
                pass

def main():
    parser = argparse.ArgumentParser(description="Subset fonts and optionally convert to WOFF2")
    parser.add_argument("input_font", help="Path to input font file")
    parser.add_argument("output_font", help="Path to output font file (.ttf/.otf or .woff2)")
    parser.add_argument("unicode_range", help="Unicode range (e.g., 'U+0000-00FF,U+0131')")
    parser.add_argument(
        "--preserve-names", dest="preserve_names",
        action="store_true", default=True,
        help="Preserve font name records (default: True)"
    )
    parser.add_argument(
        "--no-preserve-names", dest="preserve_names",
        action="store_false",
        help="Drop/strip name records that would collide with RFNs"
    )
    parser.add_argument("--no-direct-woff2", action="store_true",
                        help="Disable direct WOFF2 save and use woff2_compress if needed")
    parser.add_argument("--quiet", action="store_true", help="Minimal JSON output")

    args = parser.parse_args()

    result = subset_font(
        args.input_font,
        args.output_font,
        args.unicode_range,
        preserve_names=args.preserve_names,
        allow_direct_woff2=not args.no_direct_woff2,
        quiet=args.quiet
    )

    print(json.dumps(result, indent=None if args.quiet else 2))
    sys.exit(0 if result.get("success") else 1)

if __name__ == "__main__":
    main()
