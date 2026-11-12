# Procedure: Atlas Mail (`/atlas-mail`)

Multi-account IMAP mail engine (`atlas_mail.py` / `atlas.py mail`) built on Python's standard library (`imaplib`).
Combines **strict Read-Only inspection by default** (`readonly=True`, `BODY.PEEK[]`) with **3-stage Safety-Gated Write Operations** (`move` to `target_folder` via RFC 6851 `MOVE`, `copy`, `trash`, `delete`, `flags`, `labels`, `mkdir`).

## 1. Credentials & Configuration (`brain/_secrets/mail.env`)

- All mail credentials live exclusively in `<ROOT>/brain/_secrets/mail.env` (`chmod 600`, git-ignored).
- Never store credentials in tracked Markdown pages.
- If `brain/_secrets/mail.env` does not exist yet or the user wants to add an account, initialize a template block:
  ```bash
  python -I "<ROOT>/brain/_atlas/tools/atlas.py" mail init -a WORK --label "Work Mail" --user "name@example.com"
  ```
- Each account is configured with the prefix `MAIL_<ID>_*`:
  - `MAIL_<ID>_LABEL` — Display name of the mailbox
  - `MAIL_<ID>_HOST` — IMAP server (auto-detected for Gmail, iCloud, Outlook/Office365, GMX, Web.de, T-Online, Posteo, Mailbox.org, Fastmail, Strato, IONOS; required for custom domains)
  - `MAIL_<ID>_PORT` — IMAP port (default: `993` SSL/TLS)
  - `MAIL_<ID>_USER` — Login / email address
  - `MAIL_<ID>_PASSWORD` — Password or 16-character App Password (for Gmail / Google Workspace / iCloud)
  - `MAIL_<ID>_ALLOW_WRITE` — Optional (`true`/`false`, default: `true`). Setting `false` hard-blocks all write commands on that account.
  - `MAIL_<ID>_TRASH_FOLDER` — Optional custom Trash folder name (auto-detected via RFC 6154 `\Trash` by default).

## 2. Read-Only Commands (`readonly=True` — never alters `\Seen` / unread state)

You can invoke the mail engine either via `atlas.py mail ...` or directly via `atlas_mail.py ...`:

1. **Check accounts, connection status & server IMAP capabilities (`MOVE`, `UIDPLUS`, `X-GM-EXT-1`):**
   ```bash
   python -I "<ROOT>/brain/_atlas/tools/atlas.py" mail accounts
   ```
2. **List all IMAP folders (with Modified UTF-7 decoding & special-use flags):**
   ```bash
   python -I "<ROOT>/brain/_atlas/tools/atlas.py" mail folders -a <account>
   ```
3. **List messages in a folder (with `UNSEEN`, `★`, `↩` `FLAGS` & `LABELS` badges):**
   ```bash
   python -I "<ROOT>/brain/_atlas/tools/atlas.py" mail list -a <account> -f INBOX -n 50 [--unread] [--flagged] [--since YYYY-MM-DD]
   ```
4. **Search messages (Subject, From, To, Body, attachment filenames, and labels):**
   ```bash
   python -I "<ROOT>/brain/_atlas/tools/atlas.py" mail search "<query>" -a <account> -f INBOX
   ```
5. **Read a single message in full (including `FLAGS`, `LABELS` & optional attachment extraction):**
   ```bash
   python -I "<ROOT>/brain/_atlas/tools/atlas.py" mail read <UID> -a <account> -f INBOX [--save-attachments "<dir>"]
   ```
6. **Full deduplicated mailbox audit (All Mail or INBOX + Sent + Archive):**
   ```bash
   python -I "<ROOT>/brain/_atlas/tools/atlas.py" mail audit -a <account> -n 200 [--since YYYY-MM-DD]
   ```

## 3. Safety-Gated Write Commands (`MOVE`, `TRASH`, `FLAGS`, `LABELS`, `MKDIR`)

Every write operation in `atlas_mail.py` enforces a **3-Stage Safety Gate**:
1. **Stage 1 (Account Guard):** Checks `MAIL_<ID>_ALLOW_WRITE`.
2. **Stage 2 (Pre-Flight Preview):** Opens the source folder in `readonly=True` first, fetches `Subject`, `From`, `Date`, `FLAGS`, and `LABELS` of all target UIDs, and prints the exact summary.
3. **Stage 3 (Execution Gate):**
   - `--dry-run` prints the preview and exits `0` without touching the mailbox.
   - Calling any write command non-interactively **without `--yes`** blocks execution (`exit 2`, `CONFIRMATION_REQUIRED`) after printing the preview.

### 3.1 Move / Sort / Copy (`move` & `copy` -> `target_folder`)
Uses atomic **RFC 6851 `UID MOVE`** when supported by the server, or automatically falls back to **RFC 3501 `COPY` + `STORE (\Deleted)` + RFC 4315 `UID EXPUNGE`**:
```bash
# Step 1 — Dry-run preview:
python -I "<ROOT>/brain/_atlas/tools/atlas.py" mail move <UID> [UID2 ...] -t "<target_folder>" -a <account> -f INBOX --dry-run

# Step 2 — Execute (ONLY after showing the preview and getting the user's explicit go):
python -I "<ROOT>/brain/_atlas/tools/atlas.py" mail move <UID> [UID2 ...] -t "<target_folder>" -a <account> -f INBOX [--create-folder] --yes
```

### 3.2 Delete: Soft-Delete to Trash (`trash`) vs. Permanent Delete (`delete --hard`)
- **Always use `trash` by default:** Moves the message(s) into the account's Trash folder (`\Trash`, e.g. `[Gmail]/Trash` / `[Gmail]/Papierkorb`), keeping them recoverable:
  ```bash
  python -I "<ROOT>/brain/_atlas/tools/atlas.py" mail trash <UID> -a <account> -f INBOX --dry-run
  python -I "<ROOT>/brain/_atlas/tools/atlas.py" mail trash <UID> -a <account> -f INBOX --yes
  ```
- **Hard-Delete (`delete --hard`):** Only if the user explicitly requests permanent, unrecoverable deletion (`\Deleted` + `UID EXPUNGE`):
  ```bash
  python -I "<ROOT>/brain/_atlas/tools/atlas.py" mail delete <UID> -a <account> -f INBOX --hard --yes
  ```

### 3.3 Manage IMAP `FLAGS` (`flags`)
Supports RFC 3501 system flags (`\Seen`, `\Flagged`, `\Answered`, `\Draft`, `\Deleted`) and custom IMAP keywords:
```bash
# Read-only inspection of current FLAGS:
python -I "<ROOT>/brain/_atlas/tools/atlas.py" mail flags <UID> -a <account>

# Modify flags (--read, --unread, --star, --unstar, --answered, --unanswered, --add-flags, --remove-flags, --set-flags):
python -I "<ROOT>/brain/_atlas/tools/atlas.py" mail flags <UID> -a <account> --read --star --dry-run
python -I "<ROOT>/brain/_atlas/tools/atlas.py" mail flags <UID> -a <account> --read --star --yes
```

### 3.4 Manage Gmail `X-GM-LABELS` & IMAP Keyword Labels (`labels`)
Manages native Gmail labels (`X-GM-EXT-1` with Modified UTF-7 encoding) or standard IMAP keyword flags (`--use-keywords`):
```bash
# Read-only inspection of current LABELS:
python -I "<ROOT>/brain/_atlas/tools/atlas.py" mail labels <UID> -a <account>

# Add / remove / set labels:
python -I "<ROOT>/brain/_atlas/tools/atlas.py" mail labels <UID> -a <account> --add "Taxes" "Invoices" --dry-run
python -I "<ROOT>/brain/_atlas/tools/atlas.py" mail labels <UID> -a <account> --add "Taxes" --remove "Todo" --yes
```

### 3.5 Create a New IMAP Folder (`mkdir` / `create-folder`)
```bash
python -I "<ROOT>/brain/_atlas/tools/atlas.py" mail mkdir "<target_folder>" -a <account> --dry-run
python -I "<ROOT>/brain/_atlas/tools/atlas.py" mail mkdir "<target_folder>" -a <account> --yes
```

## 4. Mandatory Rules for the Agent

1. **Read-Only First:** When searching, reading, summarizing, or auditing emails, use only the Read-Only commands (`list`, `read`, `search`, `audit`, `flags <UID>`, `labels <UID>`).
2. **Human-in-the-Loop Gate (Never pass `--yes` unprompted):**
   - Before executing any write command (`move`, `copy`, `trash`, `delete`, `flags`, `labels`, `mkdir`) with `--yes`, run `--dry-run` (or inspect the messages), show the user the exact list of affected messages (**UID, Sender, Subject, Date**) and the planned target (`target_folder`, `FLAGS`, or `LABELS`), and wait for their confirmation.
3. **Soft-Delete over Hard-Delete:** Always use `trash` when asked to delete emails unless the user explicitly demands permanent `delete --hard`.
4. **Knowledge Bridge:** When auditing or reading emails relevant to the user's projects, finances, contracts, or schedule, file lasting facts into the matching `brain/` pages and deadlines/events into `brain/calendar/README.md`.
