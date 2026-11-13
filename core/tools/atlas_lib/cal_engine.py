"""Atlas Loopback Calendar, Brain Graph & Adaptive Coach UI Engine (Python 3 stdlib only).

Loads `templates/calendar.html`, compiles the zero-token JSON payload (Calendar + Adaptive Coach +
SQLite Knowledge Graph + CSRF token), safely toggles Markdown checkboxes inside `<BRAIN>/`, and
manages the loopback `127.0.0.1` HTTP server (`atlas-cal-v3`).
"""
import datetime as dt
import hmac
import json
import math
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.request

sys.dont_write_bytecode = True

from atlas_lib import graph_engine

CAL_SERVER_HEALTH_TOKEN = b"atlas-cal-v3"

_CHECKBOX_LINE_RX = re.compile(r"^(\s*(?:[-*+]|\d+\.)?\s*)\[([ xX-])\](\s+.*)$")
_FENCE_RX = re.compile(r"^\s*(`{3,}|~{3,})")


def resolve_template_path(atlas_dir=None):
    """Locate `templates/calendar.html` in installed `<BRAIN>/_atlas/` or repo `core/`."""
    candidates = []
    if atlas_dir:
        candidates.append(os.path.join(atlas_dir, "templates", "calendar.html"))
    # Relative to `.../tools/atlas_lib/cal_engine.py` -> `.../templates/calendar.html`
    here = os.path.dirname(os.path.abspath(__file__))
    core_root = os.path.normpath(os.path.join(here, "..", ".."))
    candidates.append(os.path.join(core_root, "templates", "calendar.html"))
    for p in candidates:
        if os.path.isfile(p):
            return p
    return candidates[0]


def load_calendar_template(atlas_dir=None):
    """Read `templates/calendar.html` from disk with a minimal offline fallback if missing."""
    tpl_path = resolve_template_path(atlas_dir=atlas_dir)
    try:
        with open(tpl_path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return (
            "<!DOCTYPE html><html lang='de'><head><meta charset='utf-8'>"
            "<title>Atlas · Calendar &amp; Brain Hub</title></head><body>"
            "<script>\nlet DATA = __DATA_JSON__;\n</script>"
            "<pre>Template missing: templates/calendar.html</pre></body></html>\n"
        )


def safe_json_for_script(payload):
    """Serialize `payload` as single-line JSON safe for embedding inside `<script>` tags.

    Escapes `</` -> `<\\/` (valid JSON escape, prevents `</script>` DOM breakouts) and
    JS line terminators U+2028/U+2029 while remaining 100% parseable by `JSON.parse()`.
    """
    raw = json.dumps(payload, ensure_ascii=False)
    return (
        raw.replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def csrf_token_path(cache_dir):
    return os.path.join(cache_dir, "cal_csrf.txt")


def get_or_create_csrf_token(cache_dir):
    """Return a persistent 48-hex-char CSRF secret stored inside `<BRAIN>/_atlas/cache/cal_csrf.txt`."""
    os.makedirs(cache_dir, exist_ok=True)
    p = csrf_token_path(cache_dir)
    try:
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as fh:
                tok = fh.read().strip()
            if len(tok) >= 32 and all(c in "0123456789abcdefABCDEF" for c in tok):
                return tok
    except OSError:
        pass
    tok = secrets.token_hex(24)
    tmp = f"{p}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(tok + "\n")
        if os.name != "nt":
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
        os.replace(tmp, p)
    except OSError:
        try:
            with open(p, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(tok + "\n")
        except OSError:
            pass
    return tok


def verify_csrf_token(cache_dir, candidate):
    """Constant-time verification of a client-supplied CSRF token against `cal_csrf.txt`."""
    cand = str(candidate or "").strip()
    if len(cand) < 32:
        return False
    p = csrf_token_path(cache_dir)
    try:
        with open(p, encoding="utf-8") as fh:
            expected = fh.read().strip()
    except OSError:
        return False
    if len(expected) < 32:
        return False
    return hmac.compare_digest(expected, cand)


def _node_domain(rel):
    if "/" not in rel:
        return "root"
    top = rel.split("/", 1)[0].lower()
    if top in ("people", "areas", "projects", "howto", "self", "coach", "calendar", "journal", "inbox", "skills"):
        return top
    return "other"


def _norm_todo_compare(text):
    """Normalize Markdown task text for resilient matching when line numbers shift."""
    s = str(text or "")
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = s.replace("**", "").replace("`", "").strip()
    s = re.sub(r"^\d{4}-\d{2}-\d{2}(?:\.\.\d{4}-\d{2}-\d{2})?\s+", "", s)
    s = re.sub(r"^\d{1,2}:\d{2}\s+", "", s)
    s = re.sub(r"^\[[a-zA-Z0-9_:-]+\]\s*", "", s)
    s = re.sub(r"\s*:\s*$", "", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _validate_brain_rel_path(brain, rel):
    """Validate `rel` is an existing `.md` file strictly inside `brain/` and outside `_atlas`/`_secrets`/`_archive`."""
    if not rel or not isinstance(rel, str):
        return None, "missing file path"
    clean = rel.strip().replace("\\", "/")
    if clean.startswith("brain/"):
        clean = clean[6:]
    if not clean or clean.startswith(("/", "~")) or ":" in clean:
        return None, "absolute or invalid path rejected"
    parts = [p for p in clean.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None, "path traversal rejected"
    if parts[0].startswith(".") or parts[0] in ("_atlas", "_secrets", "_archive", "node_modules", "__pycache__"):
        return None, "protected directory rejected"
    norm_rel = "/".join(parts)
    if not norm_rel.lower().endswith(".md"):
        return None, "only Markdown (.md) files can be toggled"
    brain_real = os.path.realpath(brain)
    target_full = os.path.join(brain, *parts)
    target_real = os.path.realpath(target_full)
    if target_real != brain_real and not target_real.startswith(brain_real + os.sep):
        return None, "symlink or path escape outside brain rejected"
    rel_after_real = os.path.relpath(target_real, brain_real).replace("\\", "/")
    first_seg = rel_after_real.split("/", 1)[0]
    if first_seg.startswith(".") or first_seg in ("_atlas", "_secrets", "_archive"):
        return None, "protected target rejected"
    if not os.path.isfile(target_real):
        return None, f"file not found: {norm_rel}"
    return (norm_rel, target_real), None


def find_checkbox_lines(lines):
    """Yield `(idx_0based, box_char, rest_text)` for all Markdown checkbox lines outside frontmatter & code fences."""
    in_fm = False
    fence_marker = None
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if idx == 0 and stripped == "---":
            in_fm = True
            continue
        if in_fm:
            if stripped in ("---", "..."):
                in_fm = False
            continue
        m_fence = _FENCE_RX.match(line)
        if m_fence:
            marker = m_fence.group(1)
            if fence_marker is None:
                fence_marker = marker[0]
            elif marker[0] == fence_marker:
                fence_marker = None
            continue
        if fence_marker is not None:
            continue
        m_cb = _CHECKBOX_LINE_RX.match(line)
        if m_cb:
            yield idx, m_cb.group(2), m_cb.group(3)


def toggle_todo_in_brain(brain, rel, line_no=None, what="", target_done=None):
    """Safely toggle `- [ ]` <-> `- [x]` on a task/workout line inside `<BRAIN>/<rel>`.

    Enforces strict path confinement, ignores YAML frontmatter and fenced code blocks,
    verifies `line_no` / `what`, and writes atomically with LF line endings.
    """
    validated, err = _validate_brain_rel_path(brain, rel)
    if err:
        return {"ok": False, "error": err, "code": 400}
    norm_rel, target_real = validated
    try:
        with open(target_real, encoding="utf-8", errors="replace") as fh:
            raw_content = fh.read()
    except OSError as exc:
        return {"ok": False, "error": f"cannot read file: {exc}", "code": 500}

    had_trailing_nl = raw_content.endswith("\n")
    lines = raw_content.splitlines()
    checkboxes = list(find_checkbox_lines(lines))
    if not checkboxes:
        return {"ok": False, "error": f"no toggleable checkbox found in {norm_rel}", "code": 404}

    cb_by_idx = {idx: (box, rest) for idx, box, rest in checkboxes}
    want_norm = _norm_todo_compare(what) if what else ""
    chosen_idx = None

    if line_no is not None:
        try:
            ln_idx = int(line_no) - 1
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid line number", "code": 400}
        if ln_idx in cb_by_idx:
            _box, rest = cb_by_idx[ln_idx]
            rest_norm = _norm_todo_compare(rest)
            if not want_norm or want_norm in rest_norm or rest_norm in want_norm:
                chosen_idx = ln_idx

    if chosen_idx is None and want_norm:
        exact_hits = [idx for idx, _b, rest in checkboxes if _norm_todo_compare(rest) == want_norm]
        if len(exact_hits) == 1:
            chosen_idx = exact_hits[0]
        elif len(exact_hits) > 1 and line_no is not None:
            target_0 = int(line_no) - 1
            chosen_idx = min(exact_hits, key=lambda i: abs(i - target_0))
        else:
            sub_hits = [
                idx for idx, _b, rest in checkboxes
                if want_norm in _norm_todo_compare(rest) or _norm_todo_compare(rest) in want_norm
            ]
            if len(sub_hits) == 1:
                chosen_idx = sub_hits[0]
            elif len(sub_hits) > 1 and line_no is not None:
                target_0 = int(line_no) - 1
                chosen_idx = min(sub_hits, key=lambda i: abs(i - target_0))

    if chosen_idx is None:
        return {"ok": False, "error": "matching checkbox line not found", "code": 404}

    orig_line = lines[chosen_idx]
    m_cb = _CHECKBOX_LINE_RX.match(orig_line)
    if not m_cb:
        return {"ok": False, "error": "target line is not a Markdown checkbox", "code": 400}

    prefix, cur_box, suffix = m_cb.group(1), m_cb.group(2), m_cb.group(3)
    cur_done = cur_box.lower() == "x"
    if target_done is None:
        new_done = not cur_done
    else:
        new_done = bool(target_done)
    new_box = "x" if new_done else " "
    lines[chosen_idx] = f"{prefix}[{new_box}]{suffix}"

    new_content = "\n".join(lines) + ("\n" if had_trailing_nl or raw_content else "")
    tmp_path = f"{target_real}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(new_content)
        os.replace(tmp_path, target_real)
    except OSError as exc:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return {"ok": False, "error": f"cannot write file: {exc}", "code": 500}

    return {
        "ok": True,
        "rel": norm_rel,
        "line": chosen_idx + 1,
        "done": new_done,
        "status": "done" if new_done else "open",
    }


def build_brain_graph_payload(brain, idx=None, db_path=None, today=None):
    """Compile the interactive `🕸️ Brain Graph` payload from `index.sqlite` + link/staleness analysis."""
    today = today or dt.date.today()
    if idx is None:
        try:
            idx = graph_engine.sync_index(brain, db_path=db_path)
        except Exception:
            idx = {}

    node_rels = sorted(
        r for r in idx
        if r.lower().endswith(".md") and not r.startswith("_atlas/")
    )
    node_set = set(node_rels)
    if not node_rels:
        return {
            "nodes": [],
            "edges": [],
            "stats": {
                "nodes": 0,
                "edges": 0,
                "horizontal_edges": 0,
                "journal_nodes": 0,
                "isolated": 0,
                "stale_verified": 0,
                "broken_links": 0,
                "unlinked_mentions": 0,
            },
        }

    # 1. Read directed edges from SQLite `edges` table
    raw_edges = []
    try:
        conn = graph_engine.open_graph_db(brain, db_path=db_path)
        try:
            for r in conn.execute(
                "SELECT src, dst, kind, anchor, context FROM edges ORDER BY src, dst, kind"
            ):
                s, d = r["src"], r["dst"]
                if s in node_set and d in node_set and s != d:
                    raw_edges.append({
                        "src": s,
                        "dst": d,
                        "kind": r["kind"],
                        "anchor": r["anchor"] or "",
                        "context": (r["context"] or "")[:160],
                    })
        finally:
            conn.close()
    except Exception:
        raw_edges = []

    # 2. Broken links & isolated page analysis
    broken_by_rel = {}
    isolated_set = set()
    try:
        root_dir = os.path.dirname(os.path.abspath(brain))
        rels_map = {r: os.path.join(brain, *r.split("/")) for r in node_rels}
        analysis = graph_engine.validate_and_analyze_brain(
            brain, root=root_dir, rels_map=rels_map, idx=idx, db_path=db_path
        )
        isolated_set = set(analysis.get("isolated") or [])
        for item in list(analysis.get("broken") or []) + list(analysis.get("broken_anchors") or []):
            if " -> " in item:
                src_r, tgt_desc = item.split(" -> ", 1)
                src_r = src_r.strip()
                if src_r in node_set:
                    broken_by_rel.setdefault(src_r, []).append(tgt_desc.strip())
    except Exception:
        pass

    # 3. Unlinked mentions (`scan_retroactive_links`) indexed by target `dst` AND source `src`
    mentions_for_target = {}
    total_unlinked = 0
    try:
        scan = graph_engine.scan_retroactive_links(brain, idx=idx, db_path=db_path)
        for src_rel, hits in (scan.get("mentions") or {}).items():
            if src_rel not in node_set:
                continue
            src_title = (idx.get(src_rel, {}).get("title") or os.path.splitext(os.path.basename(src_rel))[0]).strip()
            for h in hits:
                if h.get("has_body") or h.get("has_related"):
                    continue
                dst_rel = h.get("dst")
                if dst_rel in node_set and dst_rel != src_rel:
                    mentions_for_target.setdefault(dst_rel, []).append({
                        "src": src_rel,
                        "title": src_title,
                        "line": int(h.get("line") or 0),
                        "matched": h.get("matched") or "",
                        "context": (h.get("snippet") or "")[:160],
                    })
                    total_unlinked += 1
    except Exception:
        pass

    # 4. Degree, Outlinks & Backlinks maps
    outlinks_by_rel = {r: [] for r in node_rels}
    backlinks_by_rel = {r: [] for r in node_rels}
    horiz_in = {r: 0 for r in node_rels}
    horiz_out = {r: 0 for r in node_rels}
    total_deg = {r: 0 for r in node_rels}

    title_by_rel = {}
    for r in node_rels:
        e = idx.get(r, {})
        t = (e.get("title") or "").strip()
        if not t:
            stem = os.path.splitext(os.path.basename(r))[0]
            t = os.path.basename(os.path.dirname(r)) if stem.lower() == "readme" and "/" in r else stem
        title_by_rel[r] = t

    for ed in raw_edges:
        s, d, k = ed["src"], ed["dst"], ed["kind"]
        total_deg[s] += 1
        total_deg[d] += 1
        if k != "tree":
            horiz_out[s] += 1
            horiz_in[d] += 1
        outlinks_by_rel[s].append({
            "dst": d,
            "title": title_by_rel.get(d, d),
            "kind": k,
            "anchor": ed["anchor"],
            "context": ed["context"],
        })
        backlinks_by_rel[d].append({
            "src": s,
            "title": title_by_rel.get(s, s),
            "kind": k,
            "anchor": ed["anchor"],
            "context": ed["context"],
        })

    kind_rank = {"related": 0, "body": 1, "journal": 2, "tree": 3}
    nodes = []
    stale_cnt = 0
    broken_cnt = 0

    for r in node_rels:
        e = idx.get(r, {})
        ver_str = str(e.get("verified") or "").strip()
        val_str = str(e.get("valid_until") or "").strip()
        expired_vu = False
        stale_ver = False
        ver_age = None
        if re.match(r"^\d{4}-\d{2}-\d{2}$", val_str):
            try:
                if dt.date.fromisoformat(val_str) < today:
                    expired_vu = True
            except ValueError:
                pass
        if re.match(r"^\d{4}-\d{2}-\d{2}$", ver_str):
            try:
                ver_age = (today - dt.date.fromisoformat(ver_str)).days
                if not expired_vu and ver_age > 365:
                    stale_ver = True
            except ValueError:
                pass

        brk = broken_by_rel.get(r, [])
        if stale_ver:
            stale_cnt += 1
        if brk:
            broken_cnt += len(brk)

        h_deg = horiz_in[r] + horiz_out[r]
        t_deg = total_deg[r]
        is_hub = graph_engine.is_folder_hub(r)
        is_iso = (r in isolated_set) or (t_deg == 0 and r not in ("INDEX.md", "ROUTER.md", "settings.md", "tags.md"))
        spec = round(1.0 / math.log2(max(2, h_deg + 2)), 3)

        outs = sorted(outlinks_by_rel[r], key=lambda x: (kind_rank.get(x["kind"], 9), x["dst"]))
        backs = sorted(backlinks_by_rel[r], key=lambda x: (kind_rank.get(x["kind"], 9), x["src"]))
        um_list = sorted(mentions_for_target.get(r, []), key=lambda x: (x["src"], x["line"]))

        nodes.append({
            "rel": r,
            "title": title_by_rel[r],
            "domain": _node_domain(r),
            "is_hub": is_hub,
            "summary": str(e.get("summary") or "").strip(),
            "tags": list(e.get("tags") or []),
            "verified": ver_str,
            "valid_until": val_str,
            "verified_age_days": ver_age,
            "stale_verified": stale_ver,
            "expired_valid_until": expired_vu,
            "broken_links": brk[:12],
            "in_degree": horiz_in[r],
            "out_degree": horiz_out[r],
            "degree": h_deg,
            "total_degree": t_deg,
            "specificity": spec,
            "isolated": is_iso,
            "outlinks": outs[:40],
            "backlinks": backs[:40],
            "unlinked_mentions": um_list[:25],
        })

    horiz_edge_cnt = sum(1 for ed in raw_edges if ed["kind"] != "tree")
    journal_cnt = sum(1 for n in nodes if n["domain"] == "journal")
    iso_cnt = sum(1 for n in nodes if n["isolated"])

    return {
        "nodes": nodes,
        "edges": raw_edges,
        "stats": {
            "nodes": len(nodes),
            "edges": len(raw_edges),
            "horizontal_edges": horiz_edge_cnt,
            "journal_nodes": journal_cnt,
            "isolated": iso_cnt,
            "stale_verified": stale_cnt,
            "broken_links": broken_cnt,
            "unlinked_mentions": total_unlinked,
        },
    }


CAL_SERVER_SCRIPT = """import functools, hmac, http.server, importlib.util, json, os, subprocess, sys, urllib.parse
sys.dont_write_bytecode = True
try:
    sys.stdout = sys.stderr = open(os.devnull, "w")
except OSError:
    pass

PORT = int(sys.argv[1])
CACHE_DIR = os.path.abspath(sys.argv[2])
BRAIN_DIR = os.path.normpath(os.path.join(CACHE_DIR, "..", ".."))
ATLAS_DIR = os.path.join(BRAIN_DIR, "_atlas")
TOOLS_DIR = os.path.abspath(sys.argv[3]) if len(sys.argv) > 3 and os.path.isdir(sys.argv[3]) else os.path.join(ATLAS_DIR, "tools")
HOOK_PY = os.path.abspath(sys.argv[4]) if len(sys.argv) > 4 and os.path.isfile(sys.argv[4]) else os.path.join(ATLAS_DIR, "hooks", "atlas_hook.py")
ATLAS_PY = os.path.join(TOOLS_DIR, "atlas.py")

if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

def _get_cid():
    p = os.path.join(BRAIN_DIR, "_secrets", "strava.env")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as fh:
                for line in fh:
                    if line.strip().startswith("STRAVA_CLIENT_ID="):
                        v = line.split("=", 1)[1].strip()
                        if v:
                            return v
        except OSError:
            pass
    return "91804"

def _verify_csrf(token):
    cand = str(token or "").strip()
    if len(cand) < 32:
        return False
    p = os.path.join(CACHE_DIR, "cal_csrf.txt")
    try:
        with open(p, encoding="utf-8") as fh:
            expected = fh.read().strip()
    except OSError:
        return False
    return len(expected) >= 32 and hmac.compare_digest(expected, cand)

def _is_loopback_origin(val):
    if not val:
        return True
    v = val.strip().lower()
    for prefix in ("http://127.0.0.1:", "http://127.0.0.1/", "http://localhost:", "http://localhost/", "http://[::1]:", "http://[::1]/"):
        if v.startswith(prefix):
            return True
    return v in ("http://127.0.0.1", "http://localhost", "http://[::1]")

def _rerender_calendar():
    if not os.path.isfile(HOOK_PY):
        return
    try:
        spec = importlib.util.spec_from_file_location("_atlas_srv_hook", HOOK_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.BRAIN = BRAIN_DIR
        mod.STATE = os.path.join(BRAIN_DIR, "_atlas", "state.json")
        mod.CACHE = os.path.join(BRAIN_DIR, "_atlas", "cache", "index.sqlite")
        mod.CAL_HTML = os.path.join(CACHE_DIR, "calendar.html")
        mod.ERR_LOG = os.path.join(CACHE_DIR, "hook_errors.log")
        mod.CONFLICTS_FILE = os.path.join(BRAIN_DIR, "_atlas", "conflicts.json")
        mod.render_calendar_html(start_server=False)
    except Exception:
        pass

class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args, **kwargs):
        pass

    def _send_json(self, status_code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/healthz":
            body = b"atlas-cal-v3"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/strava-connect":
            cid = _get_cid()
            srv_port = self.server.server_address[1]
            redir = urllib.parse.quote(f"http://127.0.0.1:{srv_port}/strava-callback", safe="")
            url = (f"https://www.strava.com/oauth/authorize?client_id={cid}&response_type=code"
                   f"&redirect_uri={redir}&approval_prompt=force&scope=read,activity:read,activity:read_all,profile:read_all")
            self.send_response(302)
            self.send_header("Location", url)
            self.end_headers()
            return
        if parsed.path in ("/strava-callback", "/callback"):
            qs = urllib.parse.parse_qs(parsed.query)
            code = (qs.get("code") or [None])[0]
            ok = False
            if code and os.path.isfile(ATLAS_PY):
                res = subprocess.run([sys.executable, "-I", "-B", ATLAS_PY, "--root", os.path.dirname(BRAIN_DIR),
                                      "coach", "auth-strava", "--code", code, "--execute"],
                                     capture_output=True, text=True, timeout=90)
                ok = res.returncode == 0
            msg = "&#10003; Atlas Coach erfolgreich mit Strava verbunden &amp; synchronisiert!" if ok else "Strava-Verbindung fehlgeschlagen oder abgebrochen."
            col = "#34d399" if ok else "#f43f5e"
            body = (f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>Atlas · Strava Connect</title></head>"
                    f"<body style='font-family:Inter,sans-serif;background:#070a0f;color:{col};display:flex;align-items:center;"
                    f"justify-content:center;height:100vh;margin:0'><div style='background:#0d121b;border:1px solid rgba(255,255,255,0.1);"
                    f"padding:32px 40px;border-radius:14px;text-align:center;max-width:480px'><h2 style='margin:0 0 12px'>{msg}</h2>"
                    f"<p style='color:#94a3b8;font-size:14px;margin:0 0 18px'>Weiterleitung zum Atlas Coach Dashboard…</p>"
                    f"<a href='/calendar.html?view=coach' style='color:#38bdf8;font-weight:600;text-decoration:none'>Jetzt zum Coach Dashboard &rarr;</a>"
                    f"</div><script>sessionStorage.setItem('atlas_cal_filter','coach');setTimeout(()=>location.href='/calendar.html?view=coach',900);</script>"
                    f"</body></html>").encode("utf-8")
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/strava-sync":
            if os.path.isfile(ATLAS_PY):
                subprocess.run([sys.executable, "-I", "-B", ATLAS_PY, "--root", os.path.dirname(BRAIN_DIR),
                                "coach", "sync", "--days", "90", "--execute"],
                               capture_output=True, text=True, timeout=90)
            self.send_response(302)
            self.send_header("Location", "/calendar.html?view=coach")
            self.end_headers()
            return
        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/toggle-todo":
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        client_ip = (self.client_address[0] if self.client_address else "")
        if client_ip not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            self._send_json(403, {"ok": False, "error": "loopback access only"})
            return
        host_hdr = (self.headers.get("Host") or "").strip().lower()
        host_only = host_hdr.rsplit(":", 1)[0] if ":" in host_hdr and not host_hdr.endswith("]") else host_hdr
        if host_only and host_only not in ("127.0.0.1", "localhost", "[::1]"):
            self._send_json(403, {"ok": False, "error": "invalid host header"})
            return
        origin = self.headers.get("Origin")
        if origin is not None and not _is_loopback_origin(origin):
            self._send_json(403, {"ok": False, "error": "cross-origin request blocked"})
            return
        referer = self.headers.get("Referer")
        if origin is None and referer is not None and not _is_loopback_origin(referer):
            self._send_json(403, {"ok": False, "error": "cross-origin referer blocked"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = -1
        if length <= 0 or length > 65536:
            self._send_json(400, {"ok": False, "error": "invalid payload size"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._send_json(400, {"ok": False, "error": "invalid JSON"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"ok": False, "error": "JSON object expected"})
            return
        csrf_hdr = self.headers.get("X-Atlas-CSRF") or payload.get("csrf_token") or ""
        if not _verify_csrf(csrf_hdr):
            self._send_json(403, {"ok": False, "error": "CSRF token invalid or missing"})
            return
        try:
            from atlas_lib import cal_engine as _ce
            res = _ce.toggle_todo_in_brain(
                BRAIN_DIR,
                rel=payload.get("rel", ""),
                line_no=payload.get("line"),
                what=payload.get("what", ""),
                target_done=payload.get("done"),
            )
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})
            return
        if not res.get("ok"):
            self._send_json(int(res.get("code") or 400), {"ok": False, "error": res.get("error", "toggle failed")})
            return
        _rerender_calendar()
        self._send_json(200, {k: v for k, v in res.items() if k != "code"})

handler = functools.partial(_QuietHandler, directory=CACHE_DIR)
srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler)
port_file = os.path.join(CACHE_DIR, "cal_port.txt")
try:
    with open(port_file, "w", encoding="utf-8") as _pf:
        _pf.write(str(srv.server_address[1]))
except OSError:
    pass
srv.serve_forever()
"""


def cal_http_ok(port):
    """Verify the calendar server returns HTTP 200 on `/healthz` with `atlas-cal-v3`."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.25) as resp:
            return resp.status == 200 and CAL_SERVER_HEALTH_TOKEN in resp.read()
    except Exception:
        return False


def write_server_script(cache_dir):
    """Materialize `_cal_server.py` inside `cache_dir` and return its absolute path."""
    os.makedirs(cache_dir, exist_ok=True)
    srv_script = os.path.join(cache_dir, "_cal_server.py")
    with open(srv_script, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(CAL_SERVER_SCRIPT)
    return srv_script


def ensure_calendar_server(cache_dir, start_server=False, probe=False, tools_dir=None, hook_py=None):
    """Ensure a loopback HTTP server serves `cache_dir` (`127.0.0.1:<port>/calendar.html`)."""
    port_file = os.path.join(cache_dir, "cal_port.txt")
    port = 8765
    try:
        with open(port_file, encoding="utf-8") as fh:
            port = int(fh.read().strip())
    except (OSError, ValueError):
        pass
    if not start_server or probe or os.environ.get("ATLAS_NO_CAL_SERVER"):
        return f"http://127.0.0.1:{port}/calendar.html"
    if cal_http_ok(port):
        return f"http://127.0.0.1:{port}/calendar.html"
    if os.name == "nt":
        try:
            kill_ps = (
                "Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%' AND "
                "(CommandLine LIKE '%http.server%' OR CommandLine LIKE '%_cal_server.py%')\" "
                "| Invoke-CimMethod -MethodName Terminate | Out-Null"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", kill_ps],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=4,
                creationflags=0x08000000,
            )
            time.sleep(0.15)
        except Exception:
            pass
    else:
        try:
            subprocess.run(
                ["pkill", "-f", "_cal_server.py"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            time.sleep(0.1)
        except Exception:
            pass
    for cand in range(8765, 8795):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", cand))
                port = cand
                break
        except OSError:
            continue
    try:
        srv_script = write_server_script(cache_dir)
        with open(port_file, "w", encoding="utf-8") as fh:
            fh.write(str(port))
        py_exe = sys.executable
        if os.name == "nt":
            pyw = os.path.join(os.path.dirname(py_exe), "pythonw.exe")
            if os.path.isfile(pyw):
                py_exe = pyw
        here = os.path.dirname(os.path.abspath(__file__))
        eff_tools = tools_dir or os.path.normpath(os.path.join(here, ".."))
        eff_hook = hook_py or os.path.normpath(os.path.join(here, "..", "..", "hooks", "atlas_hook.py"))
        server_cmd = [py_exe, "-I", "-B", srv_script, str(port), cache_dir, eff_tools, eff_hook]
        if os.name == "nt":
            cmdline = subprocess.list2cmdline(server_cmd).replace("'", "''")
            wdir = cache_dir.replace("'", "''")
            ps = (
                f"$si = New-CimInstance -ClientOnly -ClassName Win32_ProcessStartup -Property @{{ShowWindow=[uint16]0}}; "
                f"Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
                f"-Arguments @{{CommandLine='{cmdline}'; CurrentDirectory='{wdir}'; ProcessStartupInformation=$si}} | Out-Null"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                creationflags=0x08000000,
            )
        else:
            subprocess.Popen(
                server_cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        for _ in range(20):
            if cal_http_ok(port):
                break
            time.sleep(0.1)
    except Exception:
        pass
    return f"http://127.0.0.1:{port}/calendar.html"
