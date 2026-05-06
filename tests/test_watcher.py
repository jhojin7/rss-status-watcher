from __future__ import annotations

import json
from pathlib import Path

import pytest

from rss_status_watcher.watcher import (
    Feed,
    FeedEntry,
    JsonSeenStore,
    WatcherConfig,
    build_message,
    entry_identity,
    load_feeds,
    trim_seen,
    run_once,
)


def test_load_feeds_defaults_enabled_and_skips_disabled(tmp_path: Path):
    config_path = tmp_path / "feeds.yaml"
    config_path.write_text(
        """
feeds:
  - name: OpenAI
    url: https://status.openai.com/history.rss
  - name: Disabled
    url: https://example.invalid/feed.xml
    enabled: false
""".strip(),
        encoding="utf-8",
    )

    feeds = load_feeds(config_path)

    assert feeds == [Feed(name="OpenAI", url="https://status.openai.com/history.rss", enabled=True)]


def test_entry_identity_prefers_id_then_guid_then_link_then_hash():
    assert entry_identity({"id": "id-1", "guid": "guid-1", "link": "https://x"}) == "id-1"
    assert entry_identity({"guid": "guid-1", "link": "https://x"}) == "guid-1"
    assert entry_identity({"link": "https://x"}) == "https://x"

    fallback = entry_identity({"title": "Incident", "published": "2026-05-06"})

    assert fallback.startswith("sha256:")
    assert fallback == entry_identity({"title": "Incident", "published": "2026-05-06"})


def test_build_message_strips_html_and_stays_under_discord_limit():
    entry = FeedEntry(
        item_id="1",
        title="Elevated errors",
        link="https://status.example/incidents/1",
        published="2026-05-06 01:00 UTC",
        summary="<p>" + "A" * 3000 + "</p>",
    )

    message = build_message("OpenAI", entry)

    assert message.startswith("🚨 [OpenAI] Elevated errors")
    assert "<p>" not in message
    assert "Link: https://status.example/incidents/1" in message
    assert len(message) <= 1900


def test_json_seen_store_missing_file_baselines_empty_and_saves(tmp_path: Path):
    state_path = tmp_path / "seen.json"
    store = JsonSeenStore(state_path)

    assert store.load() == {}

    store.save({"https://feed": ["a", "b"]})

    assert json.loads(state_path.read_text(encoding="utf-8")) == {"https://feed": ["a", "b"]}


def test_trim_seen_keeps_newest_unique_ids():
    assert trim_seen(["a", "b", "a", "c", "d"], limit=3) == ["b", "c", "d"]


def test_first_run_seeds_seen_without_sending(tmp_path: Path):
    state_path = tmp_path / "seen.json"
    sent: list[str] = []

    def fetcher(feed: Feed):
        return [
            FeedEntry(item_id="old-1", title="Old incident", link="https://x/1"),
            FeedEntry(item_id="old-2", title="Old incident 2", link="https://x/2"),
        ]

    def sender(message: str):
        sent.append(message)
        return True

    result = run_once(
        feeds=[Feed(name="OpenAI", url="https://feed")],
        store=JsonSeenStore(state_path),
        config=WatcherConfig(dry_run=False, max_seen_per_feed=100),
        fetcher=fetcher,
        sender=sender,
    )

    assert result.baseline_created is True
    assert result.sent == 0
    assert sent == []
    assert json.loads(state_path.read_text(encoding="utf-8")) == {"https://feed": ["old-1", "old-2"]}


def test_new_item_sends_and_updates_seen_only_on_success(tmp_path: Path):
    state_path = tmp_path / "seen.json"
    state_path.write_text(json.dumps({"https://feed": ["old"]}), encoding="utf-8")
    sent: list[str] = []

    def fetcher(feed: Feed):
        return [
            FeedEntry(item_id="old", title="Old", link="https://x/old"),
            FeedEntry(item_id="new-ok", title="New OK", link="https://x/ok"),
            FeedEntry(item_id="new-fail", title="New Fail", link="https://x/fail"),
        ]

    def sender(message: str):
        sent.append(message)
        return "New Fail" not in message

    result = run_once(
        feeds=[Feed(name="OpenAI", url="https://feed")],
        store=JsonSeenStore(state_path),
        config=WatcherConfig(dry_run=False, max_seen_per_feed=100),
        fetcher=fetcher,
        sender=sender,
    )

    assert result.sent == 1
    assert result.failed == 1
    assert len(sent) == 2
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["https://feed"] == ["old", "new-ok"]


def test_dry_run_marks_new_items_seen_without_calling_sender(tmp_path: Path):
    state_path = tmp_path / "seen.json"
    state_path.write_text(json.dumps({"https://feed": []}), encoding="utf-8")

    def fetcher(feed: Feed):
        return [FeedEntry(item_id="new", title="New", link="https://x/new")]

    def sender(message: str):
        raise AssertionError("sender should not be called in dry-run")

    result = run_once(
        feeds=[Feed(name="OpenAI", url="https://feed")],
        store=JsonSeenStore(state_path),
        config=WatcherConfig(dry_run=True, max_seen_per_feed=100),
        fetcher=fetcher,
        sender=sender,
    )

    assert result.dry_run == 1
    assert json.loads(state_path.read_text(encoding="utf-8"))["https://feed"] == ["new"]


def test_feed_fetch_error_does_not_modify_that_feed_state(tmp_path: Path):
    state_path = tmp_path / "seen.json"
    state_path.write_text(json.dumps({"https://bad": ["old"]}), encoding="utf-8")

    def fetcher(feed: Feed):
        raise RuntimeError("boom")

    result = run_once(
        feeds=[Feed(name="Bad", url="https://bad")],
        store=JsonSeenStore(state_path),
        config=WatcherConfig(dry_run=False, max_seen_per_feed=100),
        fetcher=fetcher,
        sender=lambda message: True,
    )

    assert result.feed_errors == 1
    assert json.loads(state_path.read_text(encoding="utf-8")) == {"https://bad": ["old"]}


def test_existing_state_new_feed_baselines_without_sending_old_items(tmp_path: Path):
    state_path = tmp_path / "seen.json"
    state_path.write_text(json.dumps({"https://old-feed": ["old"]}), encoding="utf-8")
    sent: list[str] = []

    def fetcher(feed: Feed):
        return [FeedEntry(item_id="visible-old", title="Visible old incident", link="https://x/visible-old")]

    result = run_once(
        feeds=[Feed(name="New Feed", url="https://new-feed")],
        store=JsonSeenStore(state_path),
        config=WatcherConfig(dry_run=False, max_seen_per_feed=100),
        fetcher=fetcher,
        sender=lambda message: sent.append(message) is None,
    )

    assert result.baselined_feeds == 1
    assert result.sent == 0
    assert sent == []
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["https://old-feed"] == ["old"]
    assert saved["https://new-feed"] == ["visible-old"]


def test_sender_exception_marks_only_prior_successes_seen(tmp_path: Path):
    state_path = tmp_path / "seen.json"
    state_path.write_text(json.dumps({"https://feed": []}), encoding="utf-8")

    def fetcher(feed: Feed):
        return [
            FeedEntry(item_id="ok", title="OK", link="https://x/ok"),
            FeedEntry(item_id="boom", title="Boom", link="https://x/boom"),
        ]

    def sender(message: str):
        if "Boom" in message:
            raise RuntimeError("webhook exploded")
        return True

    result = run_once(
        feeds=[Feed(name="OpenAI", url="https://feed")],
        store=JsonSeenStore(state_path),
        config=WatcherConfig(dry_run=False, max_seen_per_feed=100),
        fetcher=fetcher,
        sender=sender,
    )

    assert result.sent == 1
    assert result.failed == 1
    assert json.loads(state_path.read_text(encoding="utf-8"))["https://feed"] == ["ok"]


def test_discord_sender_disables_allowed_mentions(monkeypatch):
    from rss_status_watcher.watcher import send_discord_webhook

    captured = {}

    class FakeResponse:
        status_code = 204
        text = ""

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("rss_status_watcher.watcher.requests.post", fake_post)

    ok = send_discord_webhook("@everyone incident", WatcherConfig(webhook_url="https://discord.example/webhook"))

    assert ok is True
    assert captured["json"]["content"] == "@everyone incident"
    assert captured["json"]["allowed_mentions"] == {"parse": []}
