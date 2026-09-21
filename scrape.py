# -*- coding: utf-8 -*-
"""memorial-object.jp の全記事からモニュメント情報を収集し monuments.json を出力する"""
import datetime
import json
import os
import re
import sys
import time
import requests
from bs4 import BeautifulSoup

BASE = "https://www.memorial-object.jp"
SITEMAP = BASE + "/post-sitemap.xml"
OUT = "monuments.json"
EXCLUDE = {"/blog-top/"}  # 記事以外のページ
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


def get_post_urls():
    xml = get(SITEMAP).text
    urls = re.findall(r"<loc><!\[CDATA\[(https://www\.memorial-object\.jp/[^\]]+)\]\]></loc>", xml)
    return [u for u in urls if not any(u.endswith(e) or e in u for e in EXCLUDE)]


def featured_images():
    """記事URL → アイキャッチ画像のURL。本文に画像がなくアイキャッチだけの記事も「写真あり」にするため。
    取れなかったときは空の辞書(本文の画像だけで判定する従来の動作になる)"""
    try:
        posts, page = [], 1
        while True:
            r = get(f"{BASE}/wp-json/wp/v2/posts?per_page=100&page={page}&_fields=link,featured_media")
            posts += r.json()
            if page >= int(r.headers.get("X-WP-TotalPages", 1)):
                break
            page += 1
        ids = sorted({p["featured_media"] for p in posts if p.get("featured_media")})
        src = {}
        for i in range(0, len(ids), 100):
            chunk = ",".join(map(str, ids[i:i + 100]))
            r = get(f"{BASE}/wp-json/wp/v2/media?include={chunk}&per_page=100&_fields=id,source_url,media_details")
            for m in r.json():
                sizes = (m.get("media_details") or {}).get("sizes") or {}
                pick = sizes.get("medium_large") or sizes.get("large") or {}
                src[m["id"]] = pick.get("source_url") or m.get("source_url", "")
        return {p["link"]: src.get(p["featured_media"], "") for p in posts if p.get("featured_media")}
    except (FetchError, ValueError, KeyError) as e:
        print(f"  ⚠ アイキャッチ画像を取得できませんでした({e})。本文の画像だけで判定します", file=sys.stderr)
        return {}


def parse_post(url):
    r = get(url)
    soup = BeautifulSoup(r.text, "html.parser")

    m = re.search(r"google\.com/maps\?q=([\d.]+),([\d.]+)", r.text)
    if m:
        lat, lng = float(m.group(1)), float(m.group(2))
    else:
        # 旧形式: /maps/embed?pb=...!2d<lng>!3d<lat>...
        m = re.search(r"google\.com/maps/embed\?pb=[^\"']*?!2d([\d.-]+)!3d([\d.-]+)", r.text)
        if not m:
            return None
        lng, lat = float(m.group(1)), float(m.group(2))

    h1 = soup.find("h1", class_="entry-title")
    title = h1.get_text(strip=True) if h1 else ""
    name = title.split("｜")[0] if title else url

    text = soup.get_text("\n")
    address = access = ""
    am = re.search(r"設置場所\n+([^\n]+)", text)
    if am and "Google" not in am.group(1):
        address = am.group(1).strip()
    xm = re.search(r"アクセス\n+([^\n]+)", text)
    if xm:
        access = xm.group(1).strip()

    cats = sorted({a.get_text(strip=True) for a in soup.select('a[rel="category tag"]')})

    img = soup.select_one('img[src*="wp-content/uploads"]')
    image = img["src"] if img else ""

    return {
        "name": name,
        "lat": lat,
        "lng": lng,
        "address": address,
        "access": access,
        "categories": cats,
        "image": image,
        "url": url,
    }


def generated_date(items):
    """中身が前回と同じなら日付も据え置く(毎日の無意味なコミットを避ける)"""
    try:
        with open(OUT, encoding="utf-8") as f:
            prev = json.load(f)
        if isinstance(prev, dict) and prev.get("items") == items and prev.get("generated"):
            return prev["generated"]
    except (OSError, ValueError):
        pass
    return datetime.date.today().isoformat()


def main():
    urls = get_post_urls()
    print(f"{len(urls)} 記事を処理します")
    featured = featured_images()
    print(f"  アイキャッチあり {len(featured)} 記事")
    items, no_coord, failed = [], [], []
    for i, u in enumerate(urls, 1):
        try:
            item = parse_post(u)
        except FetchError as e:
            print(f"  ERROR {u}: {e}", file=sys.stderr)
            failed.append(u)
            continue
        except Exception as e:
            print(f"  ERROR {u}: {e}", file=sys.stderr)
            failed.append(u)
            continue
        if item:
            if not item["image"] and featured.get(u):
                item["image"] = featured[u]      # 本文に画像がなくてもアイキャッチがあれば写真あり
            items.append(item)
        else:
            no_coord.append(u)
        if i % 20 == 0:
            print(f"  {i}/{len(urls)}")
        time.sleep(0.3)  # サーバーに優しく

    for u in no_coord:
        print(f"  座標なし: {u}")

    if len(failed) > MAX_FETCH_ERRORS:
        print(
            f"中止: {len(failed)} 記事を取得できませんでした "
            f"(上限 {MAX_FETCH_ERRORS} 件)。{OUT} は更新しません。",
            file=sys.stderr,
        )
        for u in failed:
            print(f"  取得失敗: {u}", file=sys.stderr)
        sys.exit(1)

    items.sort(key=lambda x: x["name"])
    # ODbLの継承条項に配慮し、データセットとしてのライセンスを明記して配布する
    payload = {
        "license": "ODbL-1.0",
        "license_url": "https://opendatacommons.org/licenses/odbl/1-0/",
        "attribution": "© OpenStreetMap contributors / モニュメントnet(memorial-object.jp)",
        "note": "位置情報の一部は OpenStreetMap に由来します。この一覧は ODbL 1.0 で提供します。",
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
