"""Social media feedback sources.

* ``SyntheticSocialSource`` — the labelled synthetic feed from ``analytics/synthetic.py`` (ShopNova is fictional, so
  no real posts about it exist). Every post has ``source="synthetic"``.
* ``MastodonSource`` — a real, permitted source: Mastodon's public hashtag timeline API
  (``GET /api/v1/timelines/tag/{hashtag}``), which needs no credentials for public posts. Use it for a real brand
  hashtag. Posts keep ``source="mastodon"`` and their public URL; author handles are stored only as a hash.
  Fetching is opt-in (dashboard button / ``manage.py analytics-run --mastodon …``) and paged politely.

Real and synthetic posts are never merged without their ``source`` label, and the dashboard filters by it.
"""
from __future__ import annotations

import hashlib
import html
import re
import time

TAG_RE = re.compile(r"<[^>]+>")


class SocialSource:
    name = "source"
    synthetic = False

    def fetch(self):
        raise NotImplementedError


class SyntheticSocialSource(SocialSource):
    name = "synthetic"
    synthetic = True

    def __init__(self, posts):
        self.posts = posts

    def fetch(self):
        return [dict(p) for p in self.posts]


class MastodonSource(SocialSource):
    name = "mastodon"
    synthetic = False

    def __init__(self, instance="mastodon.social", hashtag="customerservice", limit=200, session=None, pause_s=1.0):
        import requests
        self.instance = instance.replace("https://", "").strip("/")
        self.hashtag = hashtag.lstrip("#")
        self.limit = min(int(limit), 400)
        self.http = session or requests.Session()
        self.pause_s = pause_s
        self.error = None

    def fetch(self):
        posts, max_id = [], None
        url = f"https://{self.instance}/api/v1/timelines/tag/{self.hashtag}"
        while len(posts) < self.limit:
            params = dict(limit=40, **({"max_id": max_id} if max_id else {}))
            try:
                resp = self.http.get(url, params=params, timeout=15, headers={"User-Agent": "SupportPilot-analytics/1.0"})
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"[:160]
                break
            if resp.status_code != 200:
                self.error = f"HTTP {resp.status_code}"
                break
            page = resp.json()
            if not page:
                break
            for s in page:
                text = html.unescape(TAG_RE.sub(" ", s.get("content") or "")).strip()
                acct = (s.get("account") or {}).get("acct", "")
                posts.append(dict(id=f"MAS-{self.instance}-{s['id']}", platform=f"mastodon ({self.instance})",
                                  created_at=s.get("created_at"), text=re.sub(r"\s+", " ", text),
                                  author="h_" + hashlib.sha256(acct.encode()).hexdigest()[:10],
                                  likes=s.get("favourites_count", 0), reposts=s.get("reblogs_count", 0),
                                  lang=s.get("language") or "und", url=s.get("url"), source="mastodon"))
            max_id = page[-1]["id"]
            if self.pause_s:
                time.sleep(self.pause_s)
        return posts[:self.limit]
