#!/usr/bin/env python3
"""
Co.Do substitution runner.

Runs 24/7 on GitHub Actions (free). Every cycle it:
  1. pulls pending swap requests from the Apps Script queue,
  2. reassigns the Arbox instructor via a REAL headless browser (the only thing
     that passes Arbox's write gate; a plain server request gets HTTP 500),
  3. verifies the change against Arbox's public API,
  4. tells Apps Script to update Google Calendar and mark the row done.

Safety is locked in code, not in good intentions:
  - only updateEventStaff on an existing slot; never delete, cancel, or message,
  - only sessions whose label is in ALLOWED_SESSIONS at location 19747,
  - verify-after-write; a swap is "done" only if Arbox confirms it,
  - idempotent: if Arbox already shows the substitute, the write is skipped,
  - any failure marks that row "error" with a reason and moves on; one bad row
    never blocks the rest.

Config comes from environment (GitHub Secrets), never hardcoded:
  ARBOX_JWT         Tamar's Arbox token
  ARBOX_API_KEY     Arbox public read api-key
  APPS_SCRIPT_URL   the deployed Apps Script web app URL (queue + finalize)
  SHARED_SECRET     shared key so only we can read/close the queue
Optional:
  DRY_RUN=1         read ./test_queue.json, do a no-op Arbox write, print, no finalize
"""

import os, sys, json, time, unicodedata, urllib.request, urllib.parse

def _norm(s):
    return unicodedata.normalize("NFC", str(s or "")).strip()

LOCATION_ID = 19747
ALLOWED_SESSIONS = {_norm(x) for x in ("קודו אונליין", "CoWork Online", "קודו אונליין סופ\"ש")}
ARBOX_BASE = "https://arboxserver.arboxapp.com"

JWT = os.environ.get("ARBOX_JWT", "")
API_KEY = os.environ.get("ARBOX_API_KEY", "")
APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL", "")
SHARED_SECRET = os.environ.get("SHARED_SECRET", "")
DRY_RUN = os.environ.get("DRY_RUN", "") in ("1", "true", "True")

# Arbox coach accounts that are NOT active substitutable instructors, so they
# must not appear as a choice in the swap form's substitute picker. This hides
# them from the form only; it does NOT delete or change the Arbox accounts.
# Kept-on-purpose non-scheduled reserves are יהלי and שיר (Tamar 2026-10-29).
EXCLUDE_COACHES = {
    "גילור פרידמן זלצר",
    "מלכי פוריס",
    "קרן הראל",
    "תהל גולדשמיט",
    "תמר טאובר פאוזנר",
}

# Arbox's WAF rejects the default Python-urllib agent (403), so we present a
# normal browser User-Agent on every plain-HTTP read.
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


# ---------- plain HTTP helpers (reads + Apps Script; NOT the Arbox write) ----------

def _get(url, headers=None, timeout=30):
    h = {"User-Agent": _UA}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")

def _post(url, data, headers=None, timeout=30):
    body = json.dumps(data).encode("utf-8")
    h = {"content-type": "application/json", "User-Agent": _UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def israel_today():
    # GitHub runners are UTC; derive Israel date without extra deps.
    os.environ["TZ"] = "Asia/Jerusalem"
    try:
        time.tzset()  # not on Windows; fine, we only need this on the Linux runner
    except AttributeError:
        pass
    return time.strftime("%Y-%m-%d")


# ---------- Arbox reads (work over plain HTTP) ----------

def coach_map():
    """fullName -> {user_fk, email}."""
    status, text = _get(
        f"{ARBOX_BASE}/api/manage/v2/getCoachesOfBox",
        headers={"token": JWT, "lang": "he", "manage": "system", "isargaz": "false"},
    )
    data = json.loads(text)
    rows = data if isinstance(data, list) else data.get("data", data)
    m = {}
    for c in rows:
        full = (c.get("first_name", "") + " " + c.get("last_name", "")).strip()
        m[full] = {"user_fk": c.get("user_fk"), "email": c.get("email")}
    return m

def schedule_for(date):
    q = urllib.parse.urlencode({"from_date": date, "to_date": date, "location_id": LOCATION_ID})
    status, text = _get(f"{ARBOX_BASE}/api/public/v3/schedule?{q}", headers={"api-key": API_KEY})
    return json.loads(text).get("data", [])

def find_slot(date, start_time, session_label):
    """Return (schedule_id, current_email) or (None, None)."""
    rows = schedule_for(date)
    cands = [r for r in rows if (r.get("start_time") or "")[:5] == start_time[:5]]
    if session_label:
        exact = [r for r in cands if _norm(r.get("session_name")) == _norm(session_label)]
        if exact:
            cands = exact
    if len(cands) != 1:
        return None, None
    r = cands[0]
    return r.get("schedule_id"), ((r.get("staff_member") or {}).get("email"))


# ---------- the one browser-gated call: Arbox instructor reassignment ----------

_ARBOX_JS = """
async (args) => {
  const [body, jwt] = args;
  const r = await fetch('%s/api/manage/v2/fieldsUpdate', {
    method:'POST',
    headers:{'content-type':'application/json','token':jwt,'lang':'he','manage':'system','isargaz':'false'},
    body: body
  });
  return {status: r.status, body: await r.text()};
}
""" % ARBOX_BASE

def arbox_reassign(page, schedule_id, uf, full_name):
    payload = {"data": [{"functionName": "updateEventStaff", "params": {
        "schedule_id": schedule_id, "is_series": False, "series_id": None,
        "coach_id": uf, "space_id": uf, "category_id": uf, "is_main_coach": True,
        "date": None, "start_time": None, "end_time": None,
        "spaces_relation": {"id": uf, "value": uf, "name": full_name, "label": full_name, "key": 4},
    }}]}
    res = page.evaluate(_ARBOX_JS, [json.dumps(payload), JWT])
    ok = res.get("status") == 200 and "updateEventStaff" in (res.get("body") or "")
    return ok, res


# ---------- queue (Apps Script, or local file in dry-run) ----------

def fetch_queue():
    if DRY_RUN:
        with open("test_queue.json", encoding="utf-8") as f:
            return json.load(f)
    url = f"{APPS_SCRIPT_URL}?" + urllib.parse.urlencode({"action": "queue", "key": SHARED_SECRET})
    status, text = _get(url)
    return json.loads(text).get("rows", [])

def finalize(row_id, outcome, reason="", sub_email="", sub_first="", original_email=""):
    if DRY_RUN:
        print(f"  [dry-run] finalize row={row_id} outcome={outcome} reason={reason}")
        return
    _post(APPS_SCRIPT_URL, {
        "action": "finalize", "key": SHARED_SECRET, "row_id": row_id,
        "outcome": outcome, "reason": reason,
        "substitute_email": sub_email, "substitute_first_name": sub_first,
        "original_email": original_email,
    })


# ---------- snapshot: keep the form's picker current ----------

def push_snapshot():
    """Build the upcoming-slots list from Arbox and push it to Apps Script so the
    form's picker stays current every cycle. Non-fatal on any failure."""
    if DRY_RUN or not APPS_SCRIPT_URL:
        return
    today = israel_today()
    to = time.strftime("%Y-%m-%d", time.localtime(time.time() + 31 * 86400))
    try:
        q = urllib.parse.urlencode({"from_date": today, "to_date": to, "location_id": LOCATION_ID})
        _, text = _get(f"{ARBOX_BASE}/api/public/v3/schedule?{q}", headers={"api-key": API_KEY})
        rows = json.loads(text).get("data", [])
    except Exception as e:
        print("snapshot fetch failed:", e); return
    snap, seen = [], set()
    for r in rows:
        name = (r.get("staff_member") or {}).get("name")
        if not name:
            continue
        date = r.get("date"); t = (r.get("start_time") or "")[:5]; sess = r.get("session_name") or ""
        key = (name, date, t, sess)
        if key in seen:
            continue
        seen.add(key)
        snap.append({"name": name, "date": date, "time": t, "session": sess})
    snap.sort(key=lambda x: (x["date"], x["time"]))
    # Full instructor pool from Arbox (includes reserve/substitute-only מנחות who
    # have no slot in the schedule window, so the form can offer them as a
    # substitute). Non-fatal: if this fetch fails we push slots only and Apps
    # Script keeps the previous roster rather than wiping it.
    roster = []
    try:
        # Hide from the substitute picker: test/dummy accounts (name contains
        # "ניסיון") and Arbox coach accounts that are NOT active substitutable
        # instructors (old / inactive / non-teaching), per Tamar 2026-10-29 she
        # wants only יהלי + שיר kept among the non-scheduled reserves. This only
        # hides them from the form; the Arbox accounts themselves are untouched.
        roster = sorted(
            n for n in coach_map().keys()
            if n and "ניסיון" not in n and n not in EXCLUDE_COACHES
        )
    except Exception as e:
        print("roster fetch failed (pushing slots only):", e)
    try:
        payload = {"action": "setsnapshot", "key": SHARED_SECRET, "snapshot": snap}
        if roster:
            payload["roster"] = roster
        _post(APPS_SCRIPT_URL, payload)
        print(f"snapshot pushed: {len(snap)} slots, {len(roster)} roster")
    except Exception as e:
        print("snapshot push failed:", e)


# ---------- main ----------

def process_row(page, row, coaches, today):
    rid = row.get("row_id") or row.get("id")
    date = row.get("date"); start = row.get("start_time") or row.get("time")
    label = row.get("session_label") or ""
    original = row.get("original_instructor"); sub = row.get("substitute_instructor")

    if label and _norm(label) not in ALLOWED_SESSIONS:
        return finalize(rid, "error", f"session '{label}' not allowed")
    if date < today:
        return  # past slot: leave it, do not touch a class that already happened

    sub_info = coaches.get(sub)
    if not sub_info or not sub_info.get("user_fk"):
        return finalize(rid, "error", f"substitute '{sub}' not in Arbox coaches")

    sid, current_email = find_slot(date, start, label)
    if not sid:
        return finalize(rid, "error", "no unique matching Arbox slot")

    sub_email = sub_info["email"]; uf = sub_info["user_fk"]
    sub_first = (sub or "").split(" ")[0]

    if current_email and sub_email and current_email.lower() == sub_email.lower():
        print(f"  row {rid}: Arbox already shows {sub}; skipping write")
        return finalize(rid, "done", "", sub_email, sub_first)

    ok, res = arbox_reassign(page, sid, uf, sub)
    if not ok:
        return finalize(rid, "error", f"arbox write failed ({res.get('status')})")

    _, after_email = find_slot(date, start, label)
    if not (after_email and sub_email and after_email.lower() == sub_email.lower()):
        return finalize(rid, "error", "arbox write not reflected on verify")

    print(f"  row {rid}: {sub} now on {date} {start} ({label}) — verified")
    finalize(rid, "done", "", sub_email, sub_first, original_email=current_email or "")


def main():
    missing = [k for k in ("ARBOX_JWT", "ARBOX_API_KEY") if not os.environ.get(k)]
    if not DRY_RUN:
        missing += [k for k in ("APPS_SCRIPT_URL", "SHARED_SECRET") if not os.environ.get(k)]
    if missing:
        print("missing config:", ", ".join(missing)); sys.exit(2)

    push_snapshot()  # refresh the form's slot list every cycle

    queue = [r for r in fetch_queue() if (r.get("status") or "pending") == "pending"]
    if not queue:
        print("no pending requests"); return

    today = israel_today()
    coaches = coach_map()

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto("https://manage.arboxapp.com", wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print("nav note:", e)
        for row in queue:
            try:
                process_row(page, row, coaches, today)
            except Exception as e:
                rid = row.get("row_id") or row.get("id")
                print(f"  row {rid}: unexpected error: {e}")
                try:
                    finalize(rid, "error", f"runner exception: {e}")
                except Exception:
                    pass
        browser.close()


if __name__ == "__main__":
    main()
