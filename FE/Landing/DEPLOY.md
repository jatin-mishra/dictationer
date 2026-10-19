# Deploying the Dictationer waitlist on Cloudflare (free)

Everything runs on one Cloudflare Worker, on the free plan:

```
Writer's phone ──> Worker (dictationer-waitlist)
                    ├─ /            the landing page from ./public   (static assets: free, unlimited)
                    ├─ /api/join    step-1 signup ──┐
                    ├─ /api/profile step-2 answers ─┼─> D1 database "dictationer-waitlist"
                    ├─ /api/plan    plan picked   ─┤
                    ├─ /api/me      own place now ─┤   (5M row reads + 100k row writes a day free)
                    └─ /api/admin/*  owner only ──┘──> Google Sheet via =IMPORTDATA() for your dashboard
                                      optional ──> Brevo: confirmation email in the writer's language
```

Cloudflare now recommends Workers static assets (not Pages) for new sites, so this uses Workers.
Free-plan limits that matter: 100,000 Worker requests a day (only `/api/*` calls count; the page itself is free),
10 ms CPU per request (each signup uses a fraction of that), D1 5 GB storage.

---

## 1. One-time setup (15 minutes)

You need a free Cloudflare account and Node.js 20 or newer.

```bash
cd dictationer-waitlist         # this folder
npm install                     # installs Wrangler, Cloudflare's CLI
npx wrangler login              # opens the browser; approve access to your account
```

## 2. Create the database (2 minutes)

```bash
npx wrangler d1 create dictationer-waitlist
```

It prints a `database_id`. Paste it into `wrangler.jsonc`, replacing `PASTE_THE_ID_FROM_wrangler_d1_create`.
Then create the table:

```bash
npm run db:init                 # runs schema.sql against the live database
```

## 3. Set your secret export key (1 minute)

This key is the only way to read the data over the web. It must be at least 24 characters (shorter keys are refused). Make it long and random:

```bash
openssl rand -hex 24            # copy the output
npx wrangler secret put EXPORT_KEY   # paste it when asked
```

## 3b. Who can see the data

| Who | What they can do |
| --- | --- |
| A visitor | Add their own signup, then save their own plan choice and book details using a one-time token their browser receives. They can't read anything back. Re-entering an email that is already on the list reveals nothing about that person. |
| Another website | Nothing: form posts from any other site are refused. |
| You | Everything, three ways: the Cloudflare dashboard (open D1, pick dictationer-waitlist, use its Console), `npx wrangler d1 execute ... --remote`, or `/api/admin/*` with your key. |

Rules to keep it that way:
- Never put `EXPORT_KEY` in `wrangler.jsonc`, the page, or a public Sheet. It lives only as a Wrangler secret.
- Prefer the header form for checks: `curl -H "Authorization: Bearer <EXPORT_KEY>" https://<your-site>/api/admin/stats`. The `?key=` form exists only for Google Sheets; keep that Sheet private.
- If the key ever leaks, rotate it: `npx wrangler secret put EXPORT_KEY` with a new value (the old one stops working immediately), then update the Sheet formula.
- Turn on two-factor login for your Cloudflare and Google accounts.
- Extra lock (optional, free): Cloudflare Zero Trust → Access → add a self-hosted application for `<your-site>/api/admin/*`, allowing only your email. Browsers then need your login as well as the key. Note that Google Sheets IMPORTDATA can't pass Access, so use the D1 console or wrangler for data if you turn this on.

## 4. Try it on your laptop first (5 minutes)

```bash
echo "EXPORT_KEY=\"$(openssl rand -hex 24)\"" > .dev.vars   # local-only key
npm run db:init:local
npm run dev                     # open http://localhost:8787
```

Sign up once in English and once in Hindi (switch with the dropdown) and pick a plan, then check
`http://localhost:8787/api/admin/stats?key=<the key in .dev.vars>`. You should see 2 signups and 2 plan answers.

## 5. Deploy (1 minute)

```bash
npm run deploy
```

Wrangler prints your live address, for example `https://dictationer-waitlist.<your-subdomain>.workers.dev`.
Put that address in two places, then deploy once more:

- `wrangler.jsonc` → `vars.SITE_URL`
- `public/index.html` → `CONFIG.SITE_URL` (used in the WhatsApp share links)

```bash
npm run deploy
```

## 6. Check it live (5 minutes)

- [ ] `https://<your-site>/api/health` returns `{"ok":true}`
- [ ] Open the site on your phone, sign up, see "You're on page 1"
- [ ] Open your referral link in a private window, sign up with another email: the first email moves up
- [ ] `https://<your-site>/?lang=hi` opens fully in Hindi
- [ ] `https://<your-site>/api/admin/stats?key=<EXPORT_KEY>` shows the signups
- [ ] Delete test rows: `npx wrangler d1 execute dictationer-waitlist --remote --command "DELETE FROM signups"`

## 7. Your own domain (before you share widely)

Cloudflare says `workers.dev` addresses are meant for hobby projects, and a real domain also looks more trustworthy
in WhatsApp groups. In the dashboard: **Workers & Pages → dictationer-waitlist → Domains → Add domain**.
You can buy the domain there through Cloudflare Registrar (at cost) or connect one you already own.
Then update `SITE_URL` in both places from step 5 and deploy again.

## 8. Dashboard in Google Sheets (10 minutes)

1. New Google Sheet, tab named `raw`. In cell A1:
   `=IMPORTDATA("https://<your-site>/api/admin/export.csv?key=<EXPORT_KEY>")`
2. Build your formulas on other tabs, pointing at `raw` (columns: B created_at, C email, E language,
   G page_lang, H variant, J utm_source, U referred_by, Z stage, AA wtp (plan picked), AB plan_at, AF profile_at, AG chosen_country). For example:
   - Daily signups (dates in column A of your tab): `=COUNTIFS(raw!$B:$B,">="&A2,raw!$B:$B,"<"&A2+1)`
   - Paid intent: `=COUNTIFS(raw!AA:AA,"<>0",raw!AA:AA,"?*")/COUNTA(raw!AA2:AA)`
   - By language: `=QUERY(raw!A:AE,"select E, count(C) where C is not null group by E order by count(C) desc",1)`
   - Hindi headline test: `=QUERY(raw!A:AE,"select H, count(C) where G = 'hi' group by H",1)`
   - Profile completion: `=COUNTA(raw!AF2:AF)/COUNTA(raw!C2:C)`
3. Keep this Sheet private: the formula contains your key. Google refreshes IMPORTDATA about once an hour;
   for a live number, open `/api/admin/stats?key=...`.

## 9. Confirmation email (optional, 15 minutes)

1. Free Brevo account → verify a sender address on your domain (Brevo walks you through SPF/DKIM).
2. Brevo → SMTP & API → create an API key.
3. Add both as secrets and deploy:

```bash
npx wrangler secret put BREVO_API_KEY
npx wrangler secret put SENDER_EMAIL     # e.g. hello@yourdomain.in
npm run deploy
```

Brevo's free plan sends 300 emails a day. Without these secrets the site works and simply skips the email.

## 9b. Upgrading a live site to the country picker (once)

Sites created before the country picker need one extra column. Run this once, then deploy:

```bash
npx wrangler d1 execute dictationer-waitlist --remote --command "ALTER TABLE signups ADD COLUMN chosen_country TEXT"
npm run deploy
```

How the country picker works:
- The page preselects the visitor's country from Cloudflare's request data (`/api/geo`); the visitor can change it, and the choice is remembered.
- India shows rupee prices (₹0 / ₹699 / ₹1,499). Every other country shows dollar prices ($0 / $15 / $29).
- `chosen_country` holds what they picked; `country` holds where Cloudflare saw them. Plan answers are stored as `0`, `699`, `1499` (rupees) or `usd_15`, `usd_29` (dollars).
- Links can force a country: `?country=US`, `?country=GB`, `?country=IN` (codes: IN US GB CA AE SG AU OT).

## 10. Day-to-day

| Task | Command or place |
| --- | --- |
| Change copy or design | Edit `public/index.html` (all English and Hindi text is in the `T` object), then `npm run deploy` |
| See totals now | `https://<your-site>/api/admin/stats?key=<EXPORT_KEY>` |
| Weekly backup | `npx wrangler d1 export dictationer-waitlist --remote --output=backup-$(date +%F).sql` |
| Someone asks to be deleted | `npx wrangler d1 execute dictationer-waitlist --remote --command "DELETE FROM signups WHERE email='name@example.com'"` |
| Errors | Dashboard → Workers & Pages → dictationer-waitlist → Logs (observability is on in `wrangler.jsonc`) |
| ₹99 reserve test | Create a Razorpay or UPI payment link, paste it into `CONFIG.RESERVE_URL`, deploy |
| Page visitors | Dashboard → Analytics → Web Analytics for your domain, or set `CONFIG.UMAMI_ID` |

## Links to share

- English channels: `https://<your-site>/?lang=en&utm_source=fb&utm_medium=group&utm_campaign=launch`
- Hindi channels: `https://<your-site>/?lang=hi&utm_source=fb&utm_medium=group&utm_campaign=hindi-typing`
- US / global English: `https://<your-site>/?lang=en&country=US&utm_source=reddit&utm_medium=post&utm_campaign=global`
- Hindi speakers abroad: `https://<your-site>/?lang=hi&country=US&utm_source=fb&utm_medium=group&utm_campaign=diaspora`

The `lang` and `country` in the link decide the language and prices the page opens with; the visitor can still switch both.
