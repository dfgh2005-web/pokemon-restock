#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
포켓몬스토어 재입고 알림 봇 — GitHub Actions용 (1회 실행)

GitHub Actions가 약 5분마다 이 스크립트를 한 번씩 실행합니다.
- 카테고리 페이지를 열어(자바스크립트 렌더링) 각 상품의 품절/판매중 상태를 읽고,
- 직전 상태(restock_state.json)와 비교해서 '품절 -> 재입고'된 상품이나
  새로 올라온 '판매중' 상품이 생기면 디스코드 웹훅으로 알립니다.
- 디스코드 웹훅 주소는 코드가 아니라 환경변수 DISCORD_WEBHOOK_URL
  (= GitHub Secret)에서 읽습니다. 그래서 공개 저장소여도 노출되지 않습니다.
"""

import os
import sys
import json
import html
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Playwright가 설치되어 있지 않습니다. (requirements.txt / playwright install chromium 확인)")
    sys.exit(1)

# ============================ 설정 ============================
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

BASE = "https://www.pokemonstore.co.kr"
CATEGORY_NO = "488339"         # 감시할 카테고리 번호 (URL의 categoryNo 값)
PAGE_SIZE = 20                 # 한 페이지당 상품 수 (사이트 기본값)
ALERT_ON_NEW_PRODUCTS = True   # 새로 올라온 '판매중' 상품도 알림할지

STATE_FILE = Path(__file__).with_name("restock_state.json")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# ============================================================


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def build_url(page_number):
    return (
        f"{BASE}/pages/product/product-list.html"
        f"?categoryNo={CATEGORY_NO}&pageNumber={page_number}"
        f"&pageSize={PAGE_SIZE}&sortType=RECENT_PRODUCT"
    )


def build_detail_url(no):
    return f"{BASE}/pages/product/product-detail.html?productNo={no}"


# -------- 상품 JSON 파싱 (사이트 API 구조에 유연하게 대응) --------
NAME_KEYS = ["productName", "name", "goodsName", "title", "productNm", "displayName"]
NO_KEYS = ["productNo", "productNumber", "goodsNo", "mallProductNo", "productId", "id"]
PRICE_KEYS = ["salePrice", "price", "immediateDiscountedPrice", "buyPrice", "saleprice"]
IMAGE_KEYS = ["imageUrl", "listImageUrl", "mainImageUrl", "thumbnailUrl", "imgUrl",
              "representImage", "imageUrls"]
URL_KEYS = ["url", "productUrl", "detailUrl", "linkUrl"]
STATUS_KEYS = ["saleStatusType", "saleStatus", "stockStatus", "productSaleStatus",
               "saleStatusLabel", "displayStatus"]
STOCK_KEYS = ["stockCnt", "stock", "stockQuantity", "stockCount", "remainStock",
              "saleCnt", "totalStock"]
SOLDOUT_KEYS = ["soldOut", "isSoldOut", "soldout", "isSoldout"]

BUYABLE_STATUS = {"ONSALE", "ON_SALE", "SALE", "SALES", "NORMAL", "SELLING",
                  "구매가능", "판매중"}
NOT_BUYABLE_STATUS = {"READY", "FINISHED", "STOP", "PROHIBITION", "SOLDOUT", "SOLD_OUT",
                      "OUTOFSTOCK", "OUT_OF_STOCK", "END",
                      "품절", "일시품절", "판매종료", "판매중지"}


def _first(d, keys):
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] not in (None, ""):
            return d[k]
    return None


def looks_like_product(d):
    return (isinstance(d, dict)
            and _first(d, NAME_KEYS) is not None
            and _first(d, NO_KEYS) is not None)


def find_product_list(data, _depth=0):
    """응답 JSON 안에서 '상품 목록처럼 보이는 리스트'를 찾아 반환."""
    if _depth > 6:
        return None
    if isinstance(data, list):
        if data and looks_like_product(data[0]):
            return [x for x in data if looks_like_product(x)]
        for item in data:
            lst = find_product_list(item, _depth + 1)
            if lst:
                return lst
        return None
    if isinstance(data, dict):
        for v in data.values():
            lst = find_product_list(v, _depth + 1)
            if lst:
                return lst
    return None


def normalize_image(url):
    if not url:
        return None
    if isinstance(url, list):
        url = url[0] if url else None
    if not isinstance(url, str):
        return None
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return BASE + url
    return url


def is_available(p):
    """상품 하나가 구매 가능한지 판단. (가능여부, 판단근거) 반환."""
    # 1) soldOut 플래그가 있으면 그대로 신뢰 (True=품절, False=판매중). 이 사이트의 핵심 신호.
    for k in SOLDOUT_KEYS:
        if isinstance(p.get(k), bool):
            return (not p[k]), f"{k}={str(p[k]).lower()}"
    # 2) 판매 상태 문자열
    status = None
    for k in STATUS_KEYS:
        v = p.get(k)
        if isinstance(v, str):
            status = v.strip().upper()
            break
    if status in NOT_BUYABLE_STATUS:
        return False, f"status={status}"
    if status in BUYABLE_STATUS:
        return True, f"status={status}"
    # 3) 재고 수량 (음수 -999 등은 '재고 관리 안함' 신호이므로 무시)
    for k in STOCK_KEYS:
        v = p.get(k)
        if isinstance(v, (int, float)) and v >= 0:
            return (v > 0), f"{k}={v}"
    # 4) 아무 신호도 없으면 판매중으로 간주
    return True, "신호없음→판매중간주"


def extract(p):
    no = _first(p, NO_KEYS)
    name = html.unescape(str(_first(p, NAME_KEYS) or "(이름없음)"))
    price = _first(p, PRICE_KEYS)
    image = normalize_image(_first(p, IMAGE_KEYS))
    url = _first(p, URL_KEYS) or build_detail_url(no)
    avail, reason = is_available(p)
    return str(no), {
        "name": str(name), "price": price, "image": image,
        "url": url, "available": avail, "reason": reason,
    }


# -------- 페이지 로딩 + 응답(JSON) 가로채기 --------
def fetch_all_products(context):
    """카테고리의 모든 페이지를 돌며 {상품번호: 정보} 딕셔너리를 만든다."""
    products = {}
    page_number = 1
    MAX_PAGES = 50

    while page_number <= MAX_PAGES:
        captured = []  # (url, product_list)

        def handler(response, _store=captured):
            try:
                ct = response.headers.get("content-type", "")
                if "json" not in ct.lower():
                    return
                data = response.json()
            except Exception:
                return
            lst = find_product_list(data)
            if lst:
                _store.append((response.url, lst))

        page = context.new_page()
        page.on("response", handler)
        try:
            page.goto(build_url(page_number), wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            page.wait_for_timeout(1500)
        finally:
            page.close()

        if not captured:
            break
        captured.sort(key=lambda x: len(x[1]), reverse=True)  # 상품 많은 응답 사용
        page_list = captured[0][1]

        new_count = 0
        for p in page_list:
            no, info = extract(p)
            if no in (None, "None"):
                continue
            if no not in products:
                new_count += 1
            products[no] = info

        if len(page_list) < PAGE_SIZE or new_count == 0:  # 마지막 페이지면 중단
            break
        page_number += 1

    return products


# -------- 상태 저장/비교 (변동 감지에 꼭 필요한 값만 저장) --------
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(products):
    # 재고 수량 등 자잘한 변동으로 매번 커밋되지 않도록, 이름과 가용여부만 정렬해 저장
    slim = {no: {"name": v["name"], "available": v["available"]}
            for no, v in sorted(products.items())}
    STATE_FILE.write_text(json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")


def compute_alerts(prev, current):
    alerts = []
    for no, info in current.items():
        if not info["available"]:
            continue
        old = prev.get(no)
        if old is None:
            if ALERT_ON_NEW_PRODUCTS:
                alerts.append(("new", info))
        elif not old.get("available", True):
            alerts.append(("restock", info))
    return alerts


# -------- 디스코드 전송 --------
def build_embed(kind, info):
    label = "🔔 재입고" if kind == "restock" else "🆕 신상품 입고"
    embed = {
        "title": f"{label}: {info['name']}"[:256],
        "url": info["url"],
        "color": 0x2ECC71 if kind == "restock" else 0x3498DB,
        "fields": [{"name": "상태", "value": "구매 가능", "inline": True}],
        "footer": {"text": "포켓몬스토어 재입고 봇"},
    }
    if info.get("price"):
        embed["fields"].append({"name": "가격", "value": f"{info['price']}", "inline": True})
    if info.get("image"):
        embed["thumbnail"] = {"url": info["image"]}
    return embed


def send_discord(webhook_url, content=None, embeds=None):
    payload = {}
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "restock-bot"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status


def notify(webhook_url, alerts):
    embeds = [build_embed(kind, info) for kind, info in alerts]
    for i in range(0, len(embeds), 10):  # 디스코드는 메시지당 임베드 최대 10개
        chunk = embeds[i:i + 10]
        head = f"포켓몬스토어 재입고/입고 알림 {len(chunk)}건" if i == 0 else None
        try:
            send_discord(webhook_url, content=head, embeds=chunk)
        except Exception as e:
            print(f"{now()} 디스코드 전송 실패: {e}")


# -------- 메인: 1회 실행 --------
def run_once():
    if ("discord.com/api/webhooks" not in DISCORD_WEBHOOK_URL
            and "discordapp.com/api/webhooks" not in DISCORD_WEBHOOK_URL):
        print("⚠️  DISCORD_WEBHOOK_URL 시크릿이 설정되지 않았습니다. (GitHub Secrets 확인)")
        sys.exit(1)

    state = load_state()
    first_run = len(state) == 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=USER_AGENT, locale="ko-KR")
            try:
                current = fetch_all_products(context)
            finally:
                context.close()
        finally:
            browser.close()

    if not current:
        # 못 읽었으면(차단/일시 오류 등) 상태를 덮어쓰지 않고 종료 → 오탐 방지
        print(f"{now()} 상품을 읽지 못했습니다. 상태를 유지하고 종료합니다.")
        return

    avail = sum(1 for v in current.values() if v["available"])
    print(f"{now()} 상품 {len(current)}개 확인 (판매중 {avail} / 품절 {len(current) - avail})")
    for no, v in list(current.items())[:5]:  # 재고 판별이 맞는지 로그로 확인용 샘플
        print(f"   [{'O' if v['available'] else 'X'}] {v['name']} ({v['reason']})")

    if first_run:
        print(f"{now()} 첫 실행이라 기준점만 저장합니다. 이제부터 변동을 알립니다.")
        save_state(current)
        return

    alerts = compute_alerts(state, current)
    if alerts:
        for kind, info in alerts:
            print(f"{now()}   ▶ {'재입고' if kind == 'restock' else '신상품'}: {info['name']}")
        notify(DISCORD_WEBHOOK_URL, alerts)
    else:
        print(f"{now()} 변동 없음.")
    save_state(current)


if __name__ == "__main__":
    run_once()
