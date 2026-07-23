# -*- coding: utf-8 -*-
"""
인스타그램 자동화 (서버판) — GitHub Actions에서 매일 실행됩니다.
1) 인기 해시태그에서 뜨는 계정 찾기 (Apify)
2) 상세 분석 → TOP3 선정
3) 카드 이미지 + 게시글 생성, 데이터 축적 (data)
4) 저장소에 커밋/푸시 → 인스타그램 자동 게시

필요한 환경변수: APIFY_TOKEN
인스타그램 열쇠는 Apify 저장금고(key-value store 'insta-secrets')에 보관됩니다.
PREVIEW=1 로 실행하면 게시/푸시 없이 파일만 만듭니다.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, date
from decimal import Decimal
from pathlib import Path

import requests
from apify_client import ApifyClient
from PIL import Image, ImageDraw, ImageFont

INSTA = Path(__file__).parent
REPO = INSTA
DATA = INSTA / "data"
CARDS = INSTA / "cards"
REPORTS = INSTA / "reports"
for d in (DATA, CARDS, REPORTS):
    d.mkdir(exist_ok=True)

GRAPH = "https://graph.instagram.com/v23.0"
RAW_BASE = "https://raw.githubusercontent.com/kds08200820-del/insta/main/cards"
PREVIEW = os.environ.get("PREVIEW") == "1"


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def load_settings():
    return load_json(INSTA / "settings.json", {
        "hashtag_pool": ["일상", "데일리", "여행", "맛집", "운동", "패션", "카페", "브이로그"],
        "tags_per_day": 2,
        "posts_per_tag": 20,
        "candidates_per_day": 5,
    })


# ---------- 비밀 열쇠 보관함 (Apify key-value store) ----------
def get_secret_store(client):
    store_info = client.key_value_stores().get_or_create(name="insta-secrets")
    store_id = getattr(store_info, "id", None) or store_info["id"]
    return client.key_value_store(store_id)


def get_ig_secrets(kv, st):
    """보관함에 열쇠가 있으면 그걸 쓰고, 없으면(첫 실행) GitHub 금고의 열쇠로 시작"""
    rec = kv.get_record("instagram")
    value = None
    if rec is not None:
        value = getattr(rec, "value", None)
        if value is None and isinstance(rec, dict):
            value = rec.get("value")
        if isinstance(value, (str, bytes)):
            value = json.loads(value)
    if isinstance(value, dict) and value.get("token"):
        return value
    env_token = os.environ.get("IG_TOKEN", "")
    user_id = st.get("ig_user_id", "")
    if not env_token or not user_id:
        raise RuntimeError("인스타그램 열쇠가 없습니다. GitHub 금고(IG_TOKEN)와 settings.json(ig_user_id)을 확인하세요.")
    value = {"token": env_token, "user_id": user_id, "refreshed": ""}
    kv.set_record("instagram", value)
    log("첫 실행: 인스타그램 열쇠를 보관함에 저장했습니다.")
    return value


def refresh_ig_token(kv, sec):
    last = sec.get("refreshed", "")
    try:
        days = (date.today() - date.fromisoformat(last)).days if last else 999
    except ValueError:
        days = 999
    if days < 7:
        return sec
    try:
        r = requests.get(
            "https://graph.instagram.com/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": sec["token"]},
            timeout=30,
        )
        if r.ok and r.json().get("access_token"):
            sec["token"] = r.json()["access_token"]
            sec["refreshed"] = date.today().isoformat()
            kv.set_record("instagram", sec)
            log("인스타그램 열쇠 갱신 완료")
        else:
            log(f"열쇠 갱신 실패(계속 진행): {r.text[:200]}")
    except Exception as e:
        log(f"열쇠 갱신 오류(계속 진행): {e}")
    return sec


# ---------- 1단계: 뜨는 계정 찾기 ----------
def find_trending_posts(client, st):
    pool = st["hashtag_pool"]
    n = max(1, int(st["tags_per_day"]))
    start = (date.today().toordinal() * n) % len(pool)
    tags = [pool[(start + i) % len(pool)] for i in range(min(n, len(pool)))]
    urls = ["https://www.instagram.com/explore/tags/" + urllib.parse.quote(t) + "/" for t in tags]
    log(f"오늘의 해시태그: {', '.join(tags)}")
    run = client.actor("apify/instagram-scraper").call(
        run_input={
            "directUrls": urls,
            "resultsType": "posts",
            "resultsLimit": int(st["posts_per_tag"]),
            "addParentData": False,
        },
        max_items=len(tags) * int(st["posts_per_tag"]) + 15,
        max_total_charge_usd=Decimal("0.25"),
        logger=None,
    )
    if run is None:
        raise RuntimeError("에피파이 수집 실행 실패")
    items = list(client.dataset(run.default_dataset_id).iterate_items())
    log(f"게시물 {len(items)}개 수집")
    return items, tags


def pick_candidates(items, featured, limit):
    scores = {}
    for it in items:
        owner = (it.get("ownerUsername") or "").strip()
        if not owner or owner in featured:
            continue
        likes = max(it.get("likesCount") or 0, 0)
        comments = it.get("commentsCount") or 0
        score = likes + comments * 3
        if score > scores.get(owner, -1):
            scores[owner] = score
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [u for u, _ in ranked[:limit]]


# ---------- 2단계: 상세 분석 ----------
def analyze_profiles(client, usernames):
    log(f"후보 분석: {', '.join(usernames)}")
    run = client.actor("apify/instagram-profile-scraper").call(
        run_input={"usernames": usernames},
        max_total_charge_usd=Decimal("0.25"),
        logger=None,
    )
    if run is None:
        raise RuntimeError("에피파이 분석 실행 실패")
    profiles = []
    for p in client.dataset(run.default_dataset_id).iterate_items():
        followers = p.get("followersCount") or 0
        posts = p.get("latestPosts") or []
        engs = [
            (x.get("likesCount") or 0) + (x.get("commentsCount") or 0)
            for x in posts if (x.get("likesCount") or 0) >= 0
        ]
        avg = sum(engs) / len(engs) if engs else 0
        # 반응 좋은 순으로 대표 콘텐츠 3개 보관 (벤치마킹 검토용)
        ranked_posts = sorted(
            posts,
            key=lambda x: max(x.get("likesCount") or 0, 0) + (x.get("commentsCount") or 0),
            reverse=True,
        )[:3]
        top_posts = [{
            "url": x.get("url", ""),
            "caption": (x.get("caption") or "").replace("\n", " ")[:120],
            "likes": max(x.get("likesCount") or 0, 0),
            "comments": x.get("commentsCount") or 0,
            "type": x.get("type", ""),
            "displayUrl": x.get("displayUrl", ""),
        } for x in ranked_posts]
        profiles.append({
            "username": p.get("username", ""),
            "fullName": p.get("fullName", ""),
            "followers": followers,
            "postsCount": p.get("postsCount") or 0,
            "bio": (p.get("biography") or "").replace("\n", " ")[:100],
            "avg_engagement": round(avg),
            "engagement_rate": round(avg / followers * 100, 2) if followers else 0,
            "top_posts": top_posts,
        })
    return profiles


def save_content_thumbs(top3, today):
    """TOP3 인플루언서의 대표 콘텐츠 섬네일을 내려받아 저장 (실패해도 계속)"""
    folder = INSTA / "content" / today
    folder.mkdir(parents=True, exist_ok=True)
    for p in top3:
        for i, post in enumerate(p.get("top_posts", [])):
            src = post.pop("displayUrl", "")
            post["thumb"] = ""
            if not src:
                continue
            try:
                r = requests.get(src, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
                r.raise_for_status()
                from io import BytesIO
                img = Image.open(BytesIO(r.content)).convert("RGB")
                w = 480
                img = img.resize((w, int(img.height * w / img.width)))
                name = f"{p['username']}_{i}.jpg".replace("/", "_")
                img.save(folder / name, "JPEG", quality=80)
                post["thumb"] = f"content/{today}/{name}"
            except Exception as e:
                log(f"섬네일 저장 실패({p['username']} {i}): {e}")


def pick_top3(profiles):
    pool = [p for p in profiles if 5000 <= p["followers"] <= 1_000_000]
    if len(pool) < 3:
        pool = [p for p in profiles if p["followers"] >= 1000] or profiles
    pool.sort(key=lambda p: p["engagement_rate"], reverse=True)
    return pool[:3]


# ---------- 3단계: 카드/글/데이터 ----------
def clean_text(s):
    s = re.sub(r"[^가-힣ㄱ-ㆎ\x20-\x7E·%…~]", "", s or "")
    return re.sub(r"\s+", " ", s).strip()


def fmt_num(n):
    if n >= 100_000_000:
        return f"{n/100_000_000:.1f}억"
    if n >= 10_000:
        return f"{n/10_000:.1f}만"
    if n >= 1_000:
        return f"{n/1_000:.1f}천"
    return str(n)


def font(size, bold=False):
    candidates = (
        ["C:/Windows/Fonts/malgunbd.ttf", "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf"]
        if bold else
        ["C:/Windows/Fonts/malgun.ttf", "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"]
    )
    for c in candidates:
        if Path(c).exists():
            return ImageFont.truetype(c, size)
    raise RuntimeError("한글 글꼴을 찾지 못했습니다.")


def make_card(top3, today_str):
    W, H = 1080, 1350
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    top_c, bot_c = (18, 24, 58), (76, 40, 110)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(top_c[i] + (bot_c[i] - top_c[i]) * t) for i in range(3)))

    gold, white, grey = (255, 209, 102), (245, 245, 250), (200, 200, 215)
    d.text((W // 2, 110), "오늘 뜨는 인플루언서", font=font(76, True), fill=white, anchor="mm")
    d.text((W // 2, 200), "TOP 3", font=font(76, True), fill=gold, anchor="mm")
    d.text((W // 2, 280), today_str, font=font(40), fill=grey, anchor="mm")

    y = 400
    for i, p in enumerate(top3):
        d.rounded_rectangle([70, y, W - 70, y + 250], radius=28, fill=(32, 34, 70))
        d.text((120, y + 60), f"{i+1}", font=font(64, True), fill=gold, anchor="lm")
        d.text((210, y + 60), f"@{p['username']}", font=font(52, True), fill=white, anchor="lm")
        meta = f"팔로워 {fmt_num(p['followers'])} · 반응 {fmt_num(p['avg_engagement'])} · 반응률 {p['engagement_rate']:.1f}%"
        d.text((210, y + 130), meta, font=font(38), fill=grey, anchor="lm")
        bio = clean_text(p["bio"])
        bio = bio[:38] + ("…" if len(bio) > 38 else "")
        if bio:
            d.text((210, y + 190), bio, font=font(32), fill=(160, 165, 195), anchor="lm")
        y += 290

    d.line([(120, H - 120), (W - 120, H - 120)], fill=(120, 110, 170), width=2)
    d.text((W // 2, H - 70), "매일 아침, 오늘의 인스타 트렌드 · @dongseok_kim19", font=font(34), fill=grey, anchor="mm")

    out = CARDS / f"{date.today().isoformat()}.jpg"
    img.save(out, "JPEG", quality=92)
    log(f"카드 저장: {out.name}")
    return out


def make_caption(top3, today_str):
    lines = [f"📈 오늘 뜨는 인플루언서 TOP3 ({today_str})", ""]
    for i, p in enumerate(top3):
        lines.append(f"{i+1}위 @{p['username']} — 팔로워 {fmt_num(p['followers'])}, 반응률 {p['engagement_rate']}%")
    lines += [
        "",
        "반응률 = 최근 게시물의 평균 (좋아요+댓글) ÷ 팔로워",
        "매일 인기 해시태그를 분석해 상승세인 계정을 소개합니다.",
        "",
        "#인스타트렌드 #인플루언서 #트렌드분석 #데일리 #마케팅",
    ]
    return "\n".join(lines)


def write_report(top3, profiles, today_str):
    out = REPORTS / f"{date.today().isoformat()}.md"
    lines = [f"# 인플루언서 분석 보고서 — {today_str}", "", "## 오늘의 TOP 3"]
    for i, p in enumerate(top3):
        lines += [
            f"### {i+1}위 @{p['username']} ({p['fullName']})",
            f"- 팔로워 {p['followers']:,} / 게시물 {p['postsCount']:,}",
            f"- 평균 반응 {p['avg_engagement']:,} (반응률 {p['engagement_rate']}%)",
            f"- 소개글: {p['bio']}",
            f"- https://www.instagram.com/{p['username']}/",
            "",
        ]
    lines.append("## 함께 검토한 후보")
    for p in profiles:
        lines.append(f"- @{p['username']}: 팔로워 {p['followers']:,}, 반응률 {p['engagement_rate']}%")
    out.write_text("\n".join(lines), encoding="utf-8")


# ---------- 4단계: 저장소 반영 + 인스타그램 게시 ----------
def telegram_report(top3, today_str, card_path):
    """텔레그램으로 오늘의 서칭·검토 결과 보고 (설정 안 되어 있으면 조용히 건너뜀)"""
    token = os.environ.get("TELEGRAM_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        log("텔레그램 미설정 — 보고 건너뜀")
        return
    api = f"https://api.telegram.org/bot{token}"
    try:
        with open(card_path, "rb") as f:
            requests.post(
                f"{api}/sendPhoto",
                data={"chat_id": chat_id, "caption": f"📈 {today_str} 인플루언서 분석 완료"},
                files={"photo": f},
                timeout=60,
            )
        lines = [f"오늘의 TOP3 상세 ({today_str})", ""]
        for i, p in enumerate(top3):
            lines.append(f"{i+1}위 @{p['username']} — 팔로워 {fmt_num(p['followers'])}, 반응률 {p['engagement_rate']}%")
            for j, post in enumerate(p.get("top_posts", [])):
                cap = clean_text(post.get("caption", ""))[:40]
                lines.append(f"   콘텐츠{j+1}: ❤{fmt_num(post['likes'])} 💬{fmt_num(post['comments'])} {cap}")
                lines.append(f"   {post['url']}")
            lines.append("")
        lines.append("전체 기록: https://kds08200820-del.github.io/insta/")
        requests.post(
            f"{api}/sendMessage",
            data={"chat_id": chat_id, "text": "\n".join(lines), "disable_web_page_preview": "true"},
            timeout=60,
        )
        log("텔레그램 보고 완료")
    except Exception as e:
        log(f"텔레그램 보고 실패(계속 진행): {e}")


def git_push_data(today):
    def git(*args):
        r = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    git("config", "user.name", "insta-bot")
    git("config", "user.email", "insta-bot@users.noreply.github.com")
    git("add", "data", "cards", "reports", "content")
    r = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO)
    if r.returncode == 0:
        log("저장할 변경 사항 없음")
        return
    git("commit", "-m", f"인스타 자동화 데이터 {today}")
    git("pull", "--rebase", "origin", "main")
    git("push", "origin", "main")
    log("데이터 저장소 반영 완료")


def wait_for_url(url, tries=24, delay=5):
    for _ in range(tries):
        try:
            if requests.head(url, timeout=10).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(delay)
    return False


def publish_to_instagram(sec, image_url, caption):
    uid, token = sec["user_id"], sec["token"]
    r = requests.post(
        f"{GRAPH}/{uid}/media",
        data={"image_url": image_url, "caption": caption, "access_token": token},
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError("게시물 준비 실패: " + r.text[:300])
    creation_id = r.json()["id"]
    for _ in range(20):
        s = requests.get(
            f"{GRAPH}/{creation_id}",
            params={"fields": "status_code", "access_token": token},
            timeout=30,
        ).json()
        if s.get("status_code") == "FINISHED":
            break
        if s.get("status_code") == "ERROR":
            raise RuntimeError("게시물 준비 오류: " + json.dumps(s))
        time.sleep(3)
    r2 = requests.post(
        f"{GRAPH}/{uid}/media_publish",
        data={"creation_id": creation_id, "access_token": token},
        timeout=60,
    )
    if not r2.ok:
        raise RuntimeError("게시 실패: " + r2.text[:300])
    log(f"인스타그램 게시 완료: {r2.json().get('id')}")


def main():
    apify_token = os.environ.get("APIFY_TOKEN", "")
    if not apify_token:
        print("APIFY_TOKEN 환경변수가 없습니다.")
        sys.exit(1)

    st = load_settings()
    client = ApifyClient(apify_token)
    today = date.today().isoformat()
    today_str = f"{date.today():%Y년 %m월 %d일}"

    kv = get_secret_store(client)
    sec = get_ig_secrets(kv, st)
    sec = refresh_ig_token(kv, sec)

    featured = load_json(DATA / "featured.json", {})
    items, tags = find_trending_posts(client, st)
    candidates = pick_candidates(items, featured, int(st["candidates_per_day"]))
    if not candidates:
        log("새 후보 없음 — 오늘은 종료")
        return
    profiles = analyze_profiles(client, candidates)
    top3 = pick_top3(profiles)
    if not top3:
        log("소개할 계정 없음 — 종료")
        return

    save_content_thumbs(top3, today)
    card_path = make_card(top3, today_str)
    caption = make_caption(top3, today_str)
    write_report(top3, profiles, today_str)
    telegram_report(top3, today_str, card_path)
    save_json(DATA / f"{today}.json", {"date": today, "tags": tags, "top3": top3, "candidates": profiles})

    index = load_json(DATA / "index.json", {"days": []})
    index["days"] = [d for d in index["days"] if d["date"] != today]
    index["days"].append({
        "date": today,
        "tags": tags,
        "top3": [{k: p[k] for k in ("username", "followers", "avg_engagement", "engagement_rate")} for p in top3],
        "card": f"cards/{today}.jpg",
    })
    index["days"].sort(key=lambda d: d["date"])
    save_json(DATA / "index.json", index)

    for p in top3:
        featured[p["username"]] = today
    save_json(DATA / "featured.json", featured)

    if PREVIEW:
        log("미리보기 모드 — 푸시/게시 생략")
        return

    git_push_data(today)
    image_url = f"{RAW_BASE}/{today}.jpg"
    if not wait_for_url(image_url):
        raise RuntimeError("카드 이미지 주소가 아직 열리지 않습니다: " + image_url)
    publish_to_instagram(sec, image_url, caption)
    log("오늘 작업 전부 완료!")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"오류로 중단: {e}")
        sys.exit(1)
