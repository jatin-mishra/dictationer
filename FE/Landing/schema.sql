-- Dictationer waitlist schema (Cloudflare D1 / SQLite)
CREATE TABLE IF NOT EXISTS signups (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  email           TEXT NOT NULL UNIQUE,
  language        TEXT,            -- language they write in
  language_other  TEXT,
  page_lang       TEXT,            -- language they read the page in: en | hi
  variant         TEXT,            -- headline test: en-A, hi-A, hi-B ...
  device          TEXT,
  utm_source      TEXT, utm_medium TEXT, utm_campaign TEXT, utm_content TEXT, utm_term TEXT,
  referrer        TEXT,
  landing         TEXT,
  country         TEXT, region TEXT, city TEXT,   -- from Cloudflare's request data, no extra lookup
  ref_code        TEXT NOT NULL UNIQUE,
  referred_by     TEXT,            -- the referrer's code; unknown codes stored as INVALID:xxx
  consent_at      TEXT NOT NULL,
  consent_version TEXT,
  marketing       INTEGER NOT NULL DEFAULT 0,
  edit_token      TEXT,
  -- step 2 (optional profile)
  name TEXT, write_types TEXT, stage TEXT, blocker TEXT, notes TEXT, whatsapp TEXT,
  wtp             TEXT,            -- plan they would pick at launch: 0 | 699 | 1499 (INR) | usd_15 | usd_29
  plan_at         TEXT,
  profile_at      TEXT,
  chosen_country  TEXT             -- country picked on the page: IN US GB CA AE SG AU OT
);
CREATE INDEX IF NOT EXISTS idx_signups_referred_by ON signups (referred_by);
CREATE INDEX IF NOT EXISTS idx_signups_created_at ON signups (created_at);
