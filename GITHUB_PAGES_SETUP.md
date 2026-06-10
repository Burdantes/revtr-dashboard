# Deploying the revTr Health Monitor to GitHub Pages

## What this is

A live dashboard monitoring reverse traceroute (revTr) health for M-Lab physical sites. It shows:
- Daily measurement volume, reach rate, fail rate
- Hop quality metrics (interdomain assumption, fishy type 4)
- An interactive map of measurements per M-Lab site
- 7-day trend charts and daily breakdown tables

The dashboard is a single static HTML page that calls a backend API running on a GCE VM for BigQuery data.

## Architecture

```
GitHub Pages (static HTML)  -->  GCE VM API (136.116.232.100:5050)  -->  BigQuery (measurement-lab.revtr_raw.revtr1)
```

- **Frontend**: `index.html` — standalone HTML/CSS/JS page, no build step
- **Backend**: Flask API running in Docker on a GCE VM in project `nsf-2148275-66720`
- **Data**: Queries BigQuery `measurement-lab.revtr_raw.revtr1`, excludes virtual sites (34.x destinations)
- The page auto-refreshes every hour

## Setup steps

1. **Copy `index.html`** from `code/analysis/revtr_monitoring/index.html` into your GitHub Pages repository (e.g., at the root or under a subdirectory like `revtr/`).

2. **Verify the API_BASE URL** in `index.html` points to the running backend:
   ```js
   const API_BASE = 'http://136.116.232.100:5050';
   ```

3. **Commit and push** to your GitHub Pages branch (typically `main` or `gh-pages`).

4. The page will be live at your GitHub Pages URL (e.g., `https://<username>.github.io/revtr/`).

## API endpoints consumed by the page

All served by the Flask backend at `API_BASE`:

| Endpoint | Returns |
|---|---|
| `/api/health` | Daily volume, reach/fail rates, alert status, 7-day breakdown |
| `/api/ping` | revTr API liveness check (sources count) |
| `/api/queries_today` | Number of revTr measurements today |
| `/api/hop_quality` | Fraction of interdomain assumption + fishy type 4 hops (physical sites, reaching only) |
| `/api/sites` | Per-M-Lab-site measurement counts with lat/lng from sites.json |

## Backend details (for reference)

- **VM**: `revtr-dashboard` in GCE project `nsf-2148275-66720`, zone `us-central1-a`
- **Container**: Docker image `revtr-monitor`, auto-restarts, listens on port 5050
- **Auth**: Uses mounted Application Default Credentials (`loqman@measurementlab.net`)
- **Firewall**: Rule `allow-revtr-dashboard` opens TCP 5050
- **Source code**: `code/analysis/revtr_monitoring/app.py` + `templates/dashboard.html`

To SSH into the VM:
```bash
gcloud compute ssh revtr-dashboard \
  --project=nsf-2148275-66720 \
  --account=ls3748@cloudbank.org \
  --zone=us-central1-a
```

To view container logs:
```bash
sudo docker logs revtr-monitor
```

To rebuild after code changes:
```bash
# From local machine, copy updated files:
gcloud compute scp app.py revtr-dashboard:~/revtr-monitor/ --zone=us-central1-a --project=nsf-2148275-66720 --account=ls3748@cloudbank.org
gcloud compute scp -r templates revtr-dashboard:~/revtr-monitor/ --zone=us-central1-a --project=nsf-2148275-66720 --account=ls3748@cloudbank.org

# SSH in and rebuild:
cd ~/revtr-monitor
sudo docker build -t revtr-monitor .
sudo docker stop revtr-monitor && sudo docker rm revtr-monitor
sudo docker run -d --name revtr-monitor --restart=unless-stopped \
  -p 5050:5050 -e PORT=5050 -e BQ_PROJECT=measurement-lab \
  -e GOOGLE_APPLICATION_CREDENTIALS=/creds/adc.json \
  -v /home/loqmansalamatian/adc.json:/creds/adc.json:ro \
  revtr-monitor
```
