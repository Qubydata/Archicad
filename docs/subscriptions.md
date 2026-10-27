# Subscriptions: sign-in, billing and included AI

With the cloud service running, users don't need an Anthropic API key. They sign in
in the panel with their e-mail and a 6-digit code, start a subscription through Stripe,
and the AI is included up to a monthly allowance. You pay Anthropic for the AI, your
subscribers pay you.

```
 Archicad MCP panel ──sign-in, account──▶  archicad-mcp-cloud  ──▶ Stripe (Checkout, Portal, webhooks)
        │                                        │
        └──── Claude requests (device token) ───▶├──▶ Anthropic API (your key), cost recorded per request
```

- **Sign-in:** e-mail plus a one-time 6-digit code (10 minutes, 5 attempts). Each signed-in
  computer gets its own device token, stored hashed on the server.
- **Billing:** Stripe Checkout starts the subscription, with a free trial the first time. The
  Stripe Customer Portal handles card changes, invoices and cancelling. Stripe webhooks keep each
  account's status and billing period up to date.
- **Included AI:** the panel sends Claude requests to the service with its device token. The
  service checks the subscription and the period's allowance, forwards the request to Anthropic
  with your key, streams the answer back unchanged and records its cost at Anthropic's list
  prices. Once the allowance is used, requests are refused with a clear message until the next
  period. Only the models in `ALLOWED_MODELS` and the app's own tools are accepted (no web search
  or other server tools on your bill).
- **Own key still works:** under Settings > "Use your own Anthropic API key instead". Those
  requests go straight to Anthropic.

## 1. Stripe

1. Create a Stripe account. Use **test mode** until everything works.
2. **Product catalog > Add product**, for example "Archicad MCP Pro", with a **recurring monthly
   price**. Copy the price ID (`price_...`).
3. **Settings > Billing > Customer portal:** activate it, and allow cancelling and updating
   payment methods.
4. **Developers > Webhooks > Add endpoint:** `https://<your service>/v1/stripe/webhook`, with the
   events `checkout.session.completed`, `customer.subscription.created`,
   `customer.subscription.updated` and `customer.subscription.deleted`. Copy the signing secret
   (`whsec_...`).
5. Copy the secret API key (`sk_test_...`, later `sk_live_...`).

## 2. E-mail for sign-in codes

The simplest option is [Resend](https://resend.com): verify your domain, create an API key, and
set `EMAIL_PROVIDER=resend`, `RESEND_API_KEY` and `EMAIL_FROM` (for example
`Archicad MCP <login@yourdomain.com>`). Any SMTP server also works (`EMAIL_PROVIDER=smtp`).

## 3. Run the service

Settings (environment variables):

| Variable | Example | Notes |
| --- | --- | --- |
| `PUBLIC_URL` | `https://cloud.yourdomain.com` | used in Stripe's return links |
| `SECRET_KEY` | 64 random characters | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `ANTHROPIC_API_KEY` | `sk-ant-...` | your organization's key; set a spend limit in the Anthropic Console |
| `STRIPE_SECRET_KEY` | `sk_live_...` | |
| `STRIPE_WEBHOOK_SECRET` | `whsec_...` | |
| `STRIPE_PRICE_ID` | `price_...` | |
| `TRIAL_DAYS` | `7` | `0` for no trial |
| `MONTHLY_ALLOWANCE_USD` | `20` | AI cost per subscriber per period, at list prices |
| `TRIAL_ALLOWANCE_USD` | `3` | AI cost during the trial |
| `ALLOWED_MODELS` | `claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5` | |
| `EMAIL_PROVIDER`, `RESEND_API_KEY`, `EMAIL_FROM` | | or `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` |
| `DATABASE_PATH` | `/data/cloud.db` | SQLite file, on a persistent disk |

The service is a small Python web app with one SQLite file. Any host that runs a Docker image
and keeps a disk works (Fly.io, Render, Railway, a VPS). With [Fly.io](https://fly.io):

```bash
fly launch --no-deploy                  # uses the Dockerfile in the repository root
fly volumes create data --size 1        # then add [mounts] source="data" destination="/data" to fly.toml
fly secrets set SECRET_KEY=... ANTHROPIC_API_KEY=... STRIPE_SECRET_KEY=... STRIPE_WEBHOOK_SECRET=... \
  STRIPE_PRICE_ID=... EMAIL_PROVIDER=resend RESEND_API_KEY=... EMAIL_FROM="Archicad MCP <login@yourdomain.com>" \
  PUBLIC_URL=https://<app>.fly.dev
fly deploy
```

Run `archicad-mcp-cloud check` on the server to list missing settings. Locally:
`uv run archicad-mcp-cloud serve` (with `EMAIL_PROVIDER=console`, sign-in codes appear in the log).

## 4. Point the app at the service

Set the repository variable **ARCHICAD_MCP_CLOUD_URL** (GitHub: Settings > Secrets and variables >
Actions > Variables) to the service's address. The installer build then puts it into the app, and
the panel offers sign-in. For local builds: `ARCHICAD_MCP_CLOUD_URL=https://... python packaging/build.py`.
For trying it without rebuilding, start the panel with the same environment variable.

## 5. Day to day

```bash
archicad-mcp-cloud user anna@studio.com       # status, billing period and AI cost so far
archicad-mcp-cloud comp tester@studio.com 30  # free access for 30 days (beta testers, partners)
```

## Pricing your plan

Your cost per subscriber is at most the allowance, plus Stripe's fee (about 2.9% + 30 cents per
charge in the US) and a little hosting. For example, at $39 per month with a $20 allowance, a
subscriber who uses everything leaves you about $17; most use less. As a rough guide with Claude
Opus 5.5, a short question costs one to a few cents and building a room over several tool calls
roughly 5 to 35 cents, depending on how much of the prompt is cached. Watch the real numbers with
`archicad-mcp-cloud user` during the beta before fixing your price. Sonnet 5.5 costs about half as
much, Haiku 5.5 a small fraction.

## Before going live

- Switch Stripe and the Anthropic key to live mode, and set a monthly spend limit on the
  Anthropic organization as a safety net.
- Publish terms of service and a privacy policy; the checkout and the sign-in e-mail can link to them.
- Review Anthropic's [Commercial Terms](https://www.anthropic.com/legal/commercial-terms) and
  [Usage Policy](https://www.anthropic.com/legal/aup): you offer Claude to your customers, so their
  use has to follow the policy too.
- Back up the SQLite file (it holds the accounts and the usage records).
