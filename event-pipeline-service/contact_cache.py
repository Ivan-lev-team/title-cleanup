"""
Persistent cross-run contact cache. Exists so the same person is never paid for
twice.

Every paid step in the pipeline is keyed and remembered:
  seamless  1 credit per resolve (email + mobile)
  prospeo   1 credit per reveal
  verify    DeBounce + LeadMagic validate

Without this, re-running a cohort, resuming after a crash, or overlapping two
company batches re-charges every provider for people already resolved. Seamless
in particular answers a repeat request with status "duplicate" and NO payload,
so the credit is spent and nothing comes back -- the worst possible outcome.

Keying, in priority order:
  1. normalized LinkedIn slug   -- stable, provider-independent
  2. lowercased email
  3. firstname|lastname|domain  -- last resort; pipe-separated because
     "Jo"+"Anne" and "Joan"+"ne" collide when concatenated bare

The file is a single JSON object written atomically (temp file + replace) so a
crash mid-write cannot corrupt it. It is small: ~300 bytes per contact, so even
50k contacts is ~15MB.
"""
import json
import os
import re
import tempfile
import threading

DEFAULT_PATH = os.path.join("data", "contact_cache.json")
_lock = threading.Lock()


def _slug(url):
    m = re.search(r"linkedin\.com/in/([a-z0-9\-_%\.]+)", (url or "").lower())
    return m.group(1).strip("/") if m else ""


def key_for(row):
    """Stable identity for a contact row. Returns '' when nothing usable."""
    s = _slug(row.get("linkedin_url") or "")
    if s:
        return "li:" + s
    em = (row.get("email") or "").strip().lower()
    if em:
        return "em:" + em
    fn = (row.get("first_name") or "").strip().lower()
    ln = (row.get("last_name") or "").strip().lower()
    dom = (row.get("company_domain") or "").strip().lower()
    if (fn or ln) and dom:
        return "nd:%s|%s|%s" % (fn, ln, dom)
    return ""


class ContactCache:
    def __init__(self, path=DEFAULT_PATH):
        self.path = path
        self.data = {}
        self.hits = {"seamless": 0, "prospeo": 0, "verify": 0}
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f) or {}
        except (ValueError, OSError):
            # a corrupt cache must never block a run; start clean
            self.data = {}

    def save(self):
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        with _lock:
            fd, tmp = tempfile.mkstemp(dir=d or ".", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.data, f)
                os.replace(tmp, self.path)
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise

    def get(self, row, section):
        k = key_for(row)
        if not k:
            return None
        rec = self.data.get(k)
        if not rec:
            return None
        return rec.get(section)

    def put(self, row, section, payload):
        k = key_for(row)
        if not k:
            return
        with _lock:
            rec = self.data.setdefault(k, {})
            rec[section] = payload
            rec.setdefault("name", (row.get("first_name", "") + " " +
                                    row.get("last_name", "")).strip())
            rec["domain"] = row.get("company_domain", "")

    def note_hit(self, section):
        with _lock:
            self.hits[section] = self.hits.get(section, 0) + 1

    def summary(self):
        return {"cached_contacts": len(self.data),
                "reused_this_run": dict(self.hits),
                "credits_saved_est": self.hits.get("seamless", 0)
                                     + self.hits.get("prospeo", 0)}
