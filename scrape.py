# -*- coding: utf-8 -*-
"""memorial-object.jp の全記事からモニュメント情報を収集し monuments.json を出力する"""
import json
import re
import sys
import time
import requests
from bs4 import BeautifulSoup

BASE = "https://www.memorial-object.jp"
SITEMAP = BASE + "/post-sitemap.xml"
OUT = "monuments.json"
EXCLUDE = {"/blog-top/"}  # 記事以外のページ

session = requests.Session()
session.headers["User-Agent"] = "monument-map-builder (site owner)"


def get_post_urls():
    xml = session.get(SITEMAP, timeout=30).text
    urls = re.findall(r"<loc><!\[CDATA\[(https://www\.memorial-object\.jp/[^\]]+)\]\]></loc>", xml)
    return [u for u in urls if not any(u.endswith(e) or e in u for e in EXCLUDE)]


def parse_post(url):
    r = session.get(url, timeout=30)
    r.raise_for_status()
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
    items, skipped = [], []
    for i, u in enumerate(urls, 1):
        try:
            item = parse_post(u)
        except Exception as e:
            print(f"  ERROR {u}: {e}", file=sys.stderr)
            skipped.append(u)
            continue
        if item:
            items.append(item)
        else:
            skipped.append(u)
        if i % 20 == 0:
            print(f"  {i}/{len(urls)}")
        time.sleep(0.3)  # サーバーに優しく
    items.sort(key=lambda x: x["name"])
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    print(f"完了: {len(items)} 件を {OUT} に保存 (座標なし等スキップ {len(skipped)} 件)")
    for s in skipped:
        print(f"  skip: {s}")


if __name__ == "__main__":
    main()
