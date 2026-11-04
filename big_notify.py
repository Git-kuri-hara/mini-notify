#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BIGシリーズ 中止検知 → スマホ通知 (ntfy)

目的  : 指定試合の中止・延期、および公式「お知らせ」の中止告知を自動監視し、
        新しい中止が出た時だけスマホへプッシュ通知する。
対象  : BIG / 100円BIG / MEGA BIG / BIG1000 / mini BIG
仕組み: (1) 公式の指定試合ページの表で「中止」の数を判定 (big_checker.py を利用)
        (2) 公式お知らせ一覧に出る「中止」「不成立」「返還」「延期」を含む新着を検知
        前回状態は state.json に保存し、変化があった時だけ通知する。
        初回実行は現状を記録するだけ(過去分の通知は送らない)。
環境変数:
  NTFY_TOPIC  必須。ntfyのトピック名(推測されにくい長い文字列)
  START_ROUND 任意。初回の監視起点の開催回(既定 1656)
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

from big_checker import DEFAULT_PROBE_IDS, KNOWN_IDS, fetch, guess_type, judge, parse_html

STATE_FILE = os.environ.get("STATE_FILE", "state.json")
INFO_URL = "https://www.toto-dream.com/information/index.html"
INFO_BASE = "https://www.toto-dream.com"
INFO_KEYWORDS = ("中止", "不成立", "返還", "延期")
USER_AGENT = "Mozilla/5.0 (compatible; big-notify/1.0)"
LOOKBACK = 1      # 起点の何回前から見るか
MAX_FORWARD = 8   # 起点から何回先まで探すか
JST = timezone(timedelta(hours=9))


def load_state(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(path, state):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def send_ntfy(topic, message, priority="default", tags="soccer"):
    req = urllib.request.Request(
        "https://ntfy.sh/%s" % topic,
        data=message.encode("utf-8"),
        method="POST",
        headers={"Priority": priority, "Tags": tags},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def parse_info(html):
    """お知らせ一覧から (id, タイトル, URL) を抽出。キーワードを含むものだけ返す。"""
    items = []
    pattern = re.compile(
        r'<a[^>]+href="([^"]*?/information/(\d{8}(?:_\d+)?)\.html)"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    for href, info_id, inner in pattern.findall(html):
        text = re.sub(r"<[^>]+>", " ", inner)
        text = " ".join(text.split())
        if any(k in text for k in INFO_KEYWORDS):
            url = href if href.startswith("http") else INFO_BASE + href
            items.append((info_id, text, url))
    return items


def signature(res):
    return "|".join(str(g["no"]) for g in res["cancelled_games"]) + "#" + res["verdict"]


def build_message(round_no, cid, label, res, url):
    lines = ["【%s 第%d回】%s" % (label, round_no, res["verdict"])]
    lines.append("開催 %d / 指定 %d(中止・延期 %d)" % (res["held_count"], res["total"], res["cancelled_count"]))
    for g in res["cancelled_games"]:
        lines.append("中止 No.%d %s %s vs %s" % (g["no"], g["schedule"], g["home"], g["away"]))
    lines.append(res["reason"])
    lines.append(url)
    return "\n".join(lines)


def discover_ids(round_no, fetch_fn):
    found = {}
    for cid in DEFAULT_PROBE_IDS:
        try:
            html, _ = fetch_fn(round_no, cid)
        except Exception:
            continue
        games, _, banner = parse_html(html)
        if games:
            found[cid] = guess_type(cid, len(games), banner)
    return found


def run(state, fetch_fn, info_fn, notify_fn, today_str):
    first_run = not state.get("initialized")
    notified = state.setdefault("signatures", {})
    messages = []
    fetched_any = False

    base_round = int(state.get("last_round", int(os.environ.get("START_ROUND", "1656"))))

    if not state.get("ids"):
        ids = discover_ids(base_round, fetch_fn)
        state["ids"] = ids if ids else dict(KNOWN_IDS)
    ids = state["ids"]

    r = base_round - LOOKBACK
    misses = 0
    highest = base_round
    while r <= base_round + MAX_FORWARD and misses < 2:
        round_has_table = False
        for cid, label in ids.items():
            try:
                html, url = fetch_fn(r, cid)
            except Exception:
                continue
            games, notice, banner = parse_html(html)
            if not games:
                continue
            round_has_table = True
            fetched_any = True
            res = judge(games, notice)
            key = "%d:%s" % (r, cid)
            sig = signature(res)
            prev = notified.get(key)
            notified[key] = sig
            changed = (prev is None and res["cancelled_count"] > 0) or (prev is not None and prev != sig)
            if changed and not first_run:
                messages.append((build_message(r, cid, label, res, url), "high", "warning"))
        if round_has_table:
            misses = 0
            highest = max(highest, r)
        else:
            misses += 1
        r += 1
    state["last_round"] = highest

    try:
        info_html = info_fn()
        fetched_any = True
        seen = set(state.get("seen_info", []))
        for info_id, title, url in parse_info(info_html):
            if info_id not in seen:
                seen.add(info_id)
                if not first_run:
                    messages.append(("【公式お知らせ】%s\n%s" % (title, url), "high", "loudspeaker"))
        state["seen_info"] = sorted(seen)
    except Exception:
        pass

    if first_run:
        messages.append(
            (
                "BIG中止監視を開始しました。監視対象くじ: %s / 起点: 第%d回"
                % (", ".join("%s=%s" % (k, v) for k, v in ids.items()), base_round),
                "default",
                "white_check_mark",
            )
        )
    state["initialized"] = True
    state["last_check_date"] = today_str

    for msg, prio, tags in messages:
        notify_fn(msg, prio, tags)
    return fetched_any, len(messages)


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("環境変数 NTFY_TOPIC が未設定です。", file=sys.stderr)
        return 2
    state = load_state(STATE_FILE)
    today = datetime.now(JST).strftime("%Y-%m-%d")
    fetched_any, sent = run(
        state,
        fetch,
        lambda: http_get(INFO_URL),
        lambda m, p, t: send_ntfy(topic, m, p, t),
        today,
    )
    save_state(STATE_FILE, state)
    print("取得: %s / 通知送信: %d件 / 最新確認回: %s" % ("OK" if fetched_any else "失敗", sent, state.get("last_round")))
    return 0 if fetched_any else 1


if __name__ == "__main__":
    sys.exit(main())
