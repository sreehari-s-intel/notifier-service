# Kibana Keyword Notifier

Polls Elasticsearch, filters by keywords, sends alerts via Email + Microsoft Teams.

## Project Structure

```
kibana-notifier/
├── main.py                    # Entry point / scheduler
├── requirements.txt
├── config/
│   └── config.yaml            # All configuration here
├── core/
│   ├── elasticsearch_client.py  # ES connection + queries
│   └── dedup.py               # Duplicate alert prevention
├── notifiers/
│   ├── email_notifier.py      # SMTP email alerts
│   └── teams_notifier.py      # Microsoft Teams webhook alerts
└── logs/
    └── notifier.log           # Auto-created on first run
```

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure `config/config.yaml`

**Elasticsearch:**
```yaml
elasticsearch:
  host: "https://your-es-host:9200"
  username: "elastic"
  password: "your-password"
  index: "logs-*"
  poll_interval_seconds: 60
  lookback_minutes: 2
```

**Keywords:**
```yaml
keywords:
  - "ERROR"
  - "CRITICAL"
  - "OutOfMemory"
```

**Email (Gmail example):**
```yaml
email:
  enabled: true
  smtp_host: "smtp.gmail.com"
  smtp_port: 587
  use_tls: true
  sender: "you@gmail.com"
  password: "your-app-password"   # Use Gmail App Password, not account password
  recipients:
    - "team@company.com"
```
> For Gmail: enable 2FA → generate App Password at https://myaccount.google.com/apppasswords

**Microsoft Teams:**
1. In Teams: channel → `...` → Connectors → Incoming Webhook → Create
2. Copy the webhook URL
```yaml
teams:
  enabled: true
  webhook_url: "https://outlook.office.com/webhook/YOUR_URL"
```

### 3. Run
```bash
python main.py
```

## How It Works

1. Every `poll_interval_seconds`, queries ES for docs from the last `lookback_minutes`
2. Filters for any doc containing at least one keyword (multi-field phrase match)
3. Deduplicates — same hit won't trigger repeat alert within `dedup_window_seconds`
4. Sends Email + Teams card for all new matches

## Run as a Service (Linux)

Create `/etc/systemd/system/kibana-notifier.service`:
```ini
[Unit]
Description=Kibana Keyword Notifier
After=network.target

[Service]
WorkingDirectory=/path/to/kibana-notifier
ExecStart=/usr/bin/python3 main.py
Restart=always

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable kibana-notifier
sudo systemctl start kibana-notifier
```
