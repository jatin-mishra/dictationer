# Dictationer waitlist

One-page bilingual (English / हिन्दी) waitlist site with a country picker (₹ for India, $ elsewhere) plus its backend, on one Cloudflare Worker with a D1 database.

| Path | What it is |
| --- | --- |
| `public/index.html` | The landing page. All copy for both languages is in the `T` object; `CONFIG` at the top of the script holds the settings |
| `public/privacy.html` | Privacy notice in both languages |
| `src/worker.js` | API: `/api/join`, `/api/plan`, `/api/profile`, `/api/me`, `/api/geo`, `/api/admin/stats`, `/api/admin/export.csv` |
| `schema.sql` | D1 table |
| `wrangler.jsonc` | Cloudflare config |
| `DEPLOY.md` | Step-by-step deployment guide |

Open `public/index.html` straight from disk to see it in preview mode (signups aren't saved).
Run `npm run dev` for the real thing locally. See `DEPLOY.md` to go live.
