"""Prompt reference resolver — turns @tags + global defaults into the list of
reference photo paths that should be attached to an image generation.

Mapping model (chosen with the user): "tags + global default".
  - A prompt line may carry @name tags picking named references, e.g.
        @fox @cafe | the fox drinking coffee
    or inline:
        the fox drinking coffee @fox @cafe
  - Tags are resolved against the reference_library (see db_manager). Each
    reference has a category ('character' | 'location') and a local photo.
  - For any category NOT explicitly tagged on a line, the global default for
    that category (app_settings 'default_character' / 'default_location') is
    applied — so a plain line still gets the batch's default character/location.

The resolved photo paths flow into the image job's `ref_paths`, which
extension_mode uploads (via the maseQ RPC) and injects into the ogiZ0b
reference slot for character/location consistency.
"""

import os
import re

from src.db import db_manager

# A tag is @ followed by letters/digits/underscore/hyphen. Underscores double
# as spaces so a reference named "cozy cafe" can be tagged @cozy_cafe. This is
# the fallback used to report unmatched tags; real matching is library-based
# (see _identifier_index / _match_reference_at) so filenames with spaces, dots
# and parentheses — e.g. @Untitled design (15).png — also resolve.
_TAG_RE = re.compile(r"@([A-Za-z0-9_\-]+)")
_WORD_CH = re.compile(r"[A-Za-z0-9]")

# Category ordering for the final reference list — character first, then
# location. Verified working live; keeps output deterministic.
_CATEGORY_ORDER = {"character": 0, "location": 1}


def extract_tags(raw_line):
    """Return the list of @tag names (without the @) found in a line."""
    return [m.group(1) for m in _TAG_RE.finditer(str(raw_line or ""))]


def strip_tags(raw_line):
    """Remove @tags (and an optional leading 'tags | prompt' separator) from a
    line, returning just the clean prompt text."""
    clean = _TAG_RE.sub("", str(raw_line or ""))
    # "tags | prompt" form — once tags are stripped, drop everything up to and
    # including the first pipe so only the prompt text remains.
    if "|" in clean:
        clean = clean.split("|", 1)[1]
    return " ".join(clean.split()).strip()


def _lookup(name):
    """Resolve a tag/name to a reference dict, trying underscore->space."""
    ref = db_manager.get_reference_by_name(name)
    if not ref and "_" in name:
        ref = db_manager.get_reference_by_name(name.replace("_", " "))
    return ref


def _identifier_index():
    """Every way to refer to a reference -> reference dict, longest identifier
    first. A reference can be named by its tag name (e.g. 'kai') OR by its
    photo filename with/without extension (e.g. 'Untitled design (15).png' /
    'Untitled design (15)'), so both @kai and @Untitled design (15).png work.
    Underscores in a name also match spaces (kept for the @cozy_cafe style)."""
    pairs = []
    for ref in db_manager.get_references():
        keys = set()
        name = str(ref.get("name") or "").strip().lower()
        if name:
            keys.add(name)
            if " " in name:
                keys.add(name.replace(" ", "_"))
        pp = str(ref.get("photo_path") or "").strip()
        if pp:
            base = os.path.basename(pp).lower()
            if base:
                keys.add(base)
                stem = os.path.splitext(base)[0]
                if stem:
                    keys.add(stem)
        for k in keys:
            if k:
                pairs.append((k, ref))
    # Longest identifier first so "untitled design (15).png" wins over a bare
    # "untitled", and so a token boundary check can reject partial hits.
    pairs.sort(key=lambda kv: len(kv[0]), reverse=True)
    return pairs


def _match_reference_at(low_line, at_pos, index):
    """At an '@' at `at_pos`, return (identifier, ref) for the longest known
    reference whose identifier starts right after the @ and ends on a token
    boundary (so @kai does not swallow 'kaiser')."""
    start = at_pos + 1
    for ident, ref in index:
        if low_line.startswith(ident, start):
            end = start + len(ident)
            nxt = low_line[end] if end < len(low_line) else ""
            if not nxt or not _WORD_CH.match(nxt):
                return ident, ref
    return None


def resolve_prompt_references(raw_line):
    """Parse a single prompt line.

    Supports both formats:
        @kai @temple | Kai exploring the ruined temple        (tag | prompt)
        The main character @Untitled design (15).png is ...   (inline filename)

    Returns a dict:
        clean_prompt   — prompt text with tags/separator removed
        ref_paths      — de-duplicated list of reference photo paths
                         (character first, then location)
        resolved_names — reference names that were applied
        missing_tags   — @tags that matched no known reference
    """
    line = str(raw_line or "")
    low = line.lower()
    index = _identifier_index()

    resolved = []          # list of reference dicts
    missing = []
    tagged_categories = set()

    # Walk the line, replacing each @reference with nothing while collecting
    # the resolved references. Library-based longest-match means filenames with
    # spaces/dots/parentheses resolve correctly; unmatched @word -> missing.
    # In parallel we build ordered `segments` (text | ref) that preserve WHERE
    # each reference sits in the sentence — used to build Flow's positional
    # inline reference markers at generation time.
    out_chars = []
    segments = []          # ordered list of {"type":"text"/"ref", ...}
    seg_buf = []           # current text run for segments
    has_pipe = "|" in line
    passed_pipe = False

    def _flush_seg_text():
        if seg_buf:
            segments.append({"type": "text", "text": "".join(seg_buf)})
            seg_buf.clear()

    i = 0
    n = len(line)
    while i < n:
        if line[i] == "@":
            hit = _match_reference_at(low, i, index)
            if hit:
                ident, ref = hit
                if ref.get("photo_path"):
                    resolved.append(ref)
                    tagged_categories.add(ref.get("category"))
                    _flush_seg_text()
                    segments.append({
                        "type": "ref",
                        "path": ref.get("photo_path"),
                        "name": os.path.basename(str(ref.get("photo_path") or "")),
                    })
                i += 1 + len(ident)
                continue
            m = _TAG_RE.match(line, i)
            if m:
                missing.append(m.group(1))
                i = m.end()
                continue
        ch = line[i]
        out_chars.append(ch)
        if ch == "|" and has_pipe and not passed_pipe:
            # classic "tags | prompt": the label text before the pipe is not a
            # real prompt — drop accumulated text, keep the refs seen so far.
            seg_buf.clear()
            segments[:] = [s for s in segments if s.get("type") == "ref"]
            passed_pipe = True
        else:
            seg_buf.append(ch)
        i += 1
    _flush_seg_text()

    clean = "".join(out_chars)
    # "tags | prompt" form — drop everything up to and including the first pipe.
    if "|" in clean:
        clean = clean.split("|", 1)[1]
    clean = " ".join(clean.split()).strip()

    # Tidy text segments: collapse whitespace runs (keep boundary spaces so the
    # sentence reads naturally around the inline reference), drop empties.
    tidy_segments = []
    for seg in segments:
        if seg.get("type") == "text":
            t = re.sub(r"\s+", " ", seg.get("text", ""))
            if t.strip() == "":
                # keep a single separating space only if it sits between refs
                t = " " if t else ""
            if t:
                tidy_segments.append({"type": "text", "text": t})
        else:
            tidy_segments.append(seg)
    segments = tidy_segments

    # Apply global defaults for any category the line didn't tag.
    for setting_key, category in (
        ("default_character", "character"),
        ("default_location", "location"),
    ):
        if category in tagged_categories:
            continue
        default_name = str(db_manager.get_setting(setting_key, "") or "").strip()
        if not default_name:
            continue
        ref = _lookup(default_name)
        if ref and ref.get("photo_path"):
            resolved.append(ref)

    # Deterministic order (character before location), then de-dup by path.
    resolved.sort(key=lambda r: _CATEGORY_ORDER.get(r.get("category"), 9))
    ref_paths = []
    seen = set()
    for ref in resolved:
        p = ref.get("photo_path")
        if p and p not in seen:
            seen.add(p)
            ref_paths.append(p)

    # Only expose segments when at least one inline reference exists — a plain
    # prompt (or defaults-only) needs no positional markers and uses the flat
    # path. Text-run before the first ref gets its leading space trimmed.
    has_inline_ref = any(s.get("type") == "ref" for s in segments)
    if segments and segments[0].get("type") == "text":
        segments[0]["text"] = segments[0]["text"].lstrip()
        if not segments[0]["text"]:
            segments = segments[1:]

    return {
        "clean_prompt": clean,
        "ref_paths": ref_paths,
        "resolved_names": [r["name"] for r in resolved],
        "missing_tags": missing,
        "segments": segments if has_inline_ref else None,
    }
