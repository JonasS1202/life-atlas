"""Atlas Calendar & To-Do Markdown Engine + Headless Loopback OAuth Callback Server (Python 3 stdlib only).

Handles atomic, lock-serialized Markdown task mutations inside `<BRAIN>/` (`toggle`, `reschedule`,
`unschedule`, `add`, opt-in `rollover_today`), zero-token free-day suggestions for smart overdue
triage, and a minimal headless `127.0.0.1` OAuth loopback callback server (`atlas-oauth-v8`) for
1-click connectors (Google Calendar, Strava, Hammerhead Karoo, Google Health).
"""
import contextlib
import datetime as dt
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request

sys.dont_write_bytecode = True

OAUTH_SERVER_HEALTH_TOKEN = b"atlas-oauth-v8"
# Backward-compatible alias for tests/scripts importing the token name
CAL_SERVER_HEALTH_TOKEN = OAUTH_SERVER_HEALTH_TOKEN

_THREAD_WRITE_LOCK = threading.RLock()
_CHECKBOX_LINE_RX = re.compile(r"^(\s*(?:[-*+]|\d+\.)?\s*)\[([ xX-])\](\s+.*)$")
_FENCE_RX = re.compile(r"^\s*(`{3,}|~{3,})")
_SYNC_MARKER_RX = re.compile(r"\s*<!--\s*g:[A-Za-z0-9_-]{4,24}\s*-->\s*$")
_ORIG_DATE_RX = re.compile(r"\((?:orig|seit)\s+\d{4}-\d{2}-\d{2}\)", re.I)


def _norm_todo_compare(text):
    """Normalize Markdown task text for resilient matching when line numbers shift."""
    s = str(text or "")
    s = _SYNC_MARKER_RX.sub("", s)
    s = re.sub(r"(?:^|\s)#(?:local|sync)(?=\s|$)", " ", s, flags=re.I)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = s.replace("**", "").replace("`", "").strip()
    s = re.sub(r"^\d{4}-\d{2}-\d{2}(?:\.\.\d{4}-\d{2}-\d{2})?\s+", "", s)
    s = re.sub(r"^\d{1,2}:\d{2}(?:-\d{1,2}:\d{2})?\s+", "", s)
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
        return None, "only Markdown (.md) files can be modified"
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


@contextlib.contextmanager
def brain_write_lock(brain):
    """Serialize read-modify-write calendar/to-do mutations across threads and processes (stdlib only, fail-safe)."""
    lock_fh = None
    locked_mode = None
    with _THREAD_WRITE_LOCK:
        try:
            cache_dir = os.path.join(brain, "_atlas", "cache")
            os.makedirs(cache_dir, exist_ok=True)
            lock_path = os.path.join(cache_dir, ".todo.lock")
            lock_fh = open(lock_path, "a+b")
            if os.name == "nt":
                import msvcrt
                lock_fh.seek(0)
                if os.path.getsize(lock_path) == 0:
                    lock_fh.write(b"0")
                    lock_fh.flush()
                    lock_fh.seek(0)
                for _ in range(40):
                    try:
                        msvcrt.locking(lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
                        locked_mode = "nt"
                        break
                    except OSError:
                        time.sleep(0.05)
            else:
                import fcntl
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
                locked_mode = "posix"
        except Exception:
            if lock_fh is not None:
                try:
                    lock_fh.close()
                except OSError:
                    pass
                lock_fh = None
        try:
            yield
        finally:
            if lock_fh is not None:
                try:
                    if locked_mode == "nt":
                        import msvcrt
                        lock_fh.seek(0)
                        msvcrt.locking(lock_fh.fileno(), msvcrt.LK_UNLCK, 1)
                    elif locked_mode == "posix":
                        import fcntl
                        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    lock_fh.close()
                except OSError:
                    pass


def _atomic_write_text(target_real, content):
    tmp_path = f"{target_real}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp_path, target_real)
        return None
    except OSError as exc:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return f"cannot write file: {exc}"


def parse_target_date(spec, today=None):
    """Parse `YYYY-MM-DD`, `+Nd`, `+Nw`, or natural aliases (`today`/`heute`, `tomorrow`/`morgen`) into `YYYY-MM-DD`."""
    today = today or dt.date.today()
    raw = str(spec or "").strip().lower()
    if not raw:
        raise ValueError("missing target date (expected YYYY-MM-DD or +Nd)")
    if raw in ("today", "heute", "0d", "+0d"):
        return today.isoformat()
    if raw in ("tomorrow", "morgen"):
        return (today + dt.timedelta(days=1)).isoformat()
    if raw in ("übermorgen", "uebermorgen", "day-after-tomorrow"):
        return (today + dt.timedelta(days=2)).isoformat()
    m_rel = re.match(r"^\+?(\d+)\s*([dwm]?)$", raw)
    if m_rel:
        n = int(m_rel.group(1))
        unit = m_rel.group(2) or "d"
        days = n * (7 if unit == "w" else (30 if unit == "m" else 1))
        return (today + dt.timedelta(days=days)).isoformat()
    try:
        return dt.date.fromisoformat(raw[:10]).isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid target date '{spec}' (expected YYYY-MM-DD, +Nd, +Nw, today, tomorrow)") from exc


def _find_frontmatter_due_lines(lines):
    """Yield `(idx_0based, indent_dash_prefix, inner_value)` for `- ...` items under `due:` in YAML frontmatter."""
    if not lines or lines[0].strip() != "---":
        return
    in_due = False
    for idx in range(1, len(lines)):
        line = lines[idx]
        stripped = line.strip()
        if stripped in ("---", "..."):
            break
        if not stripped or stripped.startswith("#"):
            continue
        m_key = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if m_key and not line.startswith((" ", "\t", "-")):
            in_due = m_key.group(1).lower() == "due"
            continue
        if in_due:
            m_item = re.match(r"^(\s*-\s+)(.+?)\s*$", line)
            if m_item:
                val = m_item.group(2).strip().strip("\"'")
                yield idx, m_item.group(1), val


def _match_line_index(candidates, line_no=None, want_norm=""):
    """Pick the best 0-based line index from `[(idx, norm_text), ...]` using `line_no` and/or `want_norm`."""
    by_idx = dict(candidates)
    chosen_idx = None
    if line_no is not None and int(line_no) > 0:
        ln_idx = int(line_no) - 1
        if ln_idx in by_idx:
            rest_norm = by_idx[ln_idx]
            if not want_norm or want_norm in rest_norm or rest_norm in want_norm:
                chosen_idx = ln_idx
    if chosen_idx is None and want_norm:
        exact_hits = [idx for idx, r_norm in candidates if r_norm == want_norm]
        if len(exact_hits) == 1:
            chosen_idx = exact_hits[0]
        elif len(exact_hits) > 1 and line_no is not None and int(line_no) > 0:
            target_0 = int(line_no) - 1
            chosen_idx = min(exact_hits, key=lambda i: abs(i - target_0))
        else:
            sub_hits = [idx for idx, r_norm in candidates if want_norm in r_norm or r_norm in want_norm]
            if len(sub_hits) == 1:
                chosen_idx = sub_hits[0]
            elif len(sub_hits) > 1 and line_no is not None and int(line_no) > 0:
                target_0 = int(line_no) - 1
                chosen_idx = min(sub_hits, key=lambda i: abs(i - target_0))
    return chosen_idx


def _insert_into_timeline_lines(lines, entry_line, target_date_iso):
    """Insert `entry_line` chronologically under `## Timeline` in `calendar/README.md` lines."""
    tl_idx = None
    next_sec_idx = len(lines)
    for i, ln in enumerate(lines):
        if ln.startswith("## ") and "timeline" in ln.lower():
            tl_idx = i
            for j in range(i + 1, len(lines)):
                if lines[j].startswith("## "):
                    next_sec_idx = j
                    break
            break
    if tl_idx is None:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.extend(["## Timeline", "", entry_line])
        return len(lines)

    insert_at = next_sec_idx
    last_item_idx = None
    for k in range(tl_idx + 1, next_sec_idx):
        m_d = re.match(r"^\s*-\s*(?:\[[ xX-]\]\s*)?(\d{4}-\d{2}-\d{2})\b", lines[k])
        if m_d:
            last_item_idx = k
            if m_d.group(1) > target_date_iso:
                insert_at = k
                break
    else:
        if last_item_idx is not None:
            insert_at = last_item_idx + 1
        else:
            insert_at = next_sec_idx
            while insert_at > tl_idx + 1 and not lines[insert_at - 1].strip():
                insert_at -= 1
    lines.insert(insert_at, entry_line)
    return insert_at + 1


def _insert_into_open_todos_lines(lines, entry_line):
    """Insert `entry_line` under `## Open To-Dos` (creating the section if absent) and return 1-based line number."""
    todo_sec = None
    insert_at = len(lines)
    for i, line in enumerate(lines):
        if line.startswith("## ") and any(w in line.lower() for w in ("to-do", "todo", "aufgaben", "backlog")):
            todo_sec = i
            insert_at = len(lines)
            for j in range(i + 1, len(lines)):
                if lines[j].startswith("## "):
                    insert_at = j
                    break
            break
    if todo_sec is None:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.extend(["## Open To-Dos", "", entry_line])
        return len(lines)
    while insert_at > todo_sec + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    if insert_at == todo_sec + 1:
        lines.insert(insert_at, "")
        insert_at += 1
    lines.insert(insert_at, entry_line)
    return insert_at + 1


def _ensure_calendar_readme_lines(brain):
    """Load `brain/calendar/README.md` lines, creating a standard skeleton if missing."""
    cal_dir = os.path.join(brain, "calendar")
    os.makedirs(cal_dir, exist_ok=True)
    cal_path = os.path.join(cal_dir, "README.md")
    if os.path.isfile(cal_path):
        with open(cal_path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
        return cal_path, raw.splitlines(), raw.endswith("\n")
    default_lines = [
        "---",
        "summary: Central calendar and to-do hub: timeline and open to-dos",
        "tags: [atlas, calendar]",
        "---",
        "# Calendar & To-Dos",
        "",
        "## Timeline",
        "",
        "## Open To-Dos",
        "",
    ]
    return cal_path, default_lines, True


def toggle_todo_in_brain(brain, rel, line_no=None, what="", target_done=None, target_status=None):
    """Safely toggle or set `- [ ]` / `- [x]` / `- [-]` on a task/workout or `due:` frontmatter line in `<BRAIN>/<rel>`.

    Enforces strict path confinement, ignores fenced code blocks, verifies `line_no` / `what`,
    serializes concurrent writes via `brain_write_lock`, and writes atomically with LF line endings.
    """
    with brain_write_lock(brain):
        return _toggle_todo_in_brain_unlocked(
            brain, rel, line_no=line_no, what=what, target_done=target_done, target_status=target_status
        )


def _toggle_todo_in_brain_unlocked(brain, rel, line_no=None, what="", target_done=None, target_status=None):
    validated, err = _validate_brain_rel_path(brain, rel)
    if err:
        return {"ok": False, "error": err, "code": 400}
    norm_rel, target_real = validated
    if line_no is not None:
        try:
            int(line_no)
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid line number", "code": 400}
    try:
        with open(target_real, encoding="utf-8", errors="replace") as fh:
            raw_content = fh.read()
    except OSError as exc:
        return {"ok": False, "error": f"cannot read file: {exc}", "code": 500}

    had_trailing_nl = raw_content.endswith("\n")
    lines = raw_content.splitlines()
    want_norm = _norm_todo_compare(what) if what else ""

    checkboxes = list(find_checkbox_lines(lines))
    cb_candidates = [(idx, _norm_todo_compare(rest)) for idx, _b, rest in checkboxes]
    chosen_idx = None if (line_no is not None and int(line_no) == 0) else _match_line_index(cb_candidates, line_no=line_no, want_norm=want_norm)

    if chosen_idx is not None:
        orig_line = lines[chosen_idx]
        m_cb = _CHECKBOX_LINE_RX.match(orig_line)
        if not m_cb:
            return {"ok": False, "error": "target line is not a Markdown checkbox", "code": 400}
        prefix, cur_box, suffix = m_cb.group(1), m_cb.group(2), m_cb.group(3)
        if target_status in ("done", "skipped", "open"):
            eff_status = target_status
        elif target_done is None:
            eff_status = "open" if cur_box.lower() == "x" else "done"
        else:
            eff_status = "done" if bool(target_done) else "open"
        new_box = {"done": "x", "skipped": "-", "open": " "}[eff_status]
        lines[chosen_idx] = f"{prefix}[{new_box}]{suffix}"
        new_content = "\n".join(lines) + ("\n" if had_trailing_nl or raw_content else "")
        w_err = _atomic_write_text(target_real, new_content)
        if w_err:
            return {"ok": False, "error": w_err, "code": 500}
        return {
            "ok": True,
            "rel": norm_rel,
            "line": chosen_idx + 1,
            "done": eff_status == "done",
            "status": eff_status,
        }

    # Fallback for `due:` frontmatter items (`line_no` is None or 0)
    if line_no in (None, 0):
        fm_items = list(_find_frontmatter_due_lines(lines))
        fm_candidates = [(idx, _norm_todo_compare(re.sub(r"^\[[ xX-]\]\s*", "", val))) for idx, _p, val in fm_items]
        fm_idx = _match_line_index(fm_candidates, line_no=None, want_norm=want_norm)
        if fm_idx is not None:
            fm_by_idx = {idx: (pref, val) for idx, pref, val in fm_items}
            pref, val = fm_by_idx[fm_idx]
            m_box = re.match(r"^\[([ xX-])\]\s*(.+)$", val)
            cur_b = m_box.group(1).lower() if m_box else " "
            clean_val = m_box.group(2).strip() if m_box else val
            if target_status in ("done", "skipped", "open"):
                eff_status = target_status
            elif target_done is None:
                eff_status = "open" if cur_b == "x" else "done"
            else:
                eff_status = "done" if bool(target_done) else "open"
            if eff_status == "open":
                lines[fm_idx] = f"{pref}{clean_val}"
            else:
                box_ch = "x" if eff_status == "done" else "-"
                safe_val = clean_val.replace('"', "'")
                lines[fm_idx] = f'{pref}"[{box_ch}] {safe_val}"'
            new_content = "\n".join(lines) + ("\n" if had_trailing_nl or raw_content else "")
            w_err = _atomic_write_text(target_real, new_content)
            if w_err:
                return {"ok": False, "error": w_err, "code": 500}
            return {
                "ok": True,
                "rel": norm_rel,
                "line": fm_idx + 1,
                "done": eff_status == "done",
                "status": eff_status,
            }

    if not checkboxes:
        return {"ok": False, "error": f"no toggleable checkbox found in {norm_rel}", "code": 404}
    return {"ok": False, "error": "matching checkbox line not found", "code": 404}


def reschedule_task_in_brain(brain, rel, target_date_spec, line_no=None, what="", today=None):
    """Move/reschedule a dated `[todo]`/`due:` item or promote an undated `Open To-Do` onto `target_date_spec`."""
    with brain_write_lock(brain):
        return _reschedule_task_in_brain_unlocked(
            brain, rel, target_date_spec, line_no=line_no, what=what, today=today
        )


def _reschedule_task_in_brain_unlocked(brain, rel, target_date_spec, line_no=None, what="", today=None):
    try:
        target_iso = parse_target_date(target_date_spec, today=today)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "code": 400}

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
    want_norm = _norm_todo_compare(what) if what else ""

    checkboxes = list(find_checkbox_lines(lines))
    cb_candidates = [(idx, _norm_todo_compare(rest)) for idx, _b, rest in checkboxes]
    chosen_idx = None if (line_no is not None and int(line_no) == 0) else _match_line_index(cb_candidates, line_no=line_no, want_norm=want_norm)

    if chosen_idx is not None:
        m_cb = _CHECKBOX_LINE_RX.match(lines[chosen_idx])
        prefix, rest = m_cb.group(1), m_cb.group(3).strip()
        m_dated = re.match(r"^(\d{4}-\d{2}-\d{2})(?:\.\.(\d{4}-\d{2}-\d{2}))?\s+(.*)$", rest)
        if m_dated:
            old_s = dt.date.fromisoformat(m_dated.group(1))
            new_s = dt.date.fromisoformat(target_iso)
            tail = m_dated.group(3).strip()
            if m_dated.group(2):
                old_e = dt.date.fromisoformat(m_dated.group(2))
                new_e = (new_s + (old_e - old_s)).isoformat()
                date_tok = f"{target_iso}..{new_e}"
            else:
                date_tok = target_iso
            new_line = f"{prefix}[ ] {date_tok} {tail}"
            if norm_rel == "calendar/README.md":
                lines.pop(chosen_idx)
                new_ln = _insert_into_timeline_lines(lines, new_line, target_iso)
            else:
                lines[chosen_idx] = new_line
                new_ln = chosen_idx + 1
            new_content = "\n".join(lines) + ("\n" if had_trailing_nl or raw_content else "")
            w_err = _atomic_write_text(target_real, new_content)
            if w_err:
                return {"ok": False, "error": w_err, "code": 500}
            return {"ok": True, "rel": norm_rel, "line": new_ln, "date": target_iso, "promoted": False}

        # Undated checkbox -> strip `[high|med|low]` and schedule onto `target_iso`
        clean_task = re.sub(r"^\[(?:high|med|low)\]\s*", "", rest, flags=re.I).strip()
        if not re.match(r"^\[[a-zA-Z]+(?::[a-zA-Z0-9_-]+)?\]\s+", clean_task):
            dated_tail = f"[todo] {clean_task}"
        else:
            dated_tail = clean_task

        if norm_rel == "calendar/README.md":
            lines.pop(chosen_idx)
            entry_line = f"- [ ] {target_iso} {dated_tail}"
            new_ln = _insert_into_timeline_lines(lines, entry_line, target_iso)
            new_content = "\n".join(lines) + ("\n" if had_trailing_nl or raw_content else "")
            w_err = _atomic_write_text(target_real, new_content)
            if w_err:
                return {"ok": False, "error": w_err, "code": 500}
            return {"ok": True, "rel": norm_rel, "line": new_ln, "date": target_iso, "promoted": True}

        # Undated checkbox in another brain page -> date it in-place AND add to `calendar/README.md` Timeline
        lines[chosen_idx] = f"{prefix}[ ] {target_iso} {dated_tail}"
        new_content = "\n".join(lines) + ("\n" if had_trailing_nl or raw_content else "")
        w_err = _atomic_write_text(target_real, new_content)
        if w_err:
            return {"ok": False, "error": w_err, "code": 500}
        cal_path, cal_lines, cal_nl = _ensure_calendar_readme_lines(brain)
        ref_suffix = "" if norm_rel in dated_tail else f" ({norm_rel})"
        cal_entry = f"- [ ] {target_iso} {dated_tail}{ref_suffix}"
        if not any(_norm_todo_compare(clean_task) in _norm_todo_compare(ln) and target_iso in ln for ln in cal_lines):
            cal_ln = _insert_into_timeline_lines(cal_lines, cal_entry, target_iso)
            _atomic_write_text(cal_path, "\n".join(cal_lines) + ("\n" if cal_nl else "\n"))
        else:
            cal_ln = chosen_idx + 1
        return {"ok": True, "rel": "calendar/README.md", "line": cal_ln, "source_rel": norm_rel, "date": target_iso, "promoted": True}

    # Frontmatter `due:` fallback
    if line_no in (None, 0):
        fm_items = list(_find_frontmatter_due_lines(lines))
        fm_candidates = [(idx, _norm_todo_compare(re.sub(r"^\[[ xX-]\]\s*", "", val))) for idx, _p, val in fm_items]
        fm_idx = _match_line_index(fm_candidates, line_no=None, want_norm=want_norm)
        if fm_idx is not None:
            fm_by_idx = {idx: (pref, val) for idx, pref, val in fm_items}
            pref, val = fm_by_idx[fm_idx]
            unboxed = re.sub(r"^\[[ xX-]\]\s*", "", val).strip()
            m_d = re.match(r"^(\d{4}-\d{2}-\d{2})(?:\.\.(\d{4}-\d{2}-\d{2}))?\s+(.*)$", unboxed)
            if not m_d:
                return {"ok": False, "error": f"frontmatter due entry has no movable YYYY-MM-DD date: {val}", "code": 400}
            old_s = dt.date.fromisoformat(m_d.group(1))
            new_s = dt.date.fromisoformat(target_iso)
            if m_d.group(2):
                old_e = dt.date.fromisoformat(m_d.group(2))
                date_tok = f"{target_iso}..{(new_s + (old_e - old_s)).isoformat()}"
            else:
                date_tok = target_iso
            lines[fm_idx] = f"{pref}{date_tok} {m_d.group(3).strip()}"
            new_content = "\n".join(lines) + ("\n" if had_trailing_nl or raw_content else "")
            w_err = _atomic_write_text(target_real, new_content)
            if w_err:
                return {"ok": False, "error": w_err, "code": 500}
            return {"ok": True, "rel": norm_rel, "line": fm_idx + 1, "date": target_iso, "promoted": False}

    return {"ok": False, "error": "matching task not found", "code": 404}


def unschedule_todo_in_brain(brain, rel, line_no=None, what="", prio="med"):
    """Move a dated `[todo]` out of `## Timeline` into the undated `## Open To-Dos` backlog.

    Refuses hard `[deadline]` / frontmatter `due:` items so legal/contract deadlines are never stripped
    of their dates by accident. Strips any `<!-- g:... -->` sync tag so external calendars clean up on push.
    """
    with brain_write_lock(brain):
        return _unschedule_todo_in_brain_unlocked(brain, rel, line_no=line_no, what=what, prio=prio)


def _unschedule_todo_in_brain_unlocked(brain, rel, line_no=None, what="", prio="med"):
    validated, err = _validate_brain_rel_path(brain, rel)
    if err:
        return {"ok": False, "error": err, "code": 400}
    norm_rel, target_real = validated
    if line_no is not None and int(line_no) == 0:
        return {"ok": False, "error": "frontmatter `due:` deadlines cannot be unscheduled — mark done, skip, or move", "code": 400}
    try:
        with open(target_real, encoding="utf-8", errors="replace") as fh:
            raw_content = fh.read()
    except OSError as exc:
        return {"ok": False, "error": f"cannot read file: {exc}", "code": 500}

    had_trailing_nl = raw_content.endswith("\n")
    lines = raw_content.splitlines()
    want_norm = _norm_todo_compare(what) if what else ""
    checkboxes = list(find_checkbox_lines(lines))
    cb_candidates = [(idx, _norm_todo_compare(rest)) for idx, _b, rest in checkboxes]
    chosen_idx = _match_line_index(cb_candidates, line_no=line_no, want_norm=want_norm)
    if chosen_idx is None:
        return {"ok": False, "error": "matching dated todo not found", "code": 404}

    m_cb = _CHECKBOX_LINE_RX.match(lines[chosen_idx])
    prefix, rest = m_cb.group(1), m_cb.group(3).strip()
    m_dated = re.match(r"^(\d{4}-\d{2}-\d{2})(?:\.\.\d{4}-\d{2}-\d{2})?\s+(?:\d{1,2}:\d{2}(?:-\d{1,2}:\d{2})?\s+)?(.*)$", rest)
    if not m_dated:
        return {"ok": False, "error": "task is already undated", "code": 400}
    tail = m_dated.group(2).strip()
    m_kind = re.match(r"^\[([a-zA-Z]+)(?::[a-zA-Z0-9_-]+)?\]\s*(.*)$", tail)
    if m_kind:
        k_lower = m_kind.group(1).lower()
        if k_lower != "todo":
            return {
                "ok": False,
                "error": f"[{k_lower}] items cannot be unscheduled into backlog (only [todo] items)",
                "code": 400,
            }
        clean_task = m_kind.group(2).strip()
    else:
        clean_task = tail
    clean_task = _SYNC_MARKER_RX.sub("", clean_task).strip()
    clean_task = re.sub(r"(?:^|\s)#(?:local|sync)(?=\s|$)", " ", clean_task, flags=re.I).strip()
    clean_task = _ORIG_DATE_RX.sub("", clean_task).strip()
    clean_task = re.sub(r"\s+", " ", clean_task).strip()
    eff_prio = prio.lower() if str(prio or "").lower() in ("high", "med", "low") else "med"
    entry = f"- [ ] [{eff_prio}] {clean_task}"

    if norm_rel == "calendar/README.md":
        lines.pop(chosen_idx)
        new_ln = _insert_into_open_todos_lines(lines, entry)
        new_content = "\n".join(lines) + ("\n" if had_trailing_nl or raw_content else "")
        w_err = _atomic_write_text(target_real, new_content)
        if w_err:
            return {"ok": False, "error": w_err, "code": 500}
        return {"ok": True, "rel": "calendar/README.md", "line": new_ln, "entry": entry, "text": clean_task}

    if norm_rel.startswith("calendar/"):
        lines.pop(chosen_idx)
        new_content = "\n".join(lines) + ("\n" if had_trailing_nl or raw_content else "")
        w_err = _atomic_write_text(target_real, new_content)
        if w_err:
            return {"ok": False, "error": w_err, "code": 500}
        cal_path, cal_lines, cal_nl = _ensure_calendar_readme_lines(brain)
        new_ln = _insert_into_open_todos_lines(cal_lines, entry)
        w_err2 = _atomic_write_text(cal_path, "\n".join(cal_lines) + ("\n" if cal_nl else "\n"))
        if w_err2:
            return {"ok": False, "error": w_err2, "code": 500}
        return {"ok": True, "rel": "calendar/README.md", "line": new_ln, "entry": entry, "text": clean_task}

    # Checklist file outside calendar/: strip date in-place
    lines[chosen_idx] = f"{prefix}[ ] [{eff_prio}] {clean_task}"
    new_content = "\n".join(lines) + ("\n" if had_trailing_nl or raw_content else "")
    w_err = _atomic_write_text(target_real, new_content)
    if w_err:
        return {"ok": False, "error": w_err, "code": 500}
    return {"ok": True, "rel": norm_rel, "line": chosen_idx + 1, "entry": lines[chosen_idx], "text": clean_task}


def rollover_overdue_todos_in_brain(brain, today=None):
    """Opt-in (`overdue_todo_policy: rollover_today`): move overdue open `[todo]` lines in `calendar/README.md` to today.

    Strictly skips `[deadline]`, `[event]`, `[blocked]`, `[goal]`, `[workout]`, and `[birthday]`.
    Appends `(orig YYYY-MM-DD)` so the original due date / procrastination age stays visible.
    """
    today = today or dt.date.today()
    today_iso = today.isoformat()
    with brain_write_lock(brain):
        cal_path = os.path.join(brain, "calendar", "README.md")
        if not os.path.isfile(cal_path):
            return {"ok": True, "moved": []}
        try:
            with open(cal_path, encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
        except OSError as exc:
            return {"ok": False, "error": str(exc), "moved": []}
        had_nl = raw.endswith("\n")
        lines = raw.splitlines()
        to_move = []
        for idx, box, rest in find_checkbox_lines(lines):
            if box != " ":
                continue
            s = rest.strip()
            m = re.match(r"^(\d{4}-\d{2}-\d{2})\s+(?:\d{1,2}:\d{2}(?:-\d{1,2}:\d{2})?\s+)?\[todo\]\s+(.+)$", s, re.I)
            if not m:
                continue
            orig_iso = m.group(1)
            if orig_iso >= today_iso:
                continue
            body = m.group(2).strip()
            sync_m = _SYNC_MARKER_RX.search(body)
            sync_tail = f" {sync_m.group(0).strip()}" if sync_m else ""
            body_clean = _SYNC_MARKER_RX.sub("", body).strip()
            if not _ORIG_DATE_RX.search(body_clean):
                body_clean = f"{body_clean} (orig {orig_iso})"
            new_line = f"- [ ] {today_iso} [todo] {body_clean}{sync_tail}"
            to_move.append((idx, orig_iso, body_clean, new_line))
        if not to_move:
            return {"ok": True, "moved": []}
        for idx, _orig, _body, _new in reversed(to_move):
            lines.pop(idx)
        moved = []
        for _idx, orig_iso, body_clean, new_line in to_move:
            _insert_into_timeline_lines(lines, new_line, today_iso)
            moved.append({"orig": orig_iso, "date": today_iso, "what": body_clean})
        w_err = _atomic_write_text(cal_path, "\n".join(lines) + ("\n" if had_nl or raw else ""))
        if w_err:
            return {"ok": False, "error": w_err, "moved": []}
        return {"ok": True, "moved": moved}


def suggest_free_days(cal_items, today=None, horizon_days=7, count=2):
    """Zero-token helper: pick up to `count` lightest upcoming days (preferring 1 weekday + 1 weekend day).

    Avoids `[blocked]` days and ranks remaining days by fewest timed events/workouts.
    Returns list of dicts: `[{"date": "YYYY-MM-DD", "weekday": "Thu", "busy_count": 0, "is_weekend": False}]`.
    """
    today = today or dt.date.today()
    wd_names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    candidates = []
    for offset in range(1, max(2, int(horizon_days or 7)) + 1):
        d = today + dt.timedelta(days=offset)
        blocked = False
        busy_score = 0
        for it in (cal_items or []):
            if it.get("status") not in ("open", None, ""):
                continue
            s_d = it.get("start")
            e_d = it.get("end") or s_d
            if isinstance(s_d, str):
                try:
                    s_d = dt.date.fromisoformat(s_d[:10])
                except ValueError:
                    continue
            if isinstance(e_d, str):
                try:
                    e_d = dt.date.fromisoformat(e_d[:10])
                except ValueError:
                    e_d = s_d
            if not (s_d <= d <= e_d):
                continue
            k = str(it.get("kind") or "")
            if k == "blocked":
                blocked = True
                break
            if k in ("event", "workout") and it.get("time"):
                busy_score += 2
            elif k in ("event", "workout", "goal"):
                busy_score += 1
        if blocked:
            continue
        candidates.append({
            "date": d.isoformat(),
            "weekday": wd_names[d.weekday()],
            "busy_count": busy_score,
            "is_weekend": d.weekday() >= 5,
            "offset": offset,
        })
    if not candidates:
        return []
    weekdays = sorted((c for c in candidates if not c["is_weekend"]), key=lambda c: (c["busy_count"], c["offset"]))
    weekends = sorted((c for c in candidates if c["is_weekend"]), key=lambda c: (c["busy_count"], c["offset"]))
    picked = []
    if weekdays:
        picked.append(weekdays[0])
    if weekends and len(picked) < count:
        picked.append(weekends[0])
    if len(picked) < count:
        used = {p["date"] for p in picked}
        for c in sorted(candidates, key=lambda c: (c["busy_count"], c["offset"])):
            if c["date"] not in used:
                picked.append(c)
                used.add(c["date"])
                if len(picked) >= count:
                    break
    return sorted(picked, key=lambda c: c["date"])


def add_todo_in_brain(brain, text, target_date_spec=None, prio="med", kind="todo", time_spec=None, today=None, layer_file=None):
    """Add a dated `[kind]` item under `## Timeline` or an undated `[prio]` task under `## Open To-Dos` in `calendar/<layer_file>`."""
    with brain_write_lock(brain):
        return _add_todo_in_brain_unlocked(
            brain, text, target_date_spec=target_date_spec, prio=prio, kind=kind,
            time_spec=time_spec, today=today, layer_file=layer_file,
        )


def _add_todo_in_brain_unlocked(brain, text, target_date_spec=None, prio="med", kind="todo", time_spec=None, today=None, layer_file=None):
    clean = str(text or "").strip()
    if len(clean) < 2:
        return {"ok": False, "error": "task text is empty", "code": 400}
    eff_kind = str(kind or "todo").strip().lower()
    if not re.match(r"^[a-z]+(?::[a-zA-Z0-9_-]+)?$", eff_kind):
        return {"ok": False, "error": f"invalid item kind: {kind!r}", "code": 400}
    t_str = ""
    if time_spec:
        ts = str(time_spec).strip()
        if not re.match(r"^\d{1,2}:\d{2}(?:-\d{1,2}:\d{2})?$", ts):
            return {"ok": False, "error": f"invalid --time {time_spec!r} (expected HH:MM or HH:MM-HH:MM)", "code": 400}
        t_str = f" {ts}"
    target_rel = "calendar/README.md"
    if layer_file and str(layer_file).strip():
        lf = os.path.basename(str(layer_file).strip())
        if not lf.lower().endswith(".md"):
            lf += ".md"
        target_rel = f"calendar/{lf}"
    if target_rel == "calendar/README.md":
        cal_path, lines, had_nl = _ensure_calendar_readme_lines(brain)
    else:
        cal_dir = os.path.join(brain, "calendar")
        os.makedirs(cal_dir, exist_ok=True)
        cal_path = os.path.join(cal_dir, os.path.basename(target_rel))
        if not os.path.isfile(cal_path):
            lbl = os.path.splitext(os.path.basename(target_rel))[0]
            base = f"---\nsummary: Calendar layer {lbl}\ntags: [atlas, calendar]\ncal_layer: {lbl}\n---\n# Calendar: {lbl}\n\n## Timeline\n"
            _atomic_write_text(cal_path, base)
        with open(cal_path, encoding="utf-8", errors="replace") as fh:
            raw_c = fh.read()
        had_nl = raw_c.endswith("\n") or not raw_c
        lines = raw_c.splitlines()
    if target_date_spec:
        try:
            target_iso = parse_target_date(target_date_spec, today=today)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "code": 400}
        entry = f"- [ ] {target_iso}{t_str} [{eff_kind}] {clean}"
        ln = _insert_into_timeline_lines(lines, entry, target_iso)
        w_err = _atomic_write_text(cal_path, "\n".join(lines) + ("\n" if had_nl else "\n"))
        if w_err:
            return {"ok": False, "error": w_err, "code": 500}
        return {"ok": True, "rel": target_rel, "line": ln, "date": target_iso, "entry": entry}

    eff_prio = prio.lower() if str(prio or "").lower() in ("high", "med", "low") else "med"
    entry = f"- [ ] [{eff_prio}] {clean}"
    ln = _insert_into_open_todos_lines(lines, entry)
    w_err = _atomic_write_text(cal_path, "\n".join(lines) + ("\n" if had_nl else "\n"))
    if w_err:
        return {"ok": False, "error": w_err, "code": 500}
    return {"ok": True, "rel": "calendar/README.md", "line": ln, "prio": eff_prio, "entry": entry}


# ---------------------------------------------------------------------------
# Headless Loopback OAuth Callback Server (`atlas-oauth-v8`)
# Zero HTML dashboards, zero static file serving — strictly OAuth connect/callback.
# ---------------------------------------------------------------------------

OAUTH_SERVER_SCRIPT = """import html, http.server, os, subprocess, sys, urllib.parse
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
ATLAS_PY = os.path.join(TOOLS_DIR, "atlas.py")

if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

def _get_strava_cid():
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

def _result_page(title, heading, detail, ok=True):
    col = "#34d399" if ok else "#f43f5e"
    return (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title></head>"
        f"<body style='font-family:Inter,system-ui,sans-serif;background:#070a0f;color:#e2e8f0;display:flex;"
        f"align-items:center;justify-content:center;height:100vh;margin:0'>"
        f"<div style='background:#0d121b;border:1px solid rgba(255,255,255,0.1);padding:32px 40px;"
        f"border-radius:14px;text-align:center;max-width:520px'>"
        f"<h2 style='margin:0 0 12px;color:{col}'>{heading}</h2>"
        f"<p style='color:#94a3b8;font-size:14px;line-height:1.5;margin:0'>{detail}</p>"
        f"</div></body></html>"
    ).encode("utf-8")

class _OAuthHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs):
        pass

    def _send_bytes(self, status, body, content_type="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        srv_port = self.server.server_address[1]

        if parsed.path == "/healthz":
            self._send_bytes(200, b"atlas-oauth-v8", "text/plain; charset=utf-8")
            return

        if parsed.path == "/strava-connect":
            cid = _get_strava_cid()
            redir = urllib.parse.quote(f"http://127.0.0.1:{srv_port}/strava-callback", safe="")
            url = (f"https://www.strava.com/oauth/authorize?client_id={cid}&response_type=code"
                   f"&redirect_uri={redir}&approval_prompt=force&scope=read,activity:read,activity:read_all,profile:read_all")
            self._redirect(url)
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
            msg = "&#10003; Strava connected &amp; synced" if ok else "Strava connection failed or cancelled"
            detail = "You can close this tab and return to your Atlas chat."
            self._send_bytes(200 if ok else 400, _result_page("Atlas · Strava Connect", msg, detail, ok=ok))
            return

        if parsed.path == "/hammerhead-connect":
            qs = urllib.parse.parse_qs(parsed.query)
            cid_in = (qs.get("client_id") or [""])[0].strip()
            csec_in = (qs.get("client_secret") or [""])[0].strip()
            if cid_in and csec_in and os.path.isfile(ATLAS_PY):
                subprocess.run([sys.executable, "-I", "-B", ATLAS_PY, "--root", os.path.dirname(BRAIN_DIR),
                                "coach", "auth-hammerhead", "--client-id", cid_in, "--client-secret", csec_in],
                               capture_output=True, text=True, timeout=15)
            from atlas_lib import coach_connectors
            local_cb = f"http://127.0.0.1:{srv_port}/hammerhead-callback"
            cfg = coach_connectors.hammerhead_oauth_config(BRAIN_DIR, local_redirect_uri=local_cb)
            state = coach_connectors.mint_hammerhead_oauth_state(BRAIN_DIR, port=srv_port)
            url = coach_connectors.build_hammerhead_authorize_url(cfg["client_id"], cfg["redirect_uri"], state=state)
            self._redirect(url)
            return

        if parsed.path == "/hammerhead-callback":
            qs = urllib.parse.parse_qs(parsed.query)
            code = (qs.get("code") or [None])[0]
            state = (qs.get("state") or [None])[0]
            from atlas_lib import coach_connectors
            state_ok = coach_connectors.verify_hammerhead_oauth_state(BRAIN_DIR, state)
            ok = False
            if code and state_ok and os.path.isfile(ATLAS_PY):
                res = subprocess.run([sys.executable, "-I", "-B", ATLAS_PY, "--root", os.path.dirname(BRAIN_DIR),
                                      "coach", "auth-hammerhead", "--code", code, "--execute"],
                                     capture_output=True, text=True, timeout=45)
                ok = res.returncode == 0
            msg = "&#10003; Hammerhead Karoo connected" if ok else "Hammerhead connection failed or cancelled"
            detail = "You can close this tab and return to your Atlas chat."
            self._send_bytes(200 if ok else 400, _result_page("Atlas · Hammerhead Connect", msg, detail, ok=ok))
            return

        if parsed.path == "/google-health-connect":
            from atlas_lib import coach_connectors
            local_cb = f"http://127.0.0.1:{srv_port}/google-health-callback"
            cfg = coach_connectors.google_health_oauth_config(BRAIN_DIR, local_redirect_uri=local_cb)
            if not cfg.get("configured"):
                detail = (
                    "Import your Google Cloud OAuth Desktop-App JSON once via CLI:<br>"
                    "<code style='color:#38bdf8'>atlas.py coach auth-google-health --client-json ~/Downloads/client_secret_*.json</code>"
                )
                self._send_bytes(200, _result_page("Atlas · Google Health Setup", "Google Health API v4 Setup", detail, ok=True))
                return
            state, _ver, challenge = coach_connectors.mint_google_health_oauth_state(BRAIN_DIR, port=srv_port)
            url = coach_connectors.build_google_health_authorize_url(
                cfg["client_id"], cfg["redirect_uri"], state=state, code_challenge=challenge, login_hint=cfg.get("login_hint", "")
            )
            self._redirect(url)
            return

        if parsed.path == "/google-health-callback":
            qs = urllib.parse.parse_qs(parsed.query)
            code = (qs.get("code") or [None])[0]
            state = (qs.get("state") or [None])[0]
            local_cb = f"http://127.0.0.1:{srv_port}/google-health-callback"
            from atlas_lib import coach_connectors
            state_ok, verifier = coach_connectors.consume_google_health_oauth_state(BRAIN_DIR, state)
            ok = False
            if code and state_ok and os.path.isfile(ATLAS_PY):
                cmd_gh = [
                    sys.executable, "-I", "-B", ATLAS_PY, "--root", os.path.dirname(BRAIN_DIR),
                    "coach", "auth-google-health", "--code", code, "--redirect-uri", local_cb, "--execute",
                ]
                if verifier:
                    cmd_gh += ["--verifier", verifier]
                res = subprocess.run(cmd_gh, capture_output=True, text=True, timeout=90)
                ok = res.returncode == 0
            msg = "&#10003; Google Health connected &amp; synced" if ok else "Google Health connection failed or cancelled"
            detail = "You can close this tab and return to your Atlas chat."
            self._send_bytes(200 if ok else 400, _result_page("Atlas · Google Health Connect", msg, detail, ok=ok))
            return

        if parsed.path == "/gcal-connect":
            qs = urllib.parse.parse_qs(parsed.query)
            acct = (qs.get("account") or ["personal"])[0].strip() or "personal"
            from atlas_lib import gcal_provider as _gp
            local_cb = f"http://127.0.0.1:{srv_port}/gcal-callback"
            cfg = _gp.gcal_oauth_config(BRAIN_DIR, account_id=acct, local_redirect_uri=local_cb)
            if not cfg.get("configured"):
                detail = (
                    "Import your Google Cloud OAuth Desktop-App JSON once via CLI:<br>"
                    f"<code style='color:#38bdf8'>atlas.py cal auth --account {html.escape(acct)} --client-json ~/Downloads/client_secret_*.json</code>"
                )
                self._send_bytes(200, _result_page("Atlas · Google Calendar Setup", "Google Calendar Sync Setup", detail, ok=True))
                return
            state, _ver, challenge = _gp.mint_gcal_oauth_state(BRAIN_DIR, account_id=acct, port=srv_port)
            url = _gp.build_gcal_authorize_url(
                cfg["client_id"], cfg["redirect_uri"], state=state, code_challenge=challenge, login_hint=cfg.get("login_hint", "")
            )
            self._redirect(url)
            return

        if parsed.path == "/gcal-callback":
            qs = urllib.parse.parse_qs(parsed.query)
            code = (qs.get("code") or [None])[0]
            state = (qs.get("state") or [None])[0]
            local_cb = f"http://127.0.0.1:{srv_port}/gcal-callback"
            from atlas_lib import gcal_provider as _gp
            state_ok, acct_id, verifier = _gp.consume_gcal_oauth_state(BRAIN_DIR, state)
            ok = False
            if code and state_ok and os.path.isfile(ATLAS_PY):
                cmd_gc = [
                    sys.executable, "-I", "-B", ATLAS_PY, "--root", os.path.dirname(BRAIN_DIR),
                    "cal", "auth", "--account", acct_id or "personal", "--code", code,
                    "--redirect-uri", local_cb, "--execute",
                ]
                if verifier:
                    cmd_gc += ["--verifier", verifier]
                res = subprocess.run(cmd_gc, capture_output=True, text=True, timeout=60)
                ok = res.returncode == 0
            safe_acct = html.escape(acct_id or "personal")
            msg = f"&#10003; Google Calendar ({safe_acct}) connected" if ok else "Google Calendar connection failed or cancelled"
            detail = "You can close this tab and return to your Atlas chat."
            self._send_bytes(200 if ok else 400, _result_page("Atlas · Google Calendar Connect", msg, detail, ok=ok))
            return

        self._send_bytes(404, b"Atlas OAuth loopback helper - use Atlas in chat.\\n", "text/plain; charset=utf-8")

srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), _OAuthHandler)
port_file = os.path.join(CACHE_DIR, "cal_port.txt")
try:
    with open(port_file, "w", encoding="utf-8") as _pf:
        _pf.write(str(srv.server_address[1]))
except OSError:
    pass
srv.serve_forever()
"""

# Backward-compatible name alias
CAL_SERVER_SCRIPT = OAUTH_SERVER_SCRIPT


def oauth_http_ok(port):
    """Verify the headless OAuth loopback helper returns HTTP 200 on `/healthz` with `atlas-oauth-v8`."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.25) as resp:
            return resp.status == 200 and OAUTH_SERVER_HEALTH_TOKEN in resp.read()
    except Exception:
        return False


cal_http_ok = oauth_http_ok


def write_oauth_server_script(cache_dir):
    """Materialize `_oauth_server.py` inside `cache_dir` and return its absolute path."""
    os.makedirs(cache_dir, exist_ok=True)
    srv_script = os.path.join(cache_dir, "_oauth_server.py")
    with open(srv_script, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(OAUTH_SERVER_SCRIPT)
    return srv_script


write_server_script = write_oauth_server_script


def ensure_oauth_server(cache_dir, start_server=False, probe=False, tools_dir=None, hook_py=None):
    """Ensure the minimal headless loopback OAuth helper runs on `http://127.0.0.1:<port>` and return its base URL."""
    port_file = os.path.join(cache_dir, "cal_port.txt")
    port = 8765
    try:
        with open(port_file, encoding="utf-8") as fh:
            port = int(fh.read().strip())
    except (OSError, ValueError):
        pass
    if not start_server or probe or os.environ.get("ATLAS_NO_CAL_SERVER"):
        return f"http://127.0.0.1:{port}"
    if oauth_http_ok(port):
        return f"http://127.0.0.1:{port}"
    if os.name == "nt":
        try:
            kill_ps = (
                "Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%' AND "
                "(CommandLine LIKE '%_cal_server.py%' OR CommandLine LIKE '%_oauth_server.py%')\" "
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
        for pat in ("[_]cal_server.py", "[_]oauth_server.py"):
            try:
                subprocess.run(
                    ["pkill", "-f", pat],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                )
            except Exception:
                pass
        time.sleep(0.1)
    for cand in range(8765, 8795):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", cand))
                port = cand
                break
        except OSError:
            continue
    try:
        srv_script = write_oauth_server_script(cache_dir)
        with open(port_file, "w", encoding="utf-8") as fh:
            fh.write(str(port))
        py_exe = sys.executable
        if os.name == "nt":
            pyw = os.path.join(os.path.dirname(py_exe), "pythonw.exe")
            if os.path.isfile(pyw):
                py_exe = pyw
        here = os.path.dirname(os.path.abspath(__file__))
        eff_tools = tools_dir or os.path.normpath(os.path.join(here, ".."))
        server_cmd = [py_exe, "-I", "-B", srv_script, str(port), cache_dir, eff_tools]
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
            if oauth_http_ok(port):
                break
            time.sleep(0.1)
    except Exception:
        pass
    return f"http://127.0.0.1:{port}"


ensure_calendar_server = ensure_oauth_server
