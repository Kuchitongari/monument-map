# -*- coding: utf-8 -*-
"""memorial-object.jp の全記事からモニュメント情報を収集し monuments.json を出力する。

記事の一覧は WordPress の公開REST API から取る(2026-09-23 変更)。
以前はサイトマップを使っていたが、写真のないOSM記事をサイトマップから外す設定を入れたため、
それらが地図に出なくなった。REST API なら公開中の記事をすべて取れる。"""
import datetime
import json
import os
import re
import sys
import time
import requests
from bs4 import BeautifulSoup
from html import unescape

BASE = "https://www.memorial-object.jp"
OUT = "monuments.json"
RETRY_WAITS = [3, 10, 30]  # 一時エラー時の待機秒 (最大4回試行)
# 取得失敗がこの数を超えたら JSON を書かずに異常終了する (旧データを守る)
MAX_FETCH_ERRORS = int(os.environ.get("MAX_FETCH_ERRORS", "5"))

session = requests.Session()
session.headers["User-Agent"] = "monument-map-builder (site owner)"


class FetchError(Exception):
    """リトライしても取得できなかった (座標なしとは区別する)"""


def get(url):
    """一時的なエラー (5xx / 429 / 通信断) はリトライする"""
    last = None
    for i, wait in enumerate([0] + RETRY_WAITS):
        if wait:
            time.sleep(wait)
        try:
            r = session.get(url, timeout=30)
            if r.status_code >= 500 or r.status_code == 429:
                last = f"{r.status_code} {r.reason}"
                print(f"  RETRY {url}: {last} ({i + 1}回目)", file=sys.stderr)
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = str(e)
            print(f"  RETRY {url}: {last} ({i + 1}回目)", file=sys.stderr)
    raise FetchError(last)


def api(path, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return get(f"{BASE}/wp-json/wp/v2/{path}?{q}")


def fetch_all(path, **params):
    """公開REST APIの一覧を全ページ分取る"""
    out, page = [], 1
    while True:
        r = api(path, page=page, per_page=100, **params)
        out += r.json()
        if page >= int(r.headers.get("X-WP-TotalPages", 1)):
            return out
        page += 1


def category_names():
    return {c["id"]: c["name"] for c in fetch_all("categories", _fields="id,name")}


def featured_images(posts):
    """アイキャッチ画像の画像ID → URL。本文に画像がない記事の代表画像に使う"""
    ids = sorted({p["featured_media"] for p in posts if p.get("featured_media")})
    src = {}
    for i in range(0, len(ids), 100):
        chunk = ",".join(map(str, ids[i:i + 100]))
        for m in api("media", include=chunk, per_page=100, _fields="id,source_url,media_details").json():
            sizes = (m.get("media_details") or {}).get("sizes") or {}
            pick = sizes.get("medium_large") or sizes.get("large") or {}
            src[m["id"]] = pick.get("source_url") or m.get("source_url", "")
    return src


def parse_post(post, cats, featured):
    """REST API の1記事 → 地図の1件。座標が取れなければ None"""
    html_body = (post.get("content") or {}).get("rendered", "")
    url = post["link"]

    m = re.search(r"google\.com/maps\?q=([\d.]+),([\d.]+)", html_body)
    if m:
        lat, lng = float(m.group(1)), float(m.group(2))
    else:
        # 旧形式: /maps/embed?pb=...!2d<lng>!3d<lat>...
        m = re.search(r"google\.com/maps/embed\?pb=[^\"']*?!2d([\d.-]+)!3d([\d.-]+)", html_body)
        if not m:
            return None
        lng, lat = float(m.group(1)), float(m.group(2))

    title = re.sub(r"<[^>]+>", "", (post.get("title") or {}).get("rendered", "")).strip()
    title = unescape(title)
    soup = BeautifulSoup(html_body, "html.parser")
    text = soup.get_text("\n")
    address = access = ""
    am = re.search(r"設置場所\n+([^\n]+)", text)
    if am and "Google" not in am.group(1):
        address = am.group(1).strip()
    xm = re.search(r"アクセス\n+([^\n]+)", text)
    if xm:
        access = xm.group(1).strip()

    img = soup.select_one('img[src*="wp-content/uploads"]')
    image = img["src"] if img else featured.get(post.get("featured_media"), "")

    return {
        "name": title.split("｜")[0] if title else url,
        "lat": lat,
        "lng": lng,
        "address": address,
        "access": access,
        "categories": sorted({cats[c] for c in post.get("categories", []) if c in cats}),
        "image": image,
        "url": url,
    }


def generated_date(items):
    """中身が前回と同じなら日付も据え置く(毎日の無意味なコミットを避ける)"""
    try:
        with open(OUT, encoding="utf-8") as f:
            prev = json.load(f)
        if (isinstance(prev, dict) and prev.get("items") == items
                and prev.get("contributions") == contributions() and prev.get("generated")):
            return prev["generated"]
    except (OSError, ValueError):
        pass
    return datetime.date.today().isoformat()


def contributions():
    """写真提供の実績(pipeline/update_contributions.py が作る site/contributions.json)。無ければ 0 人 0 枚"""
    try:
        with open("contributions.json", encoding="utf-8") as f:
            d = json.load(f)
        return {"people": int(d.get("people", 0)), "photos": int(d.get("photos", 0)),
                "names": list(d.get("names", []))}
    except (OSError, ValueError, TypeError):
        return {"people": 0, "photos": 0, "names": []}


def previous_count():
    try:
        with open(OUT, encoding="utf-8") as f:
            prev = json.load(f)
        return len(prev["items"]) if isinstance(prev, dict) else len(prev)
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def main():
    try:
        posts = fetch_all("posts", status="publish", _fields="id,link,title,content,categories,featured_media")
        cats = category_names()
        featured = featured_images(posts)
    except (FetchError, ValueError, KeyError) as e:
        print(f"中止: 記事一覧を取得できませんでした({e})。{OUT} は更新しません。", file=sys.stderr)
        sys.exit(1)
    print(f"{len(posts)} 記事を処理します(アイキャッチあり {len(featured)} 件)")

    items, no_coord, failed = [], [], []
    for p in posts:
        try:
            item = parse_post(p, cats, featured)
        except Exception as e:                      # 1記事の解析失敗で全体を止めない
            print(f"  ERROR {p.get('link')}: {e}", file=sys.stderr)
            failed.append(p.get("link", ""))
            continue
        (items if item else no_coord).append(item or p.get("link", ""))

    for u in no_coord:
        print(f"  座標なし: {u}")

    if len(failed) > MAX_FETCH_ERRORS:
        print(f"中止: {len(failed)} 記事を処理できませんでした(上限 {MAX_FETCH_ERRORS} 件)。{OUT} は更新しません。",
              file=sys.stderr)
        for u in failed:
            print(f"  失敗: {u}", file=sys.stderr)
        sys.exit(1)

    before = previous_count()
    if before and len(items) < before * 0.8:        # 取りこぼしで地図が激減するのを防ぐ
        print(f"中止: 件数が前回({before}件)より大きく減りました({len(items)}件)。{OUT} は更新しません。",
              file=sys.stderr)
        sys.exit(1)

    items.sort(key=lambda x: x["name"])
    # ODbLの継承条項に配慮し、データセットとしてのライセンスを明記して配布する
    payload = {
        "license": "ODbL-1.0",
        "license_url": "https://opendatacommons.org/licenses/odbl/1-0/",
        "attribution": "© OpenStreetMap contributors / モニュメントnet(memorial-object.jp)",
        "note": "位置情報の一部は OpenStreetMap に由来します。この一覧は ODbL 1.0 で提供します。",
        "contributions": contributions(),
        "generated": generated_date(items),
        "count": len(items),
        "items": items,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(
        f"完了: {len(items)} 件を {OUT} に保存 "
        f"(座標なし {len(no_coord)} 件 / 取得失敗 {len(failed)} 件)"
    )
    for u in failed:
        print(f"  取得失敗: {u}")


if __name__ == "__main__":
    main()
