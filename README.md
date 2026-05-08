# RSS Status Watcher

Lightweight RSS/Atom status-page watcher that sends new status updates to a Discord webhook. It is meant for 10+ official status feeds without running a full Discord bot.

## What it does

- Polls enabled feeds from `feeds.yaml`.
- Creates a first-run baseline in `seen.json` without sending old incidents.
- Sends one Discord webhook message per new item.
- Marks an item seen only after successful delivery.
- Isolates feed failures so one broken feed does not stop the run.
- Handles Discord `429` by respecting `retry_after` once.
- Supports `--dry-run` / `DRY_RUN=true` for safe testing.

## Install

```bash
cd ~/dev/rss-status-watcher
uv sync
```

## Configure

```bash
cp .env.example .env
# edit .env and set DISCORD_WEBHOOK_URL
```

Never commit `.env` or `seen.json`.

## Run safely first

First run should baseline existing feed items and send nothing:

```bash
uv run python -m rss_status_watcher.watcher --dry-run
```

Expected first-run output:

```text
Baseline created: N entries recorded across M feeds; no alerts sent.
```

Note: dry-run uses the selected state file and records items as seen. If you want a completely disposable test, use a temporary state file:

```bash
uv run python -m rss_status_watcher.watcher --dry-run --state /tmp/rss-status-watcher-seen.json
```

## Live run

After `.env` contains a real webhook URL:

```bash
uv run python -m rss_status_watcher.watcher
```

## Cron example

```cron
*/3 * * * * cd /home/hojinjang/dev/rss-status-watcher && /home/hojinjang/.local/bin/uv run python -m rss_status_watcher.watcher >> watcher.log 2>&1
```

## systemd user timer example

`~/.config/systemd/user/rss-status-watcher.service`:

```ini
[Unit]
Description=RSS Status Watcher

[Service]
Type=oneshot
WorkingDirectory=/home/hojinjang/dev/rss-status-watcher
ExecStart=/home/hojinjang/.local/bin/uv run python -m rss_status_watcher.watcher
```

`~/.config/systemd/user/rss-status-watcher.timer`:

```ini
[Unit]
Description=Run RSS Status Watcher every 3 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=3min
Persistent=true

[Install]
WantedBy=timers.target
```

Enable later only after `.env` is configured:

```bash
systemctl --user daemon-reload
systemctl --user enable --now rss-status-watcher.timer
systemctl --user list-timers rss-status-watcher.timer
```

## Feed config

Enabled starter feeds:

- OpenAI: `https://status.openai.com/history.rss`
- Anthropic Claude: `https://status.anthropic.com/history.rss`
- Slack: `https://status.slack.com/feed/rss`
- Discord: `https://status.discord.com/history.rss`
- GitHub: `https://www.githubstatus.com/history.rss`

Additional candidates are present in `feeds.yaml` with `enabled: false`. Verify before enabling.

## Development

```bash
uv run pytest
```

## Exit codes

- `0`: success or feed-only warnings without failed deliveries.
- `1`: configuration/runtime error before processing.
- `2`: at least one new item failed Discord delivery and remains unseen for retry.
