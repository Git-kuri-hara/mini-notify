#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BIGシリーズ 最低成立試合数 自動チェッカー (v2)

目的  : スポーツくじ公式サイトの指定試合ページから「中止」「延期」を自動取得し、
        BIG系くじの最低成立試合数を満たすか判定する。
対象  : BIG / 100円BIG(14試合・最低10) / MEGA BIG(12試合・最低8)
        BIG1000(11試合・最低8) / mini BIG(9試合・最低6)
情報源: https://sp.toto-dream.com/dcs/subos/screen/si02/ssin000/PGSSIN00001InitGameBIGSP.form
        ?commodityId=<くじ種ID>&holdCntId=<開催回>
        commodityId=09 が BIG であることは実ページで確認済み。
        他のくじ種のIDは未確認のため、--commodity 未指定時は 01〜20 を順に試し、
        試合表が取得できたものを判定する(くじ種はバナー画像名・試合数から推定)。

使い方:
  python big_checker.py 1656
  python big_checker.py 1656 1657
  python big_checker.py --latest 1656 --count 3
  python big_checker.py 1656 --commodity 09          # BIGのみ
  python big_checker.py 1656 --commodity 09 10 11    # 指定IDのみ
  python big_checker.py 1656 --json
  python big_checker.py --file sample.html           # 保存済みHTMLを解析
標準ライブラリのみ使用。
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser

BASE_URL = (
    "https://sp.toto-dream.com/dcs/subos/screen/si02/ssin000/"
    "PGSSIN00001InitGameBIGSP.form?commodityId={cid}&holdCntId={round}"
)
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
# 指定試合数 -> 最低成立試合数 (出典: SMBC toto「BIGの商品詳細」、JSC発表資料)
MIN_HELD_BY_TOTAL = {14: 10, 12: 8, 11: 8, 9: 6}
# 指定試合数 -> 1試合あたりの選択肢数 (BIG系3択、MEGA BIG 4択)
CHOICES_BY_TOTAL = {14: 3, 12: 4, 11: 3, 9: 3}
DEFAULT_PROBE_IDS = ["%02d" % i for i in range(1, 21)]
KNOWN_IDS = {"09": "BIG"}
CANCEL_WORDS = ("中止", "延期")


class GameTableParser(HTMLParser):
    """指定試合表(No/日程/競技場/ホーム/VS/アウェイ)、バナー画像名、ページ全体テキストを収集する。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._row = None
        self._cell = None
        self.text_parts = []
        self.banner = ""

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")
        elif tag == "img" and not self.banner:
            src = dict(attrs).get("src") or ""
            m = re.search(r"/(bnr_[A-Za-z0-9_]+\.[a-z]+)", src)
            if m:
                self.banner = m.group(1)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        self.text_parts.append(data)
        if self._cell is not None:
            self._cell.append(data)


def parse_html(html):
    parser = GameTableParser()
    parser.feed(html)
    games = []
    for row in parser.rows:
        if len(row) < 6 or not row[0].strip().isdigit():
            continue
        status_text = row[4].strip()
        games.append(
            {
                "no": int(row[0].strip()),
                "schedule": " ".join(row[1].split()),
                "venue": row[2],
                "home": row[3],
                "away": row[5],
                "status": status_text,
                "cancelled": any(w in status_text for w in CANCEL_WORDS),
            }
        )
    notice = "くじは中止となりました" in "".join(parser.text_parts)
    return games, notice, parser.banner


def guess_type(cid, total, banner):
    if cid in KNOWN_IDS:
        return KNOWN_IDS[cid]
    b = banner.lower()
    if "mega" in b:
        return "MEGA BIG(推定)"
    if "1000" in b:
        return "BIG1000(推定)"
    if "mini" in b:
        return "mini BIG(推定)"
    if "100" in b:
        return "100円BIG(推定)"
    by_total = {14: "BIG系14試合(100円BIG等・推定)", 12: "MEGA BIG(推定)", 11: "BIG1000(推定)", 9: "mini BIG(推定)"}
    return by_total.get(total, "不明")


def judge(games, notice):
    total = len(games)
    cancelled = [g for g in games if g["cancelled"]]
    held = total - len(cancelled)
    res = {
        "total": total,
        "cancelled_count": len(cancelled),
        "held_count": held,
        "cancelled_games": cancelled,
        "official_notice_cancelled": notice,
        "min_held": MIN_HELD_BY_TOTAL.get(total),
    }
    if total not in MIN_HELD_BY_TOTAL:
        res["verdict"] = "判定不能"
        res["reason"] = "指定試合数%d試合は対応表(14/12/11/9試合)にありません。" % total
        return res
    need = MIN_HELD_BY_TOTAL[total]
    if held >= need:
        res["verdict"] = "成立"
        res["reason"] = "開催%d試合 >= 最低成立%d試合。中止はあと%d試合まで許容。" % (
            held, need, total - need - len(cancelled))
    else:
        res["verdict"] = "不成立"
        res["reason"] = "開催%d試合 < 最低成立%d試合(%d試合不足)。" % (held, need, need - held)
    if notice and res["verdict"] == "成立":
        res["verdict"] = "不成立(公式表示)"
        res["reason"] += " ただし公式ページに『くじは中止となりました』の表示あり。公式表示を優先。"
    return res


def fetch(round_no, cid, timeout=15):
    url = BASE_URL.format(cid=cid, round=round_no)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace"), url


def print_result(label, url, res):
    print("=" * 60)
    print(label)
    if url:
        print("取得元: %s" % url)
    print("判定  : %s" % res["verdict"])
    print("内訳  : 開催 %d / 指定 %d / 中止・延期 %d" % (res["held_count"], res["total"], res["cancelled_count"]))
    print("理由  : %s" % res["reason"])
    if res["cancelled_games"]:
        print("中止・延期の試合:")
        for g in res["cancelled_games"]:
            print("  No.%-2d %s  %s vs %s  [%s]" % (g["no"], g["schedule"], g["home"], g["away"], g["status"]))
    t = res["total"]
    if res["verdict"] == "成立" and res["cancelled_count"] > 0 and t in CHOICES_BY_TOTAL:
        print("参考  : 中止試合は的中扱い。1等理論確率は 1/%s" % format(CHOICES_BY_TOTAL[t] ** res["held_count"], ","))


def main():
    ap = argparse.ArgumentParser(description="BIGシリーズ 最低成立試合数 自動チェッカー")
    ap.add_argument("rounds", nargs="*", type=int, help="開催回(例: 1656)")
    ap.add_argument("--latest", type=int, help="この開催回から遡ってチェック")
    ap.add_argument("--count", type=int, default=1, help="--latest と併用: 遡る回数")
    ap.add_argument("--commodity", nargs="+", help="くじ種ID(例: 09)。未指定なら01〜20を試行")
    ap.add_argument("--file", help="保存済みHTMLを解析")
    ap.add_argument("--json", action="store_true", help="JSON出力")
    args = ap.parse_args()

    targets = list(args.rounds)
    if args.latest is not None:
        targets += [args.latest - i for i in range(max(args.count, 1))]

    outputs = []
    exit_code = 0

    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            html = f.read()
        games, notice, banner = parse_html(html)
        label = "ファイル: %s / %s" % (args.file, guess_type("", len(games), banner))
        outputs.append((label, None, judge(games, notice)))
    elif not targets:
        ap.error("開催回、--latest、--file のいずれかを指定してください。")
    else:
        ids = args.commodity if args.commodity else DEFAULT_PROBE_IDS
        for r in targets:
            found = False
            for cid in ids:
                try:
                    html, url = fetch(r, cid)
                except (urllib.error.URLError, TimeoutError):
                    continue
                games, notice, banner = parse_html(html)
                if not games:
                    continue
                found = True
                label = "第%d回 commodityId=%s %s (バナー: %s)" % (
                    r, cid, guess_type(cid, len(games), banner), banner or "なし")
                outputs.append((label, url, judge(games, notice)))
                time.sleep(0.3)
            if not found:
                print("第%d回: 試合表を取得できませんでした(開催回の誤り、または接続失敗)。" % r, file=sys.stderr)
                exit_code = 1

    if args.json:
        print(json.dumps([{"label": l, "url": u, **r} for l, u, r in outputs], ensure_ascii=False, indent=2))
    else:
        for label, url, res in outputs:
            print_result(label, url, res)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
