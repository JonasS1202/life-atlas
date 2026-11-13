"""Atlas Knowledge-Graph & Dual-Tokenizer SQLite FTS5 Engine (Python 3 stdlib only).

Maintains the derived cache `<BRAIN>/_atlas/cache/index.sqlite` (incremental sync by `(mtime, size)` with optional
time budget and automatic self-healing if the database is missing, corrupt, or from an older schema version).
"""
import datetime as dt
import hashlib
import json
import math
import os
import re
import sqlite3
import time

SCHEMA_VERSION = "1"
MAX_PARSE_BYTES = 262_144  # 256 KB per Markdown note is generous while bounding worst-case I/O
SKIP_DIRS = {".git", "_secrets", "_archive", "_atlas", "node_modules", "__pycache__", ".obsidian", ".trash"}
TEXT_EXTS = {".md", ".txt"}

EDGE_WEIGHTS = {
    "related": 1.5,
    "body": 1.0,
    "journal": 0.5,
    "tree": 0.0,
}

LINK_RX = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)\s]+)\)")
WIKILINK_RX = re.compile(r"(?<!!)\[\[([^\[\]\r\n]+?)\]\]")
TICK_RX = re.compile(
    r"`((?:brain/)?(?:areas|projects|people|howto|journal|inbox|self|skills|calendar|coach|_atlas)/[^`\s]+\.md)`"
)
BRAIN_PATH_RX = re.compile(
    r"(`?)((?:brain/)?(?:areas|projects|people|howto|journal|inbox|self|skills|calendar|coach)/"
    r"[^\s`()\[\]<>\"',;:]+?\.md)\1"
)
HEADING_RX = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*#*\s*$")
LOCAL_EXT_RX = re.compile(
    r"(?i)\.(?:pdf|png|jpe?g|webp|gif|svg|txt|md|csv|tsv|json|ya?ml|xml|ics|eml|msg|docx?|xlsx?|pptx?|zip|html?)$"
)

# Generic nouns and structural labels never auto-linked as single standalone words in `atlas.py link`
LINK_STOPWORDS = {
    "readme", "overview", "index", "router", "bank", "konto", "girokonto", "home", "work", "arbeit",
    "sport", "coach", "health", "gesundheit", "finanzen", "finance", "steuern", "steuer", "versicherung",
    "vertrag", "contract", "contracts", "person", "people", "project", "projekt", "projects", "projekte",
    "area", "areas", "bereich", "notizen", "notes", "profil", "profile", "kalender", "calendar", "inbox",
    "journal", "settings", "test", "tests", "atlas", "haus", "wohnung", "auto", "strom", "internet",
    "handy", "telefon", "mail", "email", "schule", "studium", "reise", "urlaub", "training", "laufen",
    "rennrad", "allgemein", "general", "sonstiges", "misc", "todo", "todos", "aufgaben", "ideas", "ideen",
    "files", "archive", "archiv", "template", "vorlage", "kontoauszug", "depot", "sparplan",
}

PROMPT_STOPWORDS = {
    # German function words & common prompt verbs
    "aber", "alle", "allem", "allen", "aller", "alles", "also", "andere", "anderen", "anderer", "anderes",
    "auch", "auf", "aus", "bei", "beim", "bin", "bis", "bitte", "bzw", "da", "dabei", "dadurch", "dafuer",
    "dafür", "daher", "damit", "ach", "dann", "daran", "darauf", "daraus", "darin", "darum", "darunter",
    "das", "dass", "davon", "dazu", "dein", "deine", "deinem", "deinen", "deiner", "dem", "den", "denn",
    "der", "des", "dessen", "dich", "die", "dies", "diese", "diesem", "diesen", "dieser", "dieses", "dir",
    "doch", "dort", "du", "durch", "ein", "eine", "einem", "einen", "einer", "eines", "einige", "er",
    "erklaer", "erkläre", "erklaere", "erklär", "erst", "es", "etwa", "etwas", "euch", "euer", "eure",
    "falls", "fast", "finde", "folgende", "fuer", "für", "ganz", "gegen", "gehen", "geht", "gern", "gerne",
    "gib", "gibt", "gut", "hab", "habe", "haben", "hat", "hatte", "hier", "hin", "hinter", "ich", "ihm",
    "ihn", "ihnen", "ihr", "ihre", "ihrem", "ihren", "ihrer", "immer", "in", "indem", "ins", "ist", "ja",
    "jede", "jedem", "jeden", "jeder", "jedes", "jetzt", "kann", "kannst", "kein", "keine", "keinem",
    "keinen", "keiner", "koennen", "können", "könnte", "koennte", "lass", "mach", "mache", "machen", "mal",
    "man", "mehr", "mein", "meine", "meinem", "meinen", "meiner", "meines", "mich", "mir", "mit", "moechte",
    "möchte", "muss", "müssen", "muessen", "nach", "nein", "nicht", "nichts", "noch", "nun", "nur", "ob",
    "oder", "ohne", "sag", "sage", "schau", "schon", "sehr", "sei", "sein", "seine", "seinem", "seinen",
    "seiner", "seit", "selbst", "sich", "sie", "sind", "so", "soll", "sollte", "sondern", "sonst", "sowie",
    "steh", "steht", "ueber", "über", "um", "und", "uns", "unser", "unsere", "unter", "viel", "viele",
    "vom", "von", "vor", "wann", "war", "waren", "warum", "was", "weg", "weil", "weiter", "welche",
    "welchem", "welchen", "welcher", "welches", "wenn", "wer", "werde", "werden", "wie", "wieder", "will",
    "wir", "wird", "wo", "woher", "wohin", "wohl", "wollen", "wurde", "wurden", "zeig", "zeige", "zu",
    "zum", "zur", "zwar", "zwischen",
    # English function words & common prompt verbs
    "about", "after", "again", "all", "also", "always", "and", "any", "are", "ask", "been", "before",
    "being", "between", "both", "but", "can", "check", "could", "did", "does", "doing", "done", "each",
    "else", "even", "every", "explain", "find", "first", "for", "from", "get", "give", "going", "good",
    "had", "has", "have", "having", "here", "her", "hers", "him", "his", "how", "into", "its", "just",
    "keep", "know", "let", "like", "look", "make", "many", "may", "more", "most", "much", "must", "need",
    "never", "new", "next", "not", "now", "off", "only", "other", "our", "out", "over", "own", "please",
    "put", "same", "say", "see", "should", "show", "some", "still", "such", "take", "tell", "than", "that",
    "the", "their", "them", "then", "there", "these", "they", "thing", "think", "this", "those", "through",
    "time", "too", "under", "until", "use", "using", "very", "want", "was", "way", "well", "were", "what",
    "when", "where", "which", "while", "who", "whom", "why", "will", "with", "within", "without", "would",
    "you", "your", "yours",
}


# ---------------------------------------------------------------- helpers & normalization

def fwd(p):
    return os.path.normpath(p).replace("\\", "/")


def norm_text(s):
    s = str(s or "").lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return s.replace("\\", "/")


def expand_umlaut_tokens(text):
    """Return space-joined ae/oe/ue/ss variants ONLY for words containing German umlauts/eszett."""
    extras = []
    seen = set()
    for w in re.findall(r"[\w-]+", text or ""):
        low = w.lower()
        if any(ch in low for ch in ("ä", "ö", "ü", "ß")):
            folded = norm_text(low)
            if folded != low and folded not in seen:
                seen.add(folded)
                extras.append(folded)
    return " ".join(extras)


def parse_frontmatter(text):
    """Small YAML subset compatible with atlas_hook.frontmatter()."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out, key = {}, None
    for line in text[3:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if m and not line.startswith((" ", "\t", "-")):
            key, val = m.group(1).lower(), re.sub(r"\s+#.*$", "", m.group(2)).strip()
            if val.startswith("[") and val.endswith("]"):
                out[key] = [v.strip().strip("\"'") for v in val[1:-1].split(",") if v.strip()]
            else:
                out[key] = val.strip("\"'") if val else []
            continue
        m = re.match(r"^\s*-\s+(.*)$", line)
        if m and key is not None:
            if not isinstance(out.get(key), list):
                out[key] = [out[key]] if out.get(key) else []
            out[key].append(m.group(1).strip().strip("\"'"))
    return out


def as_list(v):
    return [v] if isinstance(v, str) and v else (list(v) if isinstance(v, list) else [])


def as_str(v):
    return v.strip() if isinstance(v, str) else ""


def slugify_heading(title):
    """GitHub/Markdown-compatible section anchor slug."""
    s = str(title or "").strip()
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"[`*_~]", "", s)
    s = s.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"[\s]+", "-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s


def is_folder_hub(rel):
    """True for structural index/hub files (INDEX.md, ROUTER.md, folder READMEs except project/self pages)."""
    if rel in ("INDEX.md", "ROUTER.md", "README.md", "tags.md", "settings.md"):
        return True
    if rel.endswith("/README.md"):
        parts = rel.split("/")
        # `projects/<slug>/README.md` is a real project entity; `self/README.md` is the user's profile page
        if parts[0] == "projects" and len(parts) == 3:
            return False
        if rel == "self/README.md":
            return False
        return True
    return False


def is_daily_log_file(rel):
    return bool(re.match(r"^journal/\d{4}/\d{4}-\d{2}-\d{2}\.md$", rel))


# ---------------------------------------------------------------- offset-preserving masking

def _blank_keep_nl(s):
    return re.sub(r"[^\r\n]", " ", s)


def frontmatter_end_offset(text):
    """Return character offset right after closing `---` line of YAML frontmatter, or 0 if none."""
    if not (text.startswith("---\n") or text.startswith("---\r\n")):
        return 0
    end = text.find("\n---", 3)
    if end < 0:
        return 0
    after = end + 4
    if after < len(text) and text[after] == "\r":
        after += 1
    if after < len(text) and text[after] == "\n":
        after += 1
    elif after < len(text):
        # e.g. `\n---something` is not a closing delimiter
        return 0
    return after


def mask_fenced_and_fm(text):
    """Blank YAML frontmatter and fenced code blocks (``` or ~~~) while preserving exact string length & newlines."""
    if not text:
        return ""
    fm_end = frontmatter_end_offset(text)
    out = []
    if fm_end:
        out.append(_blank_keep_nl(text[:fm_end]))
    pos = fm_end
    in_fence = False
    fence_char = ""
    fence_len = 0
    for line in text[pos:].splitlines(keepends=True):
        if not in_fence:
            m = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
            if m:
                in_fence = True
                fence_char = m.group(1)[0]
                fence_len = len(m.group(1))
                out.append(_blank_keep_nl(line))
            else:
                out.append(line)
        else:
            out.append(_blank_keep_nl(line))
            m_close = re.match(rf"^[ \t]{{0,3}}{re.escape(fence_char)}{{{fence_len},}}[ \t]*\r?\n?$", line)
            if m_close:
                in_fence = False
    return "".join(out)


INLINE_CODE_RX = re.compile(r"(`+)([^`\n]|(?!\1)`)+?\1")


def mask_all_code(text):
    """Blank YAML frontmatter, fenced code blocks, and inline code spans (`...`), preserving exact string offsets."""
    masked = mask_fenced_and_fm(text)
    return INLINE_CODE_RX.sub(lambda m: _blank_keep_nl(m.group(0)), masked)


def mask_prose_for_mentions(text, mask_inline_code=True):
    """Blank frontmatter, code, headings, existing links/wikilinks, URLs, HTML tags, and table separator lines."""
    masked = mask_all_code(text) if mask_inline_code else mask_fenced_and_fm(text)
    patterns = (
        r"(?m)^#{1,6}\s+.*$",
        r"!?\[[^\]]*\]\([^)]*\)",
        r"!?\[[^\]]*\]\[[^\]]*\]",
        r"!?\[\[[^\[\]\r\n]+\]\]",
        r"(?:(?:https?|file)://|mailto:)[^\s<>)\]]+",
        r"<[^>]+>",
        r"(?m)^\s*\|?[\s:-]+\|[\s|:-]*$",
    )
    for pat in patterns:
        masked = re.sub(pat, lambda m: _blank_keep_nl(m.group(0)), masked)
    return masked


# ---------------------------------------------------------------- parsing & link resolution

def extract_headings(text, masked_code=None):
    """Return `[(anchor, title, line_no)]` outside frontmatter and code blocks."""
    mc = masked_code if masked_code is not None else mask_all_code(text)
    out = []
    seen = set()
    for m in HEADING_RX.finditer(mc):
        # Read actual title from original text at identical span so inline code inside headings isn't lost
        raw_line = text[m.start():m.end()]
        hm = HEADING_RX.match(raw_line)
        title = hm.group(2).strip() if hm else m.group(2).strip()
        base_anchor = slugify_heading(title)
        if not base_anchor:
            continue
        anchor = base_anchor
        idx = 1
        while anchor in seen:
            anchor = f"{base_anchor}-{idx}"
            idx += 1
        seen.add(anchor)
        line_no = mc[:m.start()].count("\n") + 1
        out.append((anchor, title, line_no))
    return out


def clean_entity_title(title, rel=""):
    t = re.sub(r"[`*_]", "", str(title or "")).strip()
    t = re.sub(r"(?i)^(?:project|projekt|person|area|bereich)\s*:\s*", "", t).strip()
    if rel.startswith("journal/") and "·" in t:
        return ""
    return t


def _line_context(text, pos, max_len=140):
    line_start = text.rfind("\n", 0, pos) + 1
    line_end = text.find("\n", pos)
    if line_end < 0:
        line_end = len(text)
    s = text[line_start:line_end].strip()
    s = re.sub(r"\s+", " ", s)
    return s[:max_len]


def _parse_page_file(full_path, rel):
    try:
        with open(full_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read() if rel == "glossary.md" else fh.read(MAX_PARSE_BYTES)
    except OSError:
        return None
    fm = parse_frontmatter(text)
    masked_code = mask_all_code(text)
    masked_fenced = mask_fenced_and_fm(text)

    headings = extract_headings(text, masked_code)
    m_h1 = re.search(r"(?m)^#\s+(.+?)\s*#*\s*$", masked_code)
    if m_h1:
        raw_h1 = text[m_h1.start():m_h1.end()]
        hm1 = re.match(r"^#\s+(.+?)\s*#*\s*$", raw_h1)
        title = re.sub(r"[`*_]", "", hm1.group(1) if hm1 else m_h1.group(1)).strip()
    else:
        title = ""

    summary = as_str(fm.get("summary"))[:160]
    tags = [t.lower().strip() for t in as_list(fm.get("tags")) if str(t).strip()]
    aliases = [a.strip() for a in as_list(fm.get("aliases")) if len(str(a).strip()) >= 2]
    due = [str(d).strip() for d in as_list(fm.get("due")) if str(d).strip()]
    paths = [
        str(p).strip()
        for p in (as_list(fm.get("paths")) or as_list(fm.get("path")))
        if str(p).strip() and "<" not in str(p)
    ]
    status = as_str(fm.get("status"))
    verified = as_str(fm.get("verified"))
    valid_until = as_str(fm.get("valid_until"))
    sources = [str(s).strip() for s in as_list(fm.get("sources")) if str(s).strip()]
    related = [str(r).strip() for r in as_list(fm.get("related")) if str(r).strip()]

    glossary = []
    if rel == "glossary.md":
        for line in text.splitlines():
            cell = line.split("|")[1] if line.startswith("|") and line.count("|") >= 3 else line
            glossary += [t.strip() for t in re.findall(r"\*\*([^*]{3,60})\*\*", cell)]

    # Raw link candidates for edge resolution once all page metadata is available
    raw_links = []
    for m in LINK_RX.finditer(masked_code):
        orig = LINK_RX.match(text[m.start():m.end()])
        href = (orig.group(2) if orig else m.group(2)).strip()
        if not href or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", href) or href.startswith(("~", "/")) or "<" in href:
            continue
        raw_links.append(("md", href, _line_context(text, m.start())))

    for m in WIKILINK_RX.finditer(masked_code):
        inner = text[m.start() + 2:m.end() - 2].strip()
        if inner and "<" not in inner:
            raw_links.append(("wiki", inner, _line_context(text, m.start())))

    for m in TICK_RX.finditer(masked_fenced):
        t_rel = m.group(1)
        if t_rel.startswith("brain/"):
            t_rel = t_rel[6:]
        raw_links.append(("tick", t_rel, _line_context(text, m.start())))

    # Clean FTS body (frontmatter stripped, markdown links unwrapped to labels, umlaut shadow tokens appended)
    fm_end = frontmatter_end_offset(text)
    body_raw = text[fm_end:]
    body_clean = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", body_raw)
    umlaut_extra = expand_umlaut_tokens(f"{title} {' '.join(aliases)} {summary} {body_clean}")
    norm_body = body_clean + ("\n" + umlaut_extra if umlaut_extra else "")

    entry = {
        "title": title,
        "summary": summary,
        "tags": tags,
        "aliases": [a for a in aliases if len(a) >= 3],
        "_all_aliases": aliases,
        "due": due,
        "paths": paths,
        "status": status,
        "verified": verified,
        "valid_until": valid_until,
        "sources": sources,
        "related": related,
    }
    if rel == "glossary.md":
        entry["glossary"] = glossary

    return {
        "entry": entry,
        "headings": headings,
        "raw_links": raw_links,
        "norm_body": norm_body,
    }


def _build_lookup_maps(known_rels, entries):
    """Build fast lookup maps for resolving paths, stems, titles, and aliases to canonical `rel`."""
    rel_lower = {r.lower(): r for r in known_rels}
    by_stem = {}
    by_name = {}
    for r in known_rels:
        base = os.path.basename(r)
        stem = os.path.splitext(base)[0].lower()
        if stem != "readme":
            by_stem.setdefault(stem, []).append(r)
        elif r.startswith("projects/") and r.count("/") == 2:
            proj_slug = r.split("/")[1].lower()
            by_stem.setdefault(proj_slug, []).append(r)
    for r, e in entries.items():
        if r.startswith("_"):
            continue
        ct = clean_entity_title(e.get("title", ""), r)
        if ct:
            by_name.setdefault(ct.lower(), []).append(r)
        raw_t = (e.get("title") or "").strip()
        if raw_t and raw_t.lower() != ct.lower():
            by_name.setdefault(raw_t.lower(), []).append(r)
        for a in (e.get("_all_aliases") or e.get("aliases") or []):
            al = a.strip().lower()
            if al:
                by_name.setdefault(al, []).append(r)
    # Deduplicate lists while preserving deterministic sorted order
    for d in (by_stem, by_name):
        for k, v in list(d.items()):
            d[k] = sorted(set(v))
    return rel_lower, by_stem, by_name


def _resolve_target_internal(raw_ref, src_rel, known_rels, rel_lower, by_stem, by_name):
    """Resolve a page reference (`path`, `../path`, `[[Target#Sec|Label]]`) -> `(dst_rel_or_None, section_str, cands)`."""
    ref = str(raw_ref or "").strip()
    if "|" in ref:
        ref = ref.split("|", 1)[0].strip()
    if "#" in ref:
        page_part, sec_part = ref.split("#", 1)
    else:
        page_part, sec_part = ref, ""
    page_part = page_part.strip()
    sec_part = sec_part.strip()
    if page_part.startswith("brain/"):
        page_part = page_part[6:]

    if not page_part:
        return (src_rel if sec_part and src_rel in known_rels else None), sec_part, ([src_rel] if src_rel else [])

    # 1. Relative to src_rel directory
    if src_rel:
        cand_rel = fwd(os.path.normpath(os.path.join(os.path.dirname(src_rel), page_part)))
        for c in (cand_rel, cand_rel + ".md", fwd(os.path.join(cand_rel, "README.md"))):
            if c in known_rels:
                return c, sec_part, [c]
            if c.lower() in rel_lower:
                hit = rel_lower[c.lower()]
                return hit, sec_part, [hit]

    # 2. Direct brain-relative path
    norm_p = fwd(page_part)
    for c in (norm_p, norm_p + ".md", fwd(os.path.join(norm_p, "README.md"))):
        if c in known_rels:
            return c, sec_part, [c]
        if c.lower() in rel_lower:
            hit = rel_lower[c.lower()]
            return hit, sec_part, [hit]

    # 3. Stem match (`max-muster` -> `people/max-muster.md`)
    low = page_part.lower()
    if low.endswith(".md"):
        low = low[:-3]
    stems = by_stem.get(low, [])
    if len(stems) == 1:
        return stems[0], sec_part, stems
    if len(stems) > 1:
        return None, sec_part, stems

    # 4. Title / Alias match
    names = by_name.get(page_part.lower(), [])
    if len(names) == 1:
        return names[0], sec_part, names
    if len(names) > 1:
        return None, sec_part, names

    # Folded umlaut fallback on title/alias/stem
    folded = norm_text(page_part)
    folded_hits = set()
    for k, v in by_stem.items():
        if norm_text(k) == folded:
            folded_hits.update(v)
    for k, v in by_name.items():
        if norm_text(k) == folded:
            folded_hits.update(v)
    cands = sorted(folded_hits)
    if len(cands) == 1:
        return cands[0], sec_part, cands
    return None, sec_part, cands


def _classify_edge_kind(src_rel, dst_rel, source_type):
    if source_type == "related":
        return "related"
    if src_rel.startswith("journal/"):
        return "journal"
    if is_folder_hub(src_rel) or is_folder_hub(dst_rel):
        return "tree"
    return "body"


def _resolve_edges_for_page(src_rel, entry, raw_links, known_rels, rel_lower, by_stem, by_name):
    """Return list of `(src, dst, kind, anchor, context)` tuples for one page."""
    edges_map = {}

    for rel_ref in entry.get("related", []):
        dst, sec, _cands = _resolve_target_internal(rel_ref, src_rel, known_rels, rel_lower, by_stem, by_name)
        if dst and dst != src_rel:
            anchor = slugify_heading(sec) if sec else ""
            edges_map[(src_rel, dst, "related", anchor)] = f"related: {rel_ref}"[:160]

    for ltype, raw_target, ctx in raw_links:
        if ltype == "md":
            if "#" in raw_target:
                p_part, s_part = raw_target.split("#", 1)
            else:
                p_part, s_part = raw_target, ""
            if not p_part:
                continue
            cand = fwd(os.path.normpath(os.path.join(os.path.dirname(src_rel), p_part)))
            dst = None
            if cand in known_rels:
                dst = cand
            elif cand.lower() in rel_lower:
                dst = rel_lower[cand.lower()]
            elif fwd(os.path.join(cand, "README.md")) in known_rels:
                dst = fwd(os.path.join(cand, "README.md"))
            if dst and dst != src_rel:
                kind = _classify_edge_kind(src_rel, dst, "body")
                anchor = slugify_heading(s_part) if s_part else ""
                edges_map.setdefault((src_rel, dst, kind, anchor), ctx[:160])
        elif ltype == "wiki":
            dst, sec, _cands = _resolve_target_internal(raw_target, src_rel, known_rels, rel_lower, by_stem, by_name)
            if dst and dst != src_rel:
                kind = _classify_edge_kind(src_rel, dst, "body")
                anchor = slugify_heading(sec) if sec else ""
                edges_map.setdefault((src_rel, dst, kind, anchor), ctx[:160])
        elif ltype == "tick":
            dst = raw_target if raw_target in known_rels else rel_lower.get(raw_target.lower())
            if dst and dst != src_rel:
                kind = _classify_edge_kind(src_rel, dst, "body")
                edges_map.setdefault((src_rel, dst, kind, ""), ctx[:160])

    return [(s, d, k, a, c) for (s, d, k, a), c in edges_map.items()]


# ---------------------------------------------------------------- SQLite connection & sync

def default_db_path(brain):
    return os.path.join(brain, "_atlas", "cache", "index.sqlite")


def _remove_db_files(db_path):
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = db_path + suffix
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


def _init_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            val TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pages (
            rel         TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            summary     TEXT NOT NULL,
            tags        TEXT NOT NULL,
            aliases     TEXT NOT NULL,
            due         TEXT NOT NULL,
            paths       TEXT NOT NULL,
            status      TEXT NOT NULL,
            verified    TEXT NOT NULL,
            valid_until TEXT NOT NULL,
            sources     TEXT NOT NULL,
            related     TEXT NOT NULL,
            glossary    TEXT NOT NULL,
            mtime       REAL NOT NULL,
            size        INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS headings (
            rel         TEXT NOT NULL,
            anchor      TEXT NOT NULL,
            title       TEXT NOT NULL,
            line_no     INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (rel, anchor)
        );
        CREATE TABLE IF NOT EXISTS edges (
            src         TEXT NOT NULL,
            dst         TEXT NOT NULL,
            kind        TEXT NOT NULL,
            anchor      TEXT NOT NULL,
            context     TEXT NOT NULL,
            PRIMARY KEY (src, dst, kind, anchor)
        );
        CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
        CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
        CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
            rel UNINDEXED,
            title,
            aliases,
            tags,
            summary,
            norm_body,
            tokenize='unicode61 remove_diacritics 2',
            prefix='2 3'
        );
        """
    )
    has_trigram = False
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS pages_trigram USING fts5(
                rel UNINDEXED,
                title,
                aliases,
                norm_body,
                tokenize='trigram'
            );
            """
        )
        has_trigram = True
    except sqlite3.OperationalError:
        has_trigram = False
    conn.execute("INSERT OR REPLACE INTO meta(key, val) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
    conn.execute("INSERT OR REPLACE INTO meta(key, val) VALUES ('has_trigram', ?)", ("1" if has_trigram else "0",))


def open_graph_db(brain, db_path=None):
    """Open `index.sqlite`, automatically recreating it if corrupt or schema version changed."""
    path = db_path or default_db_path(brain)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for attempt in range(2):
        conn = None
        try:
            conn = sqlite3.connect(path, timeout=5.0)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            conn.execute("PRAGMA synchronous=NORMAL")
            row = conn.execute(
                "SELECT val FROM meta WHERE key='schema_version'"
            ).fetchone() if conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone() else None
            if row is None:
                with conn:
                    _init_schema(conn)
            elif row["val"] != SCHEMA_VERSION:
                conn.close()
                _remove_db_files(path)
                continue
            else:
                # Lightweight sanity check on pages & pages_fts
                conn.execute("SELECT count(*) FROM pages").fetchone()
                conn.execute("SELECT count(*) FROM pages_fts").fetchone()
            return conn
        except sqlite3.DatabaseError:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            if attempt == 0:
                _remove_db_files(path)
                continue
            raise
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _init_schema(conn)
    return conn


def _priority(rel):
    if rel.startswith("projects/") or rel in ("glossary.md", "tags.md", "settings.md"):
        return 0
    return 1 if rel.startswith(("people/", "self/", "calendar/", "coach/")) else 2


def _row_to_entry(row):
    entry = {
        "title": row["title"],
        "summary": row["summary"],
        "tags": json.loads(row["tags"] or "[]"),
        "aliases": json.loads(row["aliases"] or "[]"),
        "due": json.loads(row["due"] or "[]"),
        "paths": json.loads(row["paths"] or "[]"),
        "status": row["status"],
        "verified": row["verified"],
        "valid_until": row["valid_until"],
        "sources": json.loads(row["sources"] or "[]"),
        "related": json.loads(row["related"] or "[]"),
        "sig": [round(float(row["mtime"]), 6), int(row["size"])],
    }
    if row["rel"] == "glossary.md":
        entry["glossary"] = json.loads(row["glossary"] or "[]")
    return entry


def sync_index(brain, budget=None, db_path=None):
    """Incrementally sync `brain/` into `_atlas/cache/index.sqlite` and return `{rel: entry}` dict."""
    start = time.monotonic()
    path = db_path or default_db_path(brain)

    disk = {}
    if os.path.isdir(brain):
        for dp, dns, fns in os.walk(brain):
            dns[:] = sorted(d for d in dns if d not in SKIP_DIRS and not d.startswith("."))
            for fn in fns:
                if os.path.splitext(fn)[1].lower() not in TEXT_EXTS:
                    continue
                full = os.path.join(dp, fn)
                rel = os.path.relpath(full, brain).replace("\\", "/")
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                disk[rel] = (full, round(st.st_mtime, 6), st.st_size)

    for attempt in range(2):
        conn = None
        try:
            conn = open_graph_db(brain, path)
            trigram_row = conn.execute("SELECT val FROM meta WHERE key='has_trigram'").fetchone()
            has_trigram = bool(trigram_row and trigram_row["val"] == "1")

            cached_rows = conn.execute("SELECT * FROM pages").fetchall()
            cached = {r["rel"]: r for r in cached_rows}

            files = {}
            todo = []
            for rel, (full, mtime, size) in disk.items():
                row = cached.get(rel)
                if row is not None and abs(float(row["mtime"]) - mtime) < 1e-5 and int(row["size"]) == size:
                    files[rel] = _row_to_entry(row)
                else:
                    todo.append((_priority(rel), rel, full, mtime, size))

            removed = [r for r in cached if r not in disk]
            parsed_batch = []
            for _prio, rel, full, mtime, size in sorted(todo):
                if budget is not None and (time.monotonic() - start) > budget:
                    # Keep stale entry in returned dict until next call finishes budget
                    if rel in cached:
                        files[rel] = _row_to_entry(cached[rel])
                    continue
                parsed = _parse_page_file(full, rel)
                if parsed is not None:
                    entry = parsed["entry"]
                    entry["sig"] = [mtime, size]
                    files[rel] = entry
                    parsed_batch.append((rel, mtime, size, parsed))

            if removed or parsed_batch:
                known_md = {r for r in disk if r.lower().endswith(".md")}
                rel_lower, by_stem, by_name = _build_lookup_maps(known_md, files)
                with conn:
                    for r in removed:
                        conn.execute("DELETE FROM pages WHERE rel = ?", (r,))
                        conn.execute("DELETE FROM headings WHERE rel = ?", (r,))
                        conn.execute("DELETE FROM edges WHERE src = ? OR dst = ?", (r, r))
                        conn.execute("DELETE FROM pages_fts WHERE rel = ?", (r,))
                        if has_trigram:
                            conn.execute("DELETE FROM pages_trigram WHERE rel = ?", (r,))

                    for rel, mtime, size, parsed in parsed_batch:
                        e = parsed["entry"]
                        conn.execute("DELETE FROM headings WHERE rel = ?", (rel,))
                        conn.execute("DELETE FROM edges WHERE src = ?", (rel,))
                        conn.execute("DELETE FROM pages_fts WHERE rel = ?", (rel,))
                        if has_trigram:
                            conn.execute("DELETE FROM pages_trigram WHERE rel = ?", (rel,))

                        conn.execute(
                            """
                            INSERT OR REPLACE INTO pages(
                                rel, title, summary, tags, aliases, due, paths, status,
                                verified, valid_until, sources, related, glossary, mtime, size
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                rel,
                                e["title"],
                                e["summary"],
                                json.dumps(e["tags"], ensure_ascii=False),
                                json.dumps(e["aliases"], ensure_ascii=False),
                                json.dumps(e["due"], ensure_ascii=False),
                                json.dumps(e["paths"], ensure_ascii=False),
                                e["status"],
                                e["verified"],
                                e["valid_until"],
                                json.dumps(e["sources"], ensure_ascii=False),
                                json.dumps(e["related"], ensure_ascii=False),
                                json.dumps(e.get("glossary", []), ensure_ascii=False),
                                mtime,
                                size,
                            ),
                        )
                        if parsed["headings"]:
                            conn.executemany(
                                "INSERT OR IGNORE INTO headings(rel, anchor, title, line_no) VALUES (?, ?, ?, ?)",
                                [(rel, anc, htitle, lno) for anc, htitle, lno in parsed["headings"]],
                            )
                        edges = _resolve_edges_for_page(
                            rel, e, parsed["raw_links"], known_md, rel_lower, by_stem, by_name
                        )
                        if edges:
                            conn.executemany(
                                "INSERT OR REPLACE INTO edges(src, dst, kind, anchor, context) VALUES (?, ?, ?, ?, ?)",
                                edges,
                            )

                        # Do not index structural boilerplate in FTS search if excluded
                        if not rel.startswith("_") and rel not in ("tags.md", "settings.md", "ROUTER.md"):
                            aliases_txt = " ".join(e.get("_all_aliases") or e["aliases"])
                            tags_txt = " ".join(e["tags"])
                            conn.execute(
                                "INSERT INTO pages_fts(rel, title, aliases, tags, summary, norm_body) VALUES (?, ?, ?, ?, ?, ?)",
                                (rel, e["title"], aliases_txt, tags_txt, e["summary"], parsed["norm_body"]),
                            )
                            if has_trigram:
                                conn.execute(
                                    "INSERT INTO pages_trigram(rel, title, aliases, norm_body) VALUES (?, ?, ?, ?)",
                                    (rel, e["title"], aliases_txt, parsed["norm_body"]),
                                )
            # Strip internal helper key before returning
            for e in files.values():
                e.pop("_all_aliases", None)
            conn.close()
            return files
        except sqlite3.DatabaseError:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            if attempt == 0:
                _remove_db_files(path)
                continue
            break

    # Ultimate fail-open fallback (in-memory dict without crashing)
    fallback = {}
    for rel, (full, mtime, size) in disk.items():
        parsed = _parse_page_file(full, rel)
        if parsed is not None:
            e = parsed["entry"]
            e.pop("_all_aliases", None)
            e["sig"] = [mtime, size]
            fallback[rel] = e
    return fallback


# ---------------------------------------------------------------- FTS5 & Trigram Search + 1-Hop Specificity

def _fts_token_query(term, prefix=True):
    """Build a safe FTS5 MATCH expression for a single term (handling punctuation, short tokens, and prefixes)."""
    tokens = re.findall(r"[\w]+", str(term or ""), flags=re.UNICODE)
    if not tokens:
        return ""
    parts = []
    for tok in tokens:
        safe = tok.replace('"', "")
        if not safe:
            continue
        if prefix and len(safe) >= 2:
            parts.append(f'"{safe}"*')
        else:
            parts.append(f'"{safe}"')
    return " AND ".join(parts)


def query_term_matches(conn, term, has_trigram=True, exact_word=False):
    """Return `{rel: bm25_boost}` for a single term using `pages_fts` first and `pages_trigram` fallback."""
    out = {}
    raw = str(term or "").strip()
    if not raw:
        return out
    variants = [raw]
    folded = norm_text(raw)
    if folded and folded != raw.lower():
        variants.append(folded)

    for v in variants:
        q = _fts_token_query(v, prefix=not exact_word)
        if not q:
            continue
        try:
            rows = conn.execute(
                "SELECT rel, bm25(pages_fts, 10.0, 6.0, 8.0, 5.0, 1.0) AS rank FROM pages_fts WHERE pages_fts MATCH ? LIMIT 60",
                (q,),
            ).fetchall()
            for r in rows:
                # sqlite3 bm25() returns negative numbers (more negative = better match)
                score = max(0.25, min(15.0, -float(r["rank"])))
                if score > out.get(r["rel"], 0.0):
                    out[r["rel"]] = score
        except sqlite3.DatabaseError:
            pass

    # Trigram fallback for >= 3 char compound substrings (e.g. "rückgewinnung" in "Kundenrückgewinnung")
    clean_len = len(re.sub(r"\s+", "", raw))
    if len(out) < 3 and clean_len >= 3:
        if has_trigram:
            for v in variants:
                tri_q = '"' + v.replace('"', '""') + '"'
                try:
                    rows = conn.execute(
                        "SELECT rel, bm25(pages_trigram, 8.0, 5.0, 1.0) AS rank FROM pages_trigram WHERE pages_trigram MATCH ? LIMIT 40",
                        (tri_q,),
                    ).fetchall()
                    for r in rows:
                        score = max(0.2, min(8.0, -float(r["rank"]) * 0.6))
                        if score > out.get(r["rel"], 0.0):
                            out[r["rel"]] = score
                except sqlite3.DatabaseError:
                    pass
        else:
            like_pat = f"%{folded}%"
            try:
                rows = conn.execute(
                    "SELECT rel FROM pages_fts WHERE lower(norm_body) LIKE ? OR lower(title) LIKE ? LIMIT 40",
                    (like_pat, like_pat),
                ).fetchall()
                for r in rows:
                    out.setdefault(r["rel"], 1.0)
            except sqlite3.DatabaseError:
                pass
    return out


def expand_1hop_neighbors(brain, seed_rels, skip_pages=None, max_neighbors=3, include_daily_logs=False, db_path=None):
    """Specificity-weighted 1-hop graph expansion (`score = w_kind / ln(1 + deg_total(dst))`).

    Suppresses `INDEX.md`, `ROUTER.md`, folder `README.md` hubs (`tree` edges = 0.0), and already-hit pages.
    Returns `[(neighbor_rel, score, via_rel, kind)]`.
    """
    seeds = [r for r in (seed_rels or []) if r]
    if not seeds or max_neighbors <= 0:
        return []
    skip = set(skip_pages or ()) | set(seeds) | {"INDEX.md", "ROUTER.md", "tags.md", "settings.md"}

    conn = open_graph_db(brain, db_path)
    try:
        placeholders = ",".join("?" for _ in seeds)
        edge_rows = conn.execute(
            f"""
            SELECT src, dst, kind FROM edges
            WHERE kind != 'tree' AND (src IN ({placeholders}) OR dst IN ({placeholders}))
            """,
            tuple(seeds) + tuple(seeds),
        ).fetchall()
        if not edge_rows:
            return []

        cands = {}  # neighbor -> list of (seed, kind)
        seed_set = set(seeds)
        for r in edge_rows:
            s, d, k = r["src"], r["dst"], r["kind"]
            w = EDGE_WEIGHTS.get(k, 0.0)
            if w <= 0.0:
                continue
            if s in seed_set and d not in skip and not d.startswith("_") and not is_folder_hub(d):
                if include_daily_logs or not is_daily_log_file(d):
                    cands.setdefault(d, []).append((s, k))
            if d in seed_set and s not in skip and not s.startswith("_") and not is_folder_hub(s):
                if include_daily_logs or not is_daily_log_file(s):
                    cands.setdefault(s, []).append((d, k))

        if not cands:
            return []

        cand_list = list(cands.keys())
        c_ph = ",".join("?" for _ in cand_list)
        # Compute total non-tree degree (in + out) for each candidate node
        deg_map = {c: 0 for c in cand_list}
        for row in conn.execute(
            f"SELECT src AS node, count(*) AS cnt FROM edges WHERE kind != 'tree' AND src IN ({c_ph}) GROUP BY src",
            tuple(cand_list),
        ):
            deg_map[row["node"]] = deg_map.get(row["node"], 0) + int(row["cnt"])
        for row in conn.execute(
            f"SELECT dst AS node, count(*) AS cnt FROM edges WHERE kind != 'tree' AND dst IN ({c_ph}) GROUP BY dst",
            tuple(cand_list),
        ):
            deg_map[row["node"]] = deg_map.get(row["node"], 0) + int(row["cnt"])

        scored = []
        for cand, links in cands.items():
            deg = max(1, deg_map.get(cand, 1))
            denom = math.log(1.0 + deg)
            # Deduplicate (seed, cand) keeping highest-weight edge kind per seed
            by_seed = {}
            for seed, k in links:
                w = EDGE_WEIGHTS.get(k, 0.0)
                if seed not in by_seed or w > by_seed[seed][0]:
                    by_seed[seed] = (w, k)
            total_score = sum(w / denom for w, _k in by_seed.values())
            best_seed, (_bw, best_kind) = max(by_seed.items(), key=lambda kv: kv[1][0])
            scored.append((cand, round(total_score, 4), best_seed, best_kind))

        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[:max_neighbors]
    finally:
        conn.close()


def search_pages(brain, terms, idx=None, tag_vocab_map=None, skip_pages=None, limit=12, text_search=True, db_path=None):
    """Ranked search combining frontmatter/tag-synonym boosts with SQLite FTS5 BM25 + Trigram fallback."""
    if idx is None:
        idx = sync_index(brain, db_path=db_path)
    vocab = tag_vocab_map or {}
    skip = set(skip_pages or ()) | {"tags.md", "settings.md"}
    words = [str(w).strip() for w in (terms or []) if str(w).strip()]
    if not words:
        return [], []

    canon = {}
    for tag, syns in vocab.items():
        for w in [tag] + list(syns):
            canon[w.lower()] = tag.lower()

    scores, why = {}, {}
    for rel, e in idx.items():
        if rel.startswith("_") or rel in skip:
            continue
        hay_title = (e.get("title") or "").lower()
        hay_sum = (e.get("summary") or "").lower()
        aliases = [a.lower() for a in e.get("aliases", [])]
        page_tags = {canon.get(t.lower(), t.lower()) for t in e.get("tags", [])}
        for raw_w in words:
            w = raw_w.lower()
            wf = norm_text(w)
            pts = 0.0
            if canon.get(w, w) in page_tags or canon.get(wf, wf) in page_tags:
                pts += 8.0
            if any(w in a or wf in norm_text(a) for a in aliases):
                pts += 6.0
            if w in hay_title or wf in norm_text(hay_title):
                pts += 5.0
            if w in hay_sum or wf in norm_text(hay_sum):
                pts += 3.0
            if w in rel.lower() or wf in norm_text(rel.lower()):
                pts += 1.5
            if pts > 0:
                scores[rel] = scores.get(rel, 0.0) + pts
                why.setdefault(rel, set()).add(w)

    if text_search:
        conn = open_graph_db(brain, db_path)
        try:
            trigram_row = conn.execute("SELECT val FROM meta WHERE key='has_trigram'").fetchone()
            has_trigram = bool(trigram_row and trigram_row["val"] == "1")
            for raw_w in words:
                w = raw_w.lower()
                hits = query_term_matches(conn, raw_w, has_trigram=has_trigram)
                for rel, bm_score in hits.items():
                    if rel in idx and not rel.startswith("_") and rel not in skip:
                        scores[rel] = scores.get(rel, 0.0) + bm_score
                        why.setdefault(rel, set()).add(w)
        finally:
            conn.close()

    ranked = sorted(scores, key=lambda r: (-len(why[r]), -scores[r], r))[:limit]
    neighbors = expand_1hop_neighbors(brain, ranked[:3], skip_pages=skip | set(ranked), max_neighbors=3, db_path=db_path) if text_search and ranked else []
    return ranked, neighbors


def compute_prompt_hints(brain, prompt, idx, tag_vocab_map, vocab_terms, skip_pages, max_hints=5, max_df_share=0.6, db_path=None):
    """Fast SQLite-backed replacement for `hints()` + specificity-weighted 1-hop neighbor expansion.

    Returns `(direct_hints, linked_neighbors)` where:
      - `direct_hints`: `[(rel, summary, matched_terms)]`
      - `linked_neighbors`: `[(neighbor_rel, summary, via_rel, kind)]`
    """
    skip = set(skip_pages or ()) | {"ROUTER.md", "INDEX.md", "tags.md", "settings.md"}
    low = norm_text(prompt)
    comp = re.sub(r"[^a-z0-9]", "", low)
    cands = {}

    # 1. Known vocabulary entities (titles, aliases, glossary terms)
    for t in vocab_terms:
        tl = norm_text(t)
        if not tl:
            continue
        if re.search(r"(?<![\w-])" + re.escape(tl) + r"(?![\w-])", low):
            cands[t] = True
        elif len(re.sub(r"[^a-z0-9]", "", tl)) >= 6 and re.sub(r"[^a-z0-9]", "", tl) in comp:
            cands[t] = True

    # 2. Alphanumeric IDs (contract numbers, tickers like DBX0AN, C24, O2)
    for m in re.finditer(r"\b(?=[A-Za-z0-9./-]*\d)[A-Za-z0-9][A-Za-z0-9./-]{1,}[A-Za-z0-9]\b", prompt):
        tok = m.group(0)
        if not re.match(r"^\d{1,2}$", tok):
            cands.setdefault(tok, False)

    # 3. Tag & tag-synonym hits
    canon = {s.lower(): t.lower() for t, syns in (tag_vocab_map or {}).items() for s in syns}
    tag_pages = {}
    for tag, syns in (tag_vocab_map or {}).items():
        for word in [tag] + list(syns):
            if len(word) >= 3 and re.search(r"(?<![\w-])" + re.escape(norm_text(word)) + r"(?![\w-])", low):
                tag_pages[tag] = [
                    r for r, e in idx.items()
                    if r not in skip and not r.startswith("_")
                    and tag in {canon.get(t, t) for t in e.get("tags", [])}
                ]
                break

    # 4. Non-stopword content tokens from the prompt (catches summaries, compounds, and 2-char uppercase acronyms)
    boilerplate_stop = {
        "pages", "notes", "history", "open", "stand", "value", "fact", "facts", "status", "details",
        "source", "sources", "summary", "table", "timeline", "example", "readme", "template", "write",
        "function", "python", "architecture", "purpose", "current", "state", "decisions", "questions",
    }
    free_tokens = set()
    for raw_tok in re.findall(r"[\wÄÖÜäöüß-]{2,}", prompt):
        clean_tok = raw_tok.strip("-_")
        if not clean_tok:
            continue
        low_tok = clean_tok.lower()
        folded_tok = norm_text(low_tok)
        if low_tok in PROMPT_STOPWORDS or folded_tok in PROMPT_STOPWORDS or low_tok in boilerplate_stop:
            continue
        is_upper_acr = 2 <= len(clean_tok) <= 5 and clean_tok.isupper() and any(c.isalpha() for c in clean_tok)
        if len(clean_tok) >= 5 or is_upper_acr:
            if clean_tok not in cands and low_tok not in {k.lower() for k in cands}:
                cands[clean_tok] = True
                free_tokens.add(clean_tok)

    if not cands and not tag_pages:
        return [], []

    n = max(len(idx), 1)
    scores, hits = {}, {}
    canonical_prefixes = ("areas/", "projects/", "people/", "howto/", "self/", "calendar/", "coach/", "glossary.md")

    for tag, pages in tag_pages.items():
        if pages and len(pages) <= max_df_share * n:
            idf = math.log(1 + n / len(pages))
            for rel in pages:
                scores[rel] = scores.get(rel, 0.0) + 1.5 * idf
                hits.setdefault(rel, set()).add("#" + tag)

    conn = open_graph_db(brain, db_path)
    try:
        trigram_row = conn.execute("SELECT val FROM meta WHERE key='has_trigram'").fetchone()
        has_trigram = bool(trigram_row and trigram_row["val"] == "1")
        # Prioritize explicit vocab terms first, then content words
        ordered_cands = sorted(cands.items(), key=lambda kv: (0 if kv[0] in vocab_terms else 1, -len(kv[0])))[:16]
        for term, word_only in ordered_cands:
            matched = query_term_matches(conn, term, has_trigram=has_trigram, exact_word=word_only and len(term) <= 3)
            is_free = term in free_tokens
            matched = {
                p: s for p, s in matched.items()
                if p in idx and p not in skip and not p.startswith("_")
                and not (is_free and is_folder_hub(p))
            }
            if not matched or len(matched) > max_df_share * n:
                continue
            idf = math.log(1 + n / len(matched))
            for rel, bm_s in matched.items():
                mult = 1.3 if rel.startswith(canonical_prefixes) else 1.0
                score = idf * (1.0 + math.log(1.0 + bm_s)) * mult
                if vocab_terms.get(term) == rel:
                    score += 2.0 * idf
                scores[rel] = scores.get(rel, 0.0) + score
                hits.setdefault(rel, set()).add(term)
    finally:
        conn.close()

    ranked = sorted(scores, key=lambda r: (-scores[r], r))[:max_hints]
    direct = [(rel, idx.get(rel, {}).get("summary", ""), sorted(hits[rel], key=str.lower)[:4]) for rel in ranked]
    exp = expand_1hop_neighbors(
        brain, ranked[:3], skip_pages=skip | set(ranked), max_neighbors=3, include_daily_logs=False, db_path=db_path
    )
    neighbors = [(n_rel, idx.get(n_rel, {}).get("summary", ""), via_rel, kind) for n_rel, _sc, via_rel, kind in exp]
    return direct, neighbors


# ---------------------------------------------------------------- Entity Lexicon & Retroactive Linking (`atlas.py link`)

def build_entity_lexicon(brain, idx=None):
    """Build unambiguous canonical entity lexicon from `people/*.md`, `projects/**`, and leaf `areas/**/*.md`.

    Returns `(lexicon, ambiguous)` where:
      - `lexicon`: `{lower_term: (dst_rel, canonical_term)}`
      - `ambiguous`: `{lower_term: [dst_rel1, dst_rel2, ...]}`
    """
    if idx is None:
        idx = sync_index(brain)
    candidates = {}  # lower_term -> set of (dst_rel, display_term)

    for rel, e in sorted(idx.items()):
        if rel.startswith("_") or is_folder_hub(rel):
            continue
        is_person = rel.startswith("people/") and rel.count("/") == 1
        is_project = rel.startswith("projects/") and (
            (rel.count("/") == 2 and rel.endswith("/README.md")) or (rel.count("/") == 1 and rel.endswith(".md"))
        )
        is_area_leaf = rel.startswith("areas/") and rel.endswith(".md") and not rel.endswith("/README.md")
        if not (is_person or is_project or is_area_leaf):
            continue

        surface_forms = []
        ct = clean_entity_title(e.get("title", ""), rel)
        if ct:
            surface_forms.append(ct)
        for alias in e.get("aliases", []):
            a_clean = str(alias).strip()
            if a_clean:
                surface_forms.append(a_clean)

        for form in surface_forms:
            f = re.sub(r"\s+", " ", form).strip()
            if len(f) < 4 or len(f) > 80:
                continue
            if any(ch in f for ch in ("\n", "\r", "[", "]", "(", ")", "<", ">", "`")):
                continue
            if re.match(r"^(?:\d+|\d{4}-\d{2}-\d{2})$", f):
                continue
            low = f.lower()
            if low in LINK_STOPWORDS or low in PROMPT_STOPWORDS:
                continue
            candidates.setdefault(low, set()).add((rel, f))

    lexicon = {}
    ambiguous = {}
    for low, pairs in candidates.items():
        rels = sorted({r for r, _f in pairs})
        if len(rels) == 1:
            disp = sorted(pairs)[0][1]
            lexicon[low] = (rels[0], disp)
        else:
            ambiguous[low] = rels
    return lexicon, ambiguous


def _compile_lexicon_regex(lexicon):
    if not lexicon:
        return None
    # Longest terms first so "Max Mustermann" wins over "Mustermann"
    ordered = sorted(lexicon.keys(), key=lambda s: (-len(s), s))
    alt = "|".join(re.escape(k) for k in ordered)
    return re.compile(rf"(?<![\w/.-])({alt})(?![\w/.-])", re.IGNORECASE)


def scan_retroactive_links(brain, idx=None, db_path=None):
    """Scan existing brain for unlinked canonical entity mentions and unlinked literal `brain/` paths in `journal/`."""
    if idx is None:
        idx = sync_index(brain, db_path=db_path)
    lexicon, ambiguous = build_entity_lexicon(brain, idx)
    lex_rx = _compile_lexicon_regex(lexicon)

    conn = open_graph_db(brain, db_path)
    try:
        existing_body_edges = {
            (r["src"], r["dst"]) for r in conn.execute("SELECT src, dst FROM edges WHERE kind IN ('body', 'journal')")
        }
        existing_rel_edges = {
            (r["src"], r["dst"]) for r in conn.execute("SELECT src, dst FROM edges WHERE kind = 'related'")
        }
    finally:
        conn.close()

    known_md = {r for r in idx if r.lower().endswith(".md")}
    rel_lower = {r.lower(): r for r in known_md}

    mentions = {}       # src_rel -> list of {"start", "end", "matched", "dst", "line", "snippet", "has_body", "has_related"}
    journal_paths = {}  # j_rel -> list of {"start", "end", "raw", "label", "dst", "href", "line"}

    scan_prefixes = ("areas/", "people/", "projects/", "howto/", "self/", "journal/")
    for src_rel in sorted(known_md):
        if not src_rel.startswith(scan_prefixes) or is_folder_hub(src_rel):
            continue
        full = os.path.join(brain, src_rel)
        try:
            with open(full, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue

        # 1. Journal literal file paths (both bare `areas/x.md` and backticked `` `areas/x.md` ``)
        if src_rel.startswith("journal/"):
            masked_j = mask_prose_for_mentions(text, mask_inline_code=False)
            j_hits = []
            for m in BRAIN_PATH_RX.finditer(masked_j):
                orig_slice = text[m.start():m.end()]
                tick = m.group(1)
                raw_p = m.group(2)
                clean_p = raw_p[6:] if raw_p.startswith("brain/") else raw_p
                dst = clean_p if clean_p in known_md else rel_lower.get(clean_p.lower())
                if not dst or dst == src_rel:
                    continue
                href = fwd(os.path.relpath(os.path.join(brain, dst), os.path.join(brain, os.path.dirname(src_rel))))
                j_hits.append({
                    "start": m.start(),
                    "end": m.end(),
                    "raw": orig_slice,
                    "label": raw_p,
                    "tick": tick,
                    "dst": dst,
                    "href": href,
                    "line": masked_j[:m.start()].count("\n") + 1,
                })
            if j_hits:
                journal_paths[src_rel] = j_hits

        # 2. Canonical entity mentions in prose (skip pure auto-generated daily logs for inline/related entity backfill)
        if lex_rx and not is_daily_log_file(src_rel):
            masked_p = mask_prose_for_mentions(text, mask_inline_code=True)
            seen_dsts = set()
            page_hits = []
            for m in lex_rx.finditer(masked_p):
                key = m.group(1).lower()
                info = lexicon.get(key)
                if not info:
                    continue
                dst_rel, _disp = info
                if dst_rel == src_rel or dst_rel in seen_dsts:
                    continue
                seen_dsts.add(dst_rel)
                matched_orig = text[m.start():m.end()]
                page_hits.append({
                    "start": m.start(),
                    "end": m.end(),
                    "matched": matched_orig,
                    "dst": dst_rel,
                    "line": masked_p[:m.start()].count("\n") + 1,
                    "snippet": _line_context(text, m.start()),
                    "has_body": (src_rel, dst_rel) in existing_body_edges,
                    "has_related": (src_rel, dst_rel) in existing_rel_edges,
                })
            if page_hits:
                mentions[src_rel] = page_hits

    return {
        "lexicon_size": len(lexicon),
        "ambiguous_skipped": len(ambiguous),
        "mentions": mentions,
        "journal_paths": journal_paths,
    }


def _add_frontmatter_related(text, new_rels):
    """Idempotently add canonical `new_rels` into YAML frontmatter `related:` without touching body prose."""
    if not new_rels or not text.startswith("---"):
        return text, 0
    end = text.find("\n---", 3)
    if end < 0:
        return text, 0
    fm = parse_frontmatter(text)
    cur_list = [str(x).strip() for x in as_list(fm.get("related")) if str(x).strip()]
    to_add = [r for r in new_rels if r not in cur_list]
    if not to_add:
        return text, 0

    merged = cur_list + to_add
    head = text[:end]
    tail = text[end:]

    # Case 1: existing inline `related: [...]` or `related: val`
    m_inline = re.search(r"(?m)^related\s*:\s*\[[^\n]*\]\s*$", head)
    if m_inline:
        new_line = "related: [" + ", ".join(merged) + "]"
        head = head[:m_inline.start()] + new_line + head[m_inline.end():]
        return head + tail, len(to_add)

    # Case 2: existing block `related:` list
    m_block = re.search(r"(?m)^related\s*:\s*$(?:\n[ \t]*-\s+.*$)*", head)
    if m_block:
        added_lines = "".join(f"\n  - {r}" for r in to_add)
        head = head[:m_block.end()] + added_lines + head[m_block.end():]
        return head + tail, len(to_add)

    # Case 3: single scalar `related: foo`
    m_scalar = re.search(r"(?m)^related\s*:\s*\S+.*$", head)
    if m_scalar:
        new_line = "related: [" + ", ".join(merged) + "]"
        head = head[:m_scalar.start()] + new_line + head[m_scalar.end():]
        return head + tail, len(to_add)

    # Case 4: no `related:` key yet -> append before closing `\n---`
    new_line = "\nrelated: [" + ", ".join(merged) + "]"
    return head + new_line + tail, len(to_add)


def apply_retroactive_links(brain, do_journals=False, do_related=False, do_inline=False, execute=False, db_path=None):
    """Execute or preview retroactive wiki-linking across `journal/` paths, `related:` frontmatter, and inline prose."""
    idx = sync_index(brain, db_path=db_path)
    scan = scan_retroactive_links(brain, idx=idx, db_path=db_path)
    actions = []
    files_changed = 0
    links_added = 0

    if do_journals:
        for j_rel, hits in sorted(scan["journal_paths"].items()):
            full = os.path.join(brain, j_rel)
            try:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            new_text = text
            for h in sorted(hits, key=lambda x: x["start"], reverse=True):
                repl = f"[{h['label']}]({h['href']})"
                new_text = new_text[:h["start"]] + repl + new_text[h["end"]:]
            if new_text != text:
                files_changed += 1
                links_added += len(hits)
                actions.append(f"journal {j_rel}: link {len(hits)} path(s)")
                if execute:
                    with open(full, "w", encoding="utf-8", newline="\n") as fh:
                        fh.write(new_text)

    if do_related or do_inline:
        for src_rel, hits in sorted(scan["mentions"].items()):
            unlinked_body = [h for h in hits if not h["has_body"]]
            unlinked_rel = [h for h in hits if not h["has_related"] and not h["has_body"]]
            if not ((do_inline and unlinked_body) or (do_related and unlinked_rel)):
                continue
            full = os.path.join(brain, src_rel)
            try:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            new_text = text
            c_inline = 0
            c_rel = 0

            # Apply inline prose replacements first (before frontmatter changes shift body offsets!)
            if do_inline and unlinked_body:
                non_overlapping = []
                last_end = -1
                for h in sorted(unlinked_body, key=lambda x: x["start"]):
                    if h["start"] >= last_end:
                        non_overlapping.append(h)
                        last_end = h["end"]
                for h in reversed(non_overlapping):
                    href = fwd(
                        os.path.relpath(os.path.join(brain, h["dst"]), os.path.join(brain, os.path.dirname(src_rel)))
                    )
                    repl = f"[{h['matched']}]({href})"
                    new_text = new_text[:h["start"]] + repl + new_text[h["end"]:]
                    c_inline += 1

            if do_related and unlinked_rel:
                new_text, c_rel = _add_frontmatter_related(new_text, [h["dst"] for h in unlinked_rel])

            if new_text != text:
                files_changed += 1
                links_added += c_inline + c_rel
                parts = []
                if c_inline:
                    parts.append(f"{c_inline} inline")
                if c_rel:
                    parts.append(f"{c_rel} related:")
                actions.append(f"page {src_rel}: {', '.join(parts)}")
                if execute:
                    with open(full, "w", encoding="utf-8", newline="\n") as fh:
                        fh.write(new_text)

    if execute and files_changed:
        sync_index(brain, db_path=db_path)

    return {
        "scan": scan,
        "actions": actions,
        "files_changed": files_changed,
        "links_added": links_added,
    }


# ---------------------------------------------------------------- Wikilink Compiler & Graph Check

def compile_wikilinks_in_text(brain, src_rel, text, known_md, rel_lower, by_stem, by_name, headings_by_rel):
    """Compile unambiguous `[[Target#Section|Label]]` outside code blocks into relative `[Label](../path.md#sec)`."""
    masked = mask_all_code(text)
    matches = list(WIKILINK_RX.finditer(masked))
    if not matches:
        return text, 0, [], []

    new_text = text
    compiled = 0
    unresolved = []
    ambiguous = []

    for m in reversed(matches):
        raw_inner = text[m.start() + 2:m.end() - 2].strip()
        if not raw_inner or "<" in raw_inner:
            continue
        # Ignore array-like numeric literals just in case (`[[0, 1]]`)
        if re.match(r"^[\d\s,.;:-]+$", raw_inner):
            continue
        if "|" in raw_inner:
            target_part, label_part = raw_inner.split("|", 1)
            label_part = label_part.strip()
        else:
            target_part, label_part = raw_inner, None

        dst, sec_raw, cands = _resolve_target_internal(
            target_part, src_rel, known_md, rel_lower, by_stem, by_name
        )
        if dst is None:
            if len(cands) > 1:
                ambiguous.append((raw_inner, cands))
            else:
                unresolved.append(raw_inner)
            continue

        anchor = ""
        if sec_raw:
            slug = slugify_heading(sec_raw)
            page_anchors = headings_by_rel.get(dst, {})
            if slug in page_anchors:
                anchor = slug
            else:
                # Match by case-insensitive heading title if exact slug differs
                for anc, htitle in page_anchors.items():
                    if htitle.lower() == sec_raw.lower():
                        anchor = anc
                        break
                if not anchor:
                    anchor = slug

        if dst == src_rel and anchor:
            href = f"#{anchor}"
        else:
            rel_url = fwd(os.path.relpath(os.path.join(brain, dst), os.path.join(brain, os.path.dirname(src_rel))))
            href = f"{rel_url}#{anchor}" if anchor else rel_url

        if label_part:
            label = label_part
        else:
            clean_t = target_part.strip()
            if clean_t.startswith("#"):
                label = clean_t[1:].strip()
            else:
                label = clean_t

        repl = f"[{label}]({href})"
        new_text = new_text[:m.start()] + repl + new_text[m.end():]
        compiled += 1

    return new_text, compiled, list(reversed(unresolved)), list(reversed(ambiguous))


def _looks_like_local_source_path(s):
    val = str(s or "").strip()
    if not val or "<" in val or ">" in val:
        return False
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", val):  # URL scheme (https:, mailto:, etc.)
        return False
    if " " in val and not (val.startswith(("./", "../", "/", "~/")) or LOCAL_EXT_RX.search(val)):
        return False
    if val.startswith(("./", "../", "/", "~/")):
        return True
    if re.match(r"^(?:brain/)?(?:areas|projects|people|howto|self|inbox|journal|calendar|coach|files)/", val):
        return True
    return bool(LOCAL_EXT_RX.search(val))


def _source_exists(brain, root, page_rel, src_item):
    raw = str(src_item).strip()
    if raw.startswith("~/"):
        return os.path.exists(os.path.expanduser(raw))
    if os.path.isabs(raw):
        return os.path.exists(raw)
    clean = raw[6:] if raw.startswith("brain/") else raw
    for cand in (
        os.path.join(brain, os.path.dirname(page_rel), raw),
        os.path.join(brain, clean),
        os.path.join(root, raw),
    ):
        if os.path.exists(cand):
            return True
    return False


def validate_and_analyze_brain(brain, root, rels_map, idx=None, db_path=None):
    """Code-block-safe graph check for `atlas.py check` + repair plan for `check --fix`."""
    if idx is None:
        idx = sync_index(brain, db_path=db_path)

    conn = open_graph_db(brain, db_path)
    try:
        headings_by_rel = {}
        for row in conn.execute("SELECT rel, anchor, title FROM headings"):
            headings_by_rel.setdefault(row["rel"], {})[row["anchor"]] = row["title"]
        horiz_nodes = set()
        for row in conn.execute("SELECT src, dst FROM edges WHERE kind IN ('body', 'related')"):
            s, d = row["src"], row["dst"]
            if not is_folder_hub(s) and not is_folder_hub(d):
                horiz_nodes.add(s)
                horiz_nodes.add(d)
    finally:
        conn.close()

    known_md = set(rels_map.keys())
    rel_lower, by_stem, by_name = _build_lookup_maps(known_md, idx)
    by_base = {}
    for r in rels_map:
        by_base.setdefault(os.path.basename(r).lower(), []).append(r)

    graph = {}
    broken = []
    broken_anchors = []
    fixes = {}
    wikilink_files = {}  # rel -> new_text
    wikilink_count = 0
    ambiguous_wikilinks = []
    missing_sources = []
    stale_verified = []
    today = dt.date.today()

    for rel, full_p in sorted(rels_map.items()):
        try:
            with open(full_p, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue

        masked_code = mask_all_code(text)
        masked_fenced = mask_fenced_and_fm(text)
        targets = set()

        # 1. Standard Markdown links `[Label](target#anchor)` outside code blocks
        for m in LINK_RX.finditer(masked_code):
            orig = LINK_RX.match(text[m.start():m.end()])
            raw_href = (orig.group(2) if orig else m.group(2)).strip()
            if not raw_href or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", raw_href) or raw_href.startswith(("/", "~")) or "<" in raw_href:
                continue
            t, _, frag = raw_href.partition("#")
            if not t:
                if frag and not rel.startswith("_atlas/"):
                    slug = slugify_heading(frag)
                    if slug and slug not in headings_by_rel.get(rel, {}):
                        broken_anchors.append(f"{rel} -> #{frag}")
                continue
            tgt = fwd(os.path.normpath(os.path.join(os.path.dirname(rel), t)))
            full = os.path.join(brain, tgt)
            if not os.path.exists(full):
                if not rel.startswith("_atlas/"):
                    broken.append(f"{rel} -> {t}")
                    cand = by_base.get(os.path.basename(t).lower(), [])
                    if len(cand) == 1:
                        new_t = fwd(os.path.relpath(os.path.join(brain, cand[0]), os.path.join(brain, os.path.dirname(rel))))
                        fixes.setdefault(rel, []).append((t, new_t))
            else:
                resolved_md = None
                if tgt.endswith(".md"):
                    resolved_md = tgt
                    targets.add(tgt)
                elif os.path.isdir(full) and os.path.isfile(os.path.join(full, "README.md")):
                    resolved_md = fwd(os.path.join(tgt, "README.md"))
                    targets.add(resolved_md)
                if frag and resolved_md and not rel.startswith("_atlas/"):
                    slug = slugify_heading(frag)
                    if slug and slug not in headings_by_rel.get(resolved_md, {}):
                        broken_anchors.append(f"{rel} -> {t}#{frag}")

        # 2. Inline backtick file paths (outside fenced code blocks)
        for m in TICK_RX.finditer(masked_fenced):
            t_rel = m.group(1)
            if t_rel.startswith("brain/"):
                t_rel = t_rel[6:]
            if os.path.exists(os.path.join(brain, t_rel)):
                targets.add(t_rel)

        # 3. Frontmatter `related:` targets count toward graph reachability
        e = idx.get(rel, {})
        for rel_ref in e.get("related", []):
            dst, _sec, _c = _resolve_target_internal(rel_ref, rel, known_md, rel_lower, by_stem, by_name)
            if dst:
                targets.add(dst)

        # 4. Explicit `[[Wikilink]]` compilation & validation (on grown pages)
        if not rel.startswith("_atlas/"):
            compiled_text, n_comp, unrel, ambig = compile_wikilinks_in_text(
                brain, rel, text, known_md, rel_lower, by_stem, by_name, headings_by_rel
            )
            if n_comp:
                wikilink_files[rel] = (compiled_text, n_comp)
                wikilink_count += n_comp
                # Also add compiled targets to reachability graph immediately
                for m_w in LINK_RX.finditer(mask_all_code(compiled_text)):
                    t_w = m_w.group(2).split("#")[0]
                    if t_w and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", t_w) and not t_w.startswith(("/", "~")):
                        tgt_w = fwd(os.path.normpath(os.path.join(os.path.dirname(rel), t_w)))
                        if tgt_w in known_md:
                            targets.add(tgt_w)
            for u in unrel:
                broken.append(f"{rel} -> [[{u}]]")
            for a_raw, a_cands in ambig:
                ambiguous_wikilinks.append(f"{rel} -> [[{a_raw}]] ({', '.join(a_cands[:4])})")

            # 5. Provenance `sources:` local path check
            for src_item in e.get("sources", []):
                if _looks_like_local_source_path(src_item) and not _source_exists(brain, root, rel, src_item):
                    missing_sources.append(f"{rel} -> {src_item}")

            # 6. Bi-temporal `verified:` staleness check (> 365 days, unless expired `valid_until` in the past)
            ver_str = e.get("verified", "")
            val_str = e.get("valid_until", "")
            expired = False
            if re.match(r"^\d{4}-\d{2}-\d{2}$", val_str):
                try:
                    if dt.date.fromisoformat(val_str) < today:
                        expired = True
                except ValueError:
                    pass
            if not expired and re.match(r"^\d{4}-\d{2}-\d{2}$", ver_str):
                try:
                    age_d = (today - dt.date.fromisoformat(ver_str)).days
                    if age_d > 365:
                        stale_verified.append(f"{rel} (verified {ver_str}, {age_d}d ago)")
                except ValueError:
                    pass

        graph[rel] = targets

    isolated = sorted(
        r for r in idx
        if r.startswith(("areas/", "people/", "projects/", "howto/"))
        and not is_folder_hub(r)
        and r not in horiz_nodes
    )

    return {
        "graph": graph,
        "broken": broken,
        "broken_anchors": broken_anchors,
        "fixes": fixes,
        "wikilink_files": wikilink_files,
        "wikilink_count": wikilink_count,
        "ambiguous_wikilinks": ambiguous_wikilinks,
        "missing_sources": missing_sources,
        "stale_verified": stale_verified,
        "isolated": isolated,
    }


# ---------------------------------------------------------------- `atlas.py graph` & `atlas.py context`

def resolve_page_ref(brain, target_ref, idx=None, db_path=None):
    """Resolve user/CLI target (`areas/x.md`, `brain/areas/x.md`, `Max Muster`, `bank#section`) -> `(rel, sec, cands)`."""
    if idx is None:
        idx = sync_index(brain, db_path=db_path)
    known_md = {r for r in idx if r.lower().endswith(".md")}
    rel_lower, by_stem, by_name = _build_lookup_maps(known_md, idx)
    return _resolve_target_internal(target_ref, "", known_md, rel_lower, by_stem, by_name)


def inspect_page_graph(brain, target_ref, idx=None, db_path=None):
    """Return structured graph report (`page`, `outlinks`, `backlinks`, `unlinked_mentions`) for `atlas.py graph`."""
    if idx is None:
        idx = sync_index(brain, db_path=db_path)
    rel, _sec, cands = resolve_page_ref(brain, target_ref, idx=idx, db_path=db_path)
    if not rel:
        return {"error": f"Page not found or ambiguous: {target_ref}", "candidates": cands}

    conn = open_graph_db(brain, db_path)
    try:
        outlinks = [
            {"dst": r["dst"], "kind": r["kind"], "anchor": r["anchor"], "context": r["context"],
             "summary": idx.get(r["dst"], {}).get("summary", "")}
            for r in conn.execute(
                "SELECT dst, kind, anchor, context FROM edges WHERE src = ? ORDER BY CASE kind WHEN 'related' THEN 0 WHEN 'body' THEN 1 WHEN 'journal' THEN 2 ELSE 3 END, dst",
                (rel,),
            )
        ]
        backlinks = [
            {"src": r["src"], "kind": r["kind"], "anchor": r["anchor"], "context": r["context"],
             "summary": idx.get(r["src"], {}).get("summary", "")}
            for r in conn.execute(
                "SELECT src, kind, anchor, context FROM edges WHERE dst = ? ORDER BY CASE kind WHEN 'related' THEN 0 WHEN 'body' THEN 1 WHEN 'journal' THEN 2 ELSE 3 END, src",
                (rel,),
            )
        ]
    finally:
        conn.close()

    # Unlinked mentions targeting `rel`
    scan = scan_retroactive_links(brain, idx=idx, db_path=db_path)
    unlinked = []
    for src_rel, hits in sorted(scan["mentions"].items()):
        for h in hits:
            if h["dst"] == rel and not h["has_body"] and not h["has_related"]:
                unlinked.append({"src": src_rel, "line": h["line"], "matched": h["matched"], "context": h["snippet"]})
    for j_rel, hits in sorted(scan["journal_paths"].items()):
        for h in hits:
            if h["dst"] == rel:
                unlinked.append({"src": j_rel, "line": h["line"], "matched": h["raw"], "context": f"unlinked journal path ({h['label']})"})

    e = idx.get(rel, {})
    return {
        "page": {
            "rel": rel,
            "title": e.get("title", ""),
            "summary": e.get("summary", ""),
            "tags": e.get("tags", []),
            "verified": e.get("verified", ""),
            "valid_until": e.get("valid_until", ""),
            "sources": e.get("sources", []),
            "related": e.get("related", []),
        },
        "outlinks": outlinks,
        "backlinks": backlinks,
        "unlinked_mentions": unlinked,
    }


def extract_section_markdown(text, section_query):
    """Extract only the heading block matching `section_query` (plus YAML frontmatter header line if desired)."""
    if not section_query:
        return text
    target_slug = slugify_heading(section_query)
    masked = mask_all_code(text)
    matches = list(HEADING_RX.finditer(masked))
    for i, m in enumerate(matches):
        raw_line = text[m.start():m.end()]
        hm = HEADING_RX.match(raw_line)
        level = len(hm.group(1) if hm else m.group(1))
        htitle = (hm.group(2) if hm else m.group(2)).strip()
        if slugify_heading(htitle) == target_slug or htitle.lower() == section_query.lower():
            end_pos = len(text)
            for nxt in matches[i + 1:]:
                if len(nxt.group(1)) <= level:
                    end_pos = nxt.start()
                    break
            return text[m.start():end_pos].strip() + "\n"
    return text


def build_context_dossier(brain, target_query, hops=1, budget=24000, tag_vocab_map=None, idx=None, db_path=None):
    """Assemble a token-budgeted Markdown context dossier (`target` + backlinks + specificity-ranked neighbors)."""
    if idx is None:
        idx = sync_index(brain, db_path=db_path)
    budget = max(500, int(budget or 24000))
    hops = max(0, int(hops if hops is not None else 1))

    rel, sec, _cands = resolve_page_ref(brain, target_query, idx=idx, db_path=db_path)
    seed_rels = []
    if rel:
        seed_rels = [rel]
    else:
        words = [w for w in re.split(r"\s+", str(target_query or "").strip()) if w]
        ranked, _nb = search_pages(brain, words, idx=idx, tag_vocab_map=tag_vocab_map, limit=3, db_path=db_path)
        seed_rels = ranked
        sec = ""

    if not seed_rels:
        return f"# Context Dossier: {target_query}\n\n(No matching pages found in brain.)\n"

    chunks = [f"# Context Dossier: {target_query}\n"]
    used = len(chunks[0])

    def _append_within_budget(block):
        nonlocal used
        if used >= budget:
            return False
        rem = budget - used
        if len(block) <= rem:
            chunks.append(block)
            used += len(block)
            return True
        if rem > 160:
            cut = block[:rem - 60].rsplit("\n", 1)[0]
            tail = f"\n...[truncated at budget {budget} chars]\n"
            chunks.append(cut + tail)
            used += len(cut) + len(tail)
        return False

    # 1. Primary seed page(s)
    primary_cap = int(budget * (0.65 if hops > 0 else 0.92))
    for i, s_rel in enumerate(seed_rels):
        full = os.path.join(brain, s_rel)
        try:
            with open(full, encoding="utf-8", errors="replace") as fh:
                raw_txt = fh.read(MAX_PARSE_BYTES)
        except OSError:
            continue
        e = idx.get(s_rel, {})
        meta_bits = [f"path: `{s_rel}`"]
        if e.get("tags"):
            meta_bits.append(f"tags: `{', '.join(e['tags'])}`")
        if e.get("verified"):
            meta_bits.append(f"verified: `{e['verified']}`")
        if e.get("valid_until"):
            meta_bits.append(f"valid_until: `{e['valid_until']}`")
        body = extract_section_markdown(raw_txt, sec) if (i == 0 and sec) else raw_txt[frontmatter_end_offset(raw_txt):].strip()
        per_seed_cap = max(800, primary_cap // max(1, len(seed_rels)))
        if len(body) > per_seed_cap:
            body = body[:per_seed_cap].rsplit("\n", 1)[0] + "\n...[section excerpt]..."
        header = f"\n## Primary: {s_rel}" + (f"#{slugify_heading(sec)}" if (i == 0 and sec) else "") + "\n"
        header += " · ".join(meta_bits) + "\n"
        if e.get("summary"):
            header += f"> {e['summary']}\n"
        if not _append_within_budget(header + "\n" + body + "\n"):
            return "".join(chunks)

    # 2. Compact Backlinks overview for primary page
    ginfo = inspect_page_graph(brain, seed_rels[0], idx=idx, db_path=db_path)
    backlinks = [b for b in ginfo.get("backlinks", []) if b["kind"] != "tree"]
    if backlinks:
        bl_lines = ["\n## Backlinks\n"]
        for b in backlinks[:8]:
            anc = f"#{b['anchor']}" if b.get("anchor") else ""
            bl_lines.append(f"- `{b['src']}{anc}` ({b['kind']}): {b['context']}")
        _append_within_budget("\n".join(bl_lines) + "\n")

    # 3. Specificity-ranked multi-hop expansion
    frontier = list(seed_rels)
    visited = set(seed_rels)
    conn = open_graph_db(brain, db_path)
    try:
        for hop in range(1, hops + 1):
            if used >= budget - 200 or not frontier:
                break
            expanded = expand_1hop_neighbors(
                brain, frontier, skip_pages=visited, max_neighbors=5, include_daily_logs=False, db_path=db_path
            )
            if not expanded:
                break
            next_frontier = []
            for n_rel, sc, via_rel, kind in expanded:
                if used >= budget - 200:
                    break
                visited.add(n_rel)
                next_frontier.append(n_rel)
                # Check if there is a specific #anchor on the edge `via_rel -> n_rel`
                arow = conn.execute(
                    "SELECT anchor FROM edges WHERE src = ? AND dst = ? AND anchor != '' LIMIT 1",
                    (via_rel, n_rel),
                ).fetchone()
                target_anc = arow["anchor"] if arow else ""
                full_n = os.path.join(brain, n_rel)
                try:
                    with open(full_n, encoding="utf-8", errors="replace") as fh:
                        n_txt = fh.read(MAX_PARSE_BYTES)
                except OSError:
                    continue
                ne = idx.get(n_rel, {})
                n_body = extract_section_markdown(n_txt, target_anc) if target_anc else n_txt[frontmatter_end_offset(n_txt):].strip()
                if len(n_body) > 3000:
                    n_body = n_body[:3000].rsplit("\n", 1)[0] + "\n..."
                sec_hdr = (
                    f"\n## Neighbor ({hop}-hop, {kind} via `{via_rel}`, specificity {sc:.2f}): "
                    f"{n_rel}" + (f"#{target_anc}" if target_anc else "") + "\n"
                )
                if ne.get("summary"):
                    sec_hdr += f"> {ne['summary']}\n"
                if not _append_within_budget(sec_hdr + "\n" + n_body + "\n"):
                    break
            frontier = next_frontier
    finally:
        conn.close()

    return "".join(chunks)


# ---------------------------------------------------------------- Structured Conflict Queue (`_atlas/conflicts.json`)

def default_conflicts_path(brain):
    return os.path.join(brain, "_atlas", "conflicts.json")


def load_conflicts(brain, conflicts_path=None):
    path = conflicts_path or default_conflicts_path(brain)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return list(data.get("items", []))
        if isinstance(data, list):
            return data
    except (OSError, ValueError):
        pass
    return []


def save_conflicts(brain, items, conflicts_path=None):
    path = conflicts_path or default_conflicts_path(brain)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    payload = {"version": 1, "items": items}
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def open_conflicts(brain, conflicts_path=None):
    return [c for c in load_conflicts(brain, conflicts_path) if c.get("status", "open") == "open"]


def add_conflict(brain, pages, summary, conflicts_path=None):
    items = load_conflicts(brain, conflicts_path)
    clean_pages = []
    for p in (pages or []):
        for part in str(p).split(","):
            s = fwd(part.strip())
            if s.startswith("brain/"):
                s = s[6:]
            if s and s not in clean_pages:
                clean_pages.append(s)
    summ = str(summary or "").strip()
    # Deduplicate identical open conflict
    for ex in items:
        if ex.get("status") == "open" and ex.get("summary", "").lower() == summ.lower() and sorted(ex.get("pages", [])) == sorted(clean_pages):
            return ex
    cid = "c-" + hashlib.sha1(f"{'|'.join(sorted(clean_pages))}|{summ}|{time.time_ns()}".encode("utf-8")).hexdigest()[:8]
    entry = {
        "id": cid,
        "status": "open",
        "pages": clean_pages,
        "summary": summ,
        "created": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "resolved": "",
        "resolution": "",
    }
    items.append(entry)
    save_conflicts(brain, items, conflicts_path)
    return entry


def resolve_conflict(brain, conflict_id, resolution="", conflicts_path=None):
    items = load_conflicts(brain, conflicts_path)
    cid = str(conflict_id or "").strip()
    matched = None
    for it in items:
        if it.get("id") == cid or (cid and it.get("id", "").startswith(cid) and it.get("status") == "open"):
            it["status"] = "resolved"
            it["resolved"] = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
            if resolution:
                it["resolution"] = str(resolution).strip()
            matched = it
            break
    if matched is not None:
        save_conflicts(brain, items, conflicts_path)
    return matched
