"""RSS/Atom status-page watcher with Discord webhook delivery."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import feedparser
import requests
import yaml
from dotenv import load_dotenv

DISCORD_SAFE_LIMIT = 1900
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_ITEMS_PER_FEED = 5
DEFAULT_MAX_SEEN_PER_FEED = 100


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    enabled: bool = True


@dataclass(frozen=True)
class FeedEntry:
    item_id: str
    title: str
    link: str | None = None
    published: str | None = None
    summary: str | None = None


@dataclass(frozen=True)
class WatcherConfig:
    dry_run: bool = False
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_items_per_feed: int = DEFAULT_MAX_ITEMS_PER_FEED
    max_seen_per_feed: int = DEFAULT_MAX_SEEN_PER_FEED
    webhook_url: str | None = None
    sleep_between_messages_seconds: float = 0.5


@dataclass
class RunResult:
    baseline_created: bool = False
    baselined_feeds: int = 0
    feeds_processed: int = 0
    entries_seen: int = 0
    new_items: int = 0
    sent: int = 0
    dry_run: int = 0
    failed: int = 0
    feed_errors: int = 0
    errors: list[str] = field(default_factory=list)


class JsonSeenStore:
    def __init__(self, path: Path):
        self.path = path

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> dict[str, list[str]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON state file: {self.path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"State file must contain a JSON object: {self.path}")
        normalized: dict[str, list[str]] = {}
        for feed_url, ids in data.items():
            if isinstance(feed_url, str) and isinstance(ids, list):
                normalized[feed_url] = [str(item_id) for item_id in ids]
        return normalized

    def save(self, state: dict[str, list[str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp_path.replace(self.path)


def parse_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_feeds(path: Path) -> list[Feed]:
    if not path.exists():
        raise FileNotFoundError(f"Feed config not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_feeds = data.get("feeds")
    if not isinstance(raw_feeds, list):
        raise ValueError("feeds.yaml must contain a top-level 'feeds' list")

    feeds: list[Feed] = []
    for index, item in enumerate(raw_feeds, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Feed #{index} must be an object")
        name = str(item.get("name", "")).strip()
        url = str(item.get("url", "")).strip()
        enabled = parse_bool(item.get("enabled"), default=True)
        if not name:
            raise ValueError(f"Feed #{index} missing required name")
        if not url:
            raise ValueError(f"Feed #{index} missing required url")
        if enabled:
            feeds.append(Feed(name=name, url=url, enabled=True))
    return feeds


def entry_identity(entry: dict[str, Any]) -> str:
    for key in ("id", "guid", "link"):
        value = entry.get(key)
        if value:
            return str(value)
    basis = f"{entry.get('title', '')}|{entry.get('published', '')}|{entry.get('updated', '')}"
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def strip_html(text: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", text)
    unescaped = html.unescape(no_tags)
    return re.sub(r"\s+", " ", unescaped).strip()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def build_message(feed_name: str, entry: FeedEntry) -> str:
    title = strip_html(entry.title or "Untitled status update")
    lines = [f"🚨 [{feed_name}] {title}", "", "Status: New RSS status update"]
    if entry.published:
        lines.append(f"Published: {strip_html(entry.published)}")
    if entry.link:
        lines.append(f"Link: {entry.link}")
    if entry.summary:
        summary = truncate(strip_html(entry.summary), 500)
        if summary:
            lines.extend(["", f"Summary: {summary}"])
    return truncate("\n".join(lines), DISCORD_SAFE_LIMIT)


def parsed_entry_to_feed_entry(entry: dict[str, Any]) -> FeedEntry:
    return FeedEntry(
        item_id=entry_identity(entry),
        title=str(entry.get("title") or "Untitled status update"),
        link=str(entry.get("link")) if entry.get("link") else None,
        published=str(entry.get("published") or entry.get("updated") or "") or None,
        summary=str(entry.get("summary") or entry.get("description") or "") or None,
    )


def fetch_feed_entries(feed: Feed, config: WatcherConfig) -> list[FeedEntry]:
    response = requests.get(feed.url, timeout=config.timeout_seconds, headers={"User-Agent": "rss-status-watcher/0.1"})
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
    if getattr(parsed, "bozo", False):
        # feedparser can set bozo for recoverable feeds; entries may still be valid.
        if not getattr(parsed, "entries", None):
            raise ValueError(f"Could not parse feed: {feed.url}: {getattr(parsed, 'bozo_exception', 'unknown error')}")
    return [parsed_entry_to_feed_entry(dict(entry)) for entry in parsed.entries[: config.max_items_per_feed]]


def trim_seen(ids: Iterable[str], limit: int) -> list[str]:
    unique_in_order: list[str] = []
    seen: set[str] = set()
    for item_id in ids:
        if item_id in seen:
            continue
        unique_in_order.append(item_id)
        seen.add(item_id)
    return unique_in_order[-limit:]


def send_discord_webhook(message: str, config: WatcherConfig) -> bool:
    if not config.webhook_url:
        raise ValueError("DISCORD_WEBHOOK_URL is required unless --dry-run/DRY_RUN=true is used")
    payload = {"content": message, "allowed_mentions": {"parse": []}}
    try:
        response = requests.post(config.webhook_url, json=payload, timeout=config.timeout_seconds)
    except requests.RequestException as exc:
        print(f"Discord delivery failed: {exc}", file=sys.stderr)
        return False
    if response.status_code == 429:
        retry_after = 1.0
        try:
            retry_after = float(response.json().get("retry_after", retry_after))
        except Exception:
            retry_after = float(response.headers.get("Retry-After", retry_after))
        time.sleep(min(max(retry_after, 0.0), 30.0))
        try:
            response = requests.post(config.webhook_url, json=payload, timeout=config.timeout_seconds)
        except requests.RequestException as exc:
            print(f"Discord delivery failed after rate-limit retry: {exc}", file=sys.stderr)
            return False
    if 200 <= response.status_code < 300:
        return True
    print(f"Discord delivery failed: HTTP {response.status_code}: {response.text[:500]}", file=sys.stderr)
    return False


def run_once(
    *,
    feeds: list[Feed],
    store: JsonSeenStore,
    config: WatcherConfig,
    fetcher: Callable[[Feed], list[FeedEntry]] | None = None,
    sender: Callable[[str], bool] | None = None,
) -> RunResult:
    fetcher = fetcher or (lambda feed: fetch_feed_entries(feed, config))
    sender = sender or (lambda message: send_discord_webhook(message, config))
    result = RunResult()
    first_run = not store.exists()
    state = store.load()
    changed = False

    for feed in feeds:
        result.feeds_processed += 1
        try:
            entries = fetcher(feed)
        except Exception as exc:
            result.feed_errors += 1
            result.errors.append(f"{feed.name}: {exc}")
            print(f"Feed fetch failed for {feed.name} ({feed.url}): {exc}", file=sys.stderr)
            continue

        result.entries_seen += len(entries)
        existing_ids = state.get(feed.url, [])
        existing_set = set(existing_ids)

        if first_run or feed.url not in state:
            state[feed.url] = trim_seen([*existing_ids, *(entry.item_id for entry in entries)], config.max_seen_per_feed)
            changed = True
            result.baselined_feeds += 1
            continue

        for entry in entries:
            if entry.item_id in existing_set:
                continue
            result.new_items += 1
            message = build_message(feed.name, entry)
            if config.dry_run:
                print(f"DRY RUN: would send alert for [{feed.name}] {entry.title}")
                delivered = True
                result.dry_run += 1
            else:
                try:
                    delivered = sender(message)
                except Exception as exc:
                    delivered = False
                    result.errors.append(f"{feed.name}: delivery failed for {entry.item_id}: {exc}")
                    print(f"Delivery failed for {feed.name} item {entry.item_id}: {exc}", file=sys.stderr)
                if delivered:
                    result.sent += 1
                    if config.sleep_between_messages_seconds > 0:
                        time.sleep(config.sleep_between_messages_seconds)
            if delivered:
                existing_ids.append(entry.item_id)
                existing_set.add(entry.item_id)
                state[feed.url] = trim_seen(existing_ids, config.max_seen_per_feed)
                changed = True
            else:
                result.failed += 1

    if first_run:
        result.baseline_created = True
    if changed or first_run:
        store.save(state)
    return result


def config_from_env(args: argparse.Namespace) -> WatcherConfig:
    load_dotenv(args.env_file)
    dry_run = args.dry_run or parse_bool(os.getenv("DRY_RUN"), default=False)
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    timeout_seconds = float(os.getenv("HTTP_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    max_items_per_feed = int(os.getenv("MAX_ITEMS_PER_FEED", DEFAULT_MAX_ITEMS_PER_FEED))
    max_seen_per_feed = int(os.getenv("MAX_SEEN_PER_FEED", DEFAULT_MAX_SEEN_PER_FEED))
    if not dry_run and not webhook_url:
        raise ValueError("DISCORD_WEBHOOK_URL is required unless --dry-run or DRY_RUN=true is set")
    return WatcherConfig(
        dry_run=dry_run,
        timeout_seconds=timeout_seconds,
        max_items_per_feed=max_items_per_feed,
        max_seen_per_feed=max_seen_per_feed,
        webhook_url=webhook_url,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Poll RSS/Atom status feeds and send Discord webhook alerts.")
    parser.add_argument("--feeds", type=Path, default=Path("feeds.yaml"), help="Path to feeds.yaml")
    parser.add_argument("--state", type=Path, default=Path("seen.json"), help="Path to seen.json state")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="Path to .env file")
    parser.add_argument("--dry-run", action="store_true", help="Print/send nothing to Discord; mark new items seen")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        feeds = load_feeds(args.feeds)
        config = config_from_env(args)
        result = run_once(feeds=feeds, store=JsonSeenStore(args.state), config=config)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if result.baseline_created:
        print(f"Baseline created: {result.entries_seen} entries recorded across {result.feeds_processed} feeds; no alerts sent.")
    else:
        print(
            "Run complete: "
            f"feeds={result.feeds_processed} entries={result.entries_seen} "
            f"new={result.new_items} sent={result.sent} dry_run={result.dry_run} "
            f"failed={result.failed} feed_errors={result.feed_errors}"
        )
    if result.errors:
        for error in result.errors:
            print(f"WARN: {error}", file=sys.stderr)
    return 0 if result.failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
