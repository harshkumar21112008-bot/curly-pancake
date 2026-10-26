# Deploy in 5 steps

1. **GitHub**: `git remote add origin https://github.com/<you>/competitor-monitor.git && git push -u origin main`
2. **Database**: create a free Supabase project, copy the *connection pooler* URI (port 6543).
3. **Render**: New > Blueprint > pick this repo (uses `render.yaml`).
4. **Env vars** on Render: `DATABASE_URL` (Supabase URI), `DATAFORSEO_LOGIN`, `DATAFORSEO_PASSWORD`, `SLACK_WEBHOOK_URL`.
   `DATAFORSEO_BASE` is preset to the free sandbox (dummy volumes). Change it to `https://api.dataforseo.com` for real volumes.
5. Open the Render URL, check `/api/health`, then run a domain.

Free Render services sleep after idle; open the link once before your demo.
