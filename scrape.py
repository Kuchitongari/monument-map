# -*- coding: utf-8 -*-
"""memorial-object.jp の全記事からモニュメント情報を収集し monuments.json を出力する"""
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


def main():
    urls = get_post_urls()
    print(f"{len(urls)} 記事を処理します")
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
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    print(
        f"完了: {len(items)} 件を {OUT} に保存 "
        f"(座標なし {len(no_coord)} 件 / 取得失敗 {len(failed)} 件)"
    )
    for u in failed:
        print(f"  取得失敗: {u}")


if __name__ == "__main__":
    main()
