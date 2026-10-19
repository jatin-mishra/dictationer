/**
 * Dictationer waitlist Worker
 * - Serves the landing page from ./public (Workers Static Assets)
 * - POST /api/join      step 1 signup  -> D1
 * - POST /api/profile   step 2 profile -> D1 (needs the per-signup token)
 * - POST /api/plan      plan they'd pick at launch (needs the token)
 * - GET  /api/admin/stats        owner only: JSON totals
 * - GET  /api/admin/export.csv   owner only: all rows as CSV (Google Sheets: =IMPORTDATA(url))
 *   Owner auth = EXPORT_KEY secret (24+ chars) as "Authorization: Bearer <key>" or ?key=<key>.
 *   Visitors can only add their own signup; no route returns anyone else's data.
 * Optional: confirmation email through Brevo when BREVO_API_KEY + SENDER_EMAIL are set.
 */

const LANGS = ['hindi', 'english', 'marathi', 'bengali', 'tamil', 'telugu', 'gujarati', 'kannada', 'malayalam', 'urdu', 'punjabi', 'other'];
const REF_BOOST = 10;          // each valid referral moves the referrer 10 places up
const MIN_FILL_MS = 1200;      // faster than this is treated as a bot (autofill + tap takes longer)

const json = (obj, status = 200) => new Response(JSON.stringify(obj), {
  status, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', 'x-content-type-options': 'nosniff', 'x-robots-tag': 'noindex' }
});
const clean = (v, max) => String(v ?? '').replace(/[\u0000-\u001f\u007f]+/g, ' ').trim().slice(0, max);
const pick = (v, allowed, fallback = '') => (allowed.includes(v) ? v : fallback);

function refCode() {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  const bytes = crypto.getRandomValues(new Uint8Array(6));
  return Array.from(bytes, b => chars[b % chars.length]).join('');
}
function safeEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  let r = 0; for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}
async function readBody(request) {
  const len = Number(request.headers.get('content-length') || 0);
  if (len > 8192) throw new Error('too_large');
  return request.json();
}

async function position(db, id, code) {
  const row = await db.prepare(
    `SELECT (SELECT COUNT(*) FROM signups WHERE id <= ?1) AS rank,
            (SELECT COUNT(*) FROM signups WHERE referred_by = ?2) AS refs`
  ).bind(id, code).first();
  return Math.max(1, (row?.rank || 1) - REF_BOOST * (row?.refs || 0));
}

async function join(request, env, ctx) {
  let b; try { b = await readBody(request); } catch { return json({ ok: false, error: 'bad_request' }, 400); }
  // bots: honeypot filled or impossibly fast -> pretend success, store nothing
  if (b.website || Number(b.elapsed_ms || 0) < MIN_FILL_MS) return json({ ok: true, ref_code: 'XXXXXX', position: 1 });

  const email = clean(b.email, 254).toLowerCase();
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(email)) return json({ ok: false, error: 'invalid_email' }, 400);
  if (b.consent !== true) return json({ ok: false, error: 'consent_required' }, 400);

  const db = env.DB;
  const existing = await db.prepare('SELECT id, ref_code FROM signups WHERE email = ?1').bind(email).first();
  if (existing) {
    // Knowing an email must reveal nothing: no referral code, no position, no token.
    return json({ ok: true, dup: true });
  }

  let referredBy = clean(b.ref, 12).toUpperCase().replace(/[^A-Z0-9]/g, '');
  if (referredBy) {
    const r = await db.prepare('SELECT 1 FROM signups WHERE ref_code = ?1').bind(referredBy).first();
    if (!r) referredBy = 'INVALID:' + referredBy;
  }
  const token = crypto.randomUUID();
  const cf = request.cf || {};
  const pageLang = pick(b.page_lang, ['en', 'hi'], 'en');
  const chosenCountry = pick(String(b.chosen_country || '').toUpperCase(), COUNTRIES, '');
  let code = refCode(), inserted = null;

  for (let attempt = 0; attempt < 4 && !inserted; attempt++) {
    try {
      inserted = await db.prepare(
        `INSERT INTO signups (email, language, language_other, page_lang, variant, device,
           utm_source, utm_medium, utm_campaign, utm_content, utm_term, referrer, landing,
           country, region, city, ref_code, referred_by, consent_at, consent_version, marketing, edit_token, chosen_country)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, datetime('now'), ?19, ?20, ?21, ?22)
         RETURNING id`
      ).bind(
        email, pick(b.language, LANGS, 'other'), clean(b.language_other, 40), pageLang, clean(b.variant, 20),
        pick(b.device, ['mobile', 'tablet', 'desktop'], ''),
        clean(b.utm_source, 100).toLowerCase(), clean(b.utm_medium, 100).toLowerCase(), clean(b.utm_campaign, 100).toLowerCase(),
        clean(b.utm_content, 100).toLowerCase(), clean(b.utm_term, 100).toLowerCase(), clean(b.referrer, 200), clean(b.landing, 120),
        clean(cf.country, 2), clean(cf.region, 60), clean(cf.city, 60),
        code, referredBy || null, clean(b.consent_version, 20), b.marketing === true ? 1 : 0, token, chosenCountry
      ).first();
    } catch (e) {
      const msg = String(e && e.message || e);
      if (msg.includes('signups.email')) return json({ ok: true, dup: true }); // a double-click raced us
      if (msg.includes('signups.ref_code')) { code = refCode(); continue; }
      throw e;
    }
  }
  if (!inserted) return json({ ok: false, error: 'server_busy' }, 503);

  if (env.BREVO_API_KEY && env.SENDER_EMAIL) ctx.waitUntil(sendWelcome(env, email, code, pageLang).catch(() => {}));
  return json({ ok: true, ref_code: code, token, position: await position(db, inserted.id, code) });
}

async function profile(request, env) {
  let b; try { b = await readBody(request); } catch { return json({ ok: false, error: 'bad_request' }, 400); }
  const email = clean(b.email, 254).toLowerCase();
  const token = clean(b.token, 64);
  const row = await env.DB.prepare('SELECT id, edit_token FROM signups WHERE email = ?1').bind(email).first();
  if (!row) return json({ ok: false, error: 'not_found' }, 404);
  if (!safeEqual(row.edit_token || '', token)) return json({ ok: false, error: 'bad_token' }, 403);

  const types = clean(b.write_types, 200).split(',').filter(t => ['novel', 'short_stories', 'memoir', 'poetry', 'non_fiction', 'screenplay'].includes(t)).join(',');
  const wa = clean(b.whatsapp, 24).replace(/\D/g, '');   // digits only, e.g. 919876543210
  await env.DB.prepare(
    `UPDATE signups SET name = ?2, write_types = ?3, stage = ?4, blocker = ?5, notes = ?6, whatsapp = ?7, profile_at = datetime('now')
     WHERE id = ?1`
  ).bind(
    row.id, clean(b.name, 60), types,
    pick(b.stage, ['idea', 'drafting', 'finished', 'published']),
    pick(b.blocker, ['typing', 'finishing', 'editing', 'cover_format', 'publishing']),
    clean(b.notes, 400), /^[0-9]{10,15}$/.test(wa) ? wa : ''
  ).run();
  return json({ ok: true });
}

// A returning writer's current place and how many co-writers joined with their link (needs their token)
async function me(request, env) {
  let b; try { b = await readBody(request); } catch { return json({ ok: false, error: 'bad_request' }, 400); }
  const email = clean(b.email, 254).toLowerCase();
  const row = await env.DB.prepare('SELECT id, ref_code, edit_token FROM signups WHERE email = ?1').bind(email).first();
  if (!row || !safeEqual(row.edit_token || '', clean(b.token, 64))) return json({ ok: false, error: 'forbidden' }, 403);
  const refs = await env.DB.prepare('SELECT COUNT(*) AS n FROM signups WHERE referred_by = ?1').bind(row.ref_code).first();
  return json({ ok: true, position: await position(env.DB, row.id, row.ref_code), referrals: refs?.n || 0 });
}

// The plan a writer says they would pick at launch (nothing is charged)
async function plan(request, env) {
  let b; try { b = await readBody(request); } catch { return json({ ok: false, error: 'bad_request' }, 400); }
  const email = clean(b.email, 254).toLowerCase();
  const row = await env.DB.prepare('SELECT id, edit_token FROM signups WHERE email = ?1').bind(email).first();
  if (!row) return json({ ok: false, error: 'not_found' }, 404);
  if (!safeEqual(row.edit_token || '', clean(b.token, 64))) return json({ ok: false, error: 'bad_token' }, 403);
  const choice = pick(b.plan, PLANS);
  if (!choice) return json({ ok: false, error: 'bad_plan' }, 400);
  await env.DB.prepare("UPDATE signups SET wtp = ?2, plan_at = datetime('now') WHERE id = ?1").bind(row.id, choice).run();
  return json({ ok: true });
}

// Only the owner: needs the EXPORT_KEY secret, sent as "Authorization: Bearer <key>" or ?key=<key>
// (the query form exists only so Google Sheets IMPORTDATA can read the CSV).
function authorised(request, url, env) {
  if (!env.EXPORT_KEY || env.EXPORT_KEY.length < 24) return false;   // refuse weak or missing keys
  const auth = request.headers.get('authorization') || '';
  const given = auth.startsWith('Bearer ') ? auth.slice(7) : (url.searchParams.get('key') || '');
  return safeEqual(given, env.EXPORT_KEY);
}
// Browsers send Origin on cross-site POSTs; refuse writes that come from any other website
function sameOrigin(request, url) {
  const origin = request.headers.get('origin');
  return !origin || origin === url.origin;
}

async function stats(env) {
  const q = sql => env.DB.prepare(sql).all().then(r => r.results);
  const [totals] = await q(`SELECT COUNT(*) AS signups,
      SUM(profile_at IS NOT NULL) AS profiles,
      SUM(plan_at IS NOT NULL) AS plan_answers,
      SUM(wtp IN ('699','1499','usd_15','usd_29')) AS paid_plan_picked,
      SUM(referred_by IS NOT NULL AND referred_by NOT LIKE 'INVALID:%') AS referred,
      SUM(date(created_at) = date('now')) AS today FROM signups`);
  return json({
    ...totals,
    by_language: await q('SELECT language, COUNT(*) AS n FROM signups GROUP BY language ORDER BY n DESC'),
    by_chosen_country: await q("SELECT COALESCE(NULLIF(chosen_country,''),'unknown') AS chosen_country, COUNT(*) AS n FROM signups GROUP BY 1 ORDER BY n DESC"),
    by_country_detected: await q("SELECT COALESCE(NULLIF(country,''),'unknown') AS country, COUNT(*) AS n FROM signups GROUP BY 1 ORDER BY n DESC"),
    by_page_lang: await q('SELECT page_lang, COUNT(*) AS n FROM signups GROUP BY page_lang ORDER BY n DESC'),
    by_source: await q("SELECT COALESCE(NULLIF(utm_source,''),'direct') AS source, COUNT(*) AS n FROM signups GROUP BY source ORDER BY n DESC"),
    by_variant: await q('SELECT variant, COUNT(*) AS n FROM signups GROUP BY variant ORDER BY n DESC'),
    by_plan: await q("SELECT wtp, COUNT(*) AS n FROM signups WHERE wtp <> '' GROUP BY wtp ORDER BY n DESC"),
    last_14_days: await q("SELECT date(created_at) AS day, COUNT(*) AS n FROM signups WHERE created_at >= datetime('now','-14 days') GROUP BY day ORDER BY day")
  });
}

// Country list offered on the page (OT = another country) and the plan values it can send
const COUNTRIES = ['IN', 'US', 'GB', 'CA', 'AE', 'SG', 'AU', 'OT'];
const PLANS = ['0', '699', '1499', 'usd_15', 'usd_29'];

const EXPORT_COLS = ['id', 'created_at', 'email', 'name', 'language', 'language_other', 'page_lang', 'variant', 'device',
  'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term', 'referrer', 'landing', 'country', 'region', 'city',
  'ref_code', 'referred_by', 'consent_at', 'consent_version', 'marketing', 'write_types', 'stage', 'wtp', 'plan_at', 'blocker', 'notes',
  'whatsapp', 'profile_at', 'chosen_country'];
async function exportCsv(env) {
  const { results } = await env.DB.prepare(`SELECT ${EXPORT_COLS.join(', ')} FROM signups ORDER BY id`).all();
  const cell = v => {
    let s = v == null ? '' : String(v);
    if (/^[=+\-@]/.test(s)) s = "'" + s;                 // block spreadsheet formula injection
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const body = [EXPORT_COLS.join(','), ...results.map(r => EXPORT_COLS.map(c => cell(r[c])).join(','))].join('\n');
  return new Response(body, { headers: { 'content-type': 'text/csv; charset=utf-8', 'cache-control': 'no-store', 'x-robots-tag': 'noindex', 'content-disposition': 'inline; filename="dictationer-waitlist.csv"' } });
}

async function sendWelcome(env, email, code, lang) {
  const site = (env.SITE_URL || '').replace(/\/$/, '');
  const link = `${site}/?lang=${lang}&ref=${code}&utm_source=referral&utm_medium=email`;
  const hi = lang === 'hi';
  const subject = hi ? 'स्वागत है, फ़र्स्ट एडिशन लेखक' : 'Welcome, First Edition writer';
  const text = hi
    ? `नमस्ते,\n\nआप डिक्टेशनर की फ़र्स्ट एडिशन सूची में जुड़ गए हैं। लॉन्च पर सबसे पहले आपको मौका मिलेगा।\n\nएक सवाल: वो कौन-सी एक बात है जो आपको अपनी किताब पूरी करने से रोकती है? बस इस ईमेल का जवाब दीजिए, हम हर जवाब पढ़ते हैं।\n\nसाथी लेखकों को बुलाइए, हर एक आपको 10 पन्ने आगे ले आता है:\n${link}\n\nअपना डेटा हटवाने या सहमति वापस लेने के लिए इस ईमेल का जवाब दें।\n— डिक्टेशनर टीम`
    : `Hello,\n\nYou're on the Dictationer First Edition list. First Edition writers get access before anyone else.\n\nOne question: what is the one thing that stops you finishing your book? Just reply to this email; we read every answer.\n\nInvite co-writers. Each one moves you 10 pages closer to the front:\n${link}\n\nTo delete your data or withdraw consent, reply to this email.\n— The Dictationer team`;
  await fetch('https://api.brevo.com/v3/smtp/email', {
    method: 'POST',
    headers: { 'api-key': env.BREVO_API_KEY, 'content-type': 'application/json', accept: 'application/json' },
    body: JSON.stringify({
      sender: { name: hi ? 'डिक्टेशनर' : 'Dictationer', email: env.SENDER_EMAIL },
      to: [{ email }], replyTo: { email: env.REPLY_TO || env.SENDER_EMAIL }, subject, textContent: text
    })
  });
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, '');
    try {
      if (request.method === 'POST' && !sameOrigin(request, url)) return json({ ok: false, error: 'forbidden' }, 403);
      if (path === '/api/join' && request.method === 'POST') return await join(request, env, ctx);
      if (path === '/api/profile' && request.method === 'POST') return await profile(request, env);
      if (path === '/api/plan' && request.method === 'POST') return await plan(request, env);
      if (path === '/api/me' && request.method === 'POST') return await me(request, env);
      if (path === '/api/admin/stats' && request.method === 'GET') return authorised(request, url, env) ? await stats(env) : json({ ok: false, error: 'forbidden' }, 403);
      if (path === '/api/admin/export.csv' && request.method === 'GET') return authorised(request, url, env) ? await exportCsv(env) : new Response('forbidden', { status: 403 });
      // Where the visitor appears to be, from Cloudflare's request data; the page uses it to preselect the country
      if (path === '/api/geo' && request.method === 'GET') return json({ country: clean((request.cf || {}).country, 2).toUpperCase() });
      if (path === '/api/health') return json({ ok: true });
      if (path.startsWith('/api')) return json({ ok: false, error: 'not_found' }, 404);
      return env.ASSETS.fetch(request);
    } catch (e) {
      console.error('api_error', path, String(e && e.stack || e));
      return json({ ok: false, error: 'server_error' }, 500);
    }
  }
};
