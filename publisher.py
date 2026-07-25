# -*- coding: utf-8 -*-
"""통합 발행기 — 요일을 보고 오늘 올릴 콘텐츠를 결정해 인스타그램에 한 번만 게시한다.

  python publisher.py                 # 오늘 요일의 콘텐츠 발행
  python publisher.py --slot supply_radar
  python publisher.py --dry-run       # 생성만 하고 게시하지 않음
  python publisher.py --force         # 오늘 이미 발행했어도 다시 발행

설계
  · calendar.json 이 "요일 → 콘텐츠" 단일 진실 소스.
  · data/publish_log.json 에 발행 이력을 남겨 하루 두 번 올라가는 사고를 막는다.
  · 인스타 토큰은 Apify 저장금고(automation.py 와 동일)에서 가져와 자동 갱신한다.

환경변수
  APIFY_TOKEN     (필수) 토큰 저장금고 접근
  QUANT_PASSWORD  수급 레이더용 (quant-dashboard APP_PASSWORD)
  TELEGRAM_TOKEN / TELEGRAM_CHAT_ID  (선택) 발행 결과 알림
"""
import os, sys, json, time, argparse, pathlib, subprocess, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                            # noqa: BLE001
    pass

HERE = pathlib.Path(__file__).resolve().parent
DATA = HERE / "data"
CARDS = HERE / "cards"
LOG = DATA / "publish_log.json"
KST = timezone(timedelta(hours=9))
GRAPH = "https://graph.instagram.com/v23.0"
RAW_BASE = "https://raw.githubusercontent.com/kds08200820-del/insta/main/cards"
WD = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def log(m):
    print(f"[{datetime.now(KST):%H:%M:%S}] {m}", flush=True)


def load_json(p, d):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else d


def save_json(p, o):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(o, ensure_ascii=False, indent=2), encoding="utf-8")


def cfg():
    return load_json(HERE / "calendar.json", {})


# ------------------------- 인스타 토큰 (automation.py 와 동일한 금고) -------------------------
def ig_secrets():
    """Apify key-value store 'insta-secrets' 에서 토큰을 읽고, 만료가 가까우면 갱신한다."""
    from apify_client import ApifyClient
    tok = os.environ.get("APIFY_TOKEN", "").strip()
    if not tok:
        raise SystemExit("APIFY_TOKEN 이 필요합니다.")
    client = ApifyClient(tok)
    store = client.key_value_stores().get_or_create(name="insta-secrets")
    kv = client.key_value_store(store["id"])
    sec = (kv.get_record("ig") or {}).get("value") or {}
    if not sec.get("token"):
        env_tok = os.environ.get("IG_TOKEN", "").strip()
        if not env_tok:
            raise SystemExit("저장금고에도 IG_TOKEN 환경변수에도 인스타 토큰이 없습니다.")
        sec = {"token": env_tok, "user_id": str(cfg().get("ig_user_id") or
               load_json(HERE / "settings.json", {}).get("ig_user_id", "")), "refreshed": ""}
    # 24시간 넘었으면 장기 토큰 갱신(만료 60일 → 매 실행마다 연장)
    last = sec.get("refreshed") or ""
    stale = True
    if last:
        try:
            stale = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).days >= 1
        except Exception:                                    # noqa: BLE001
            stale = True
    if stale:
        try:
            q = urllib.parse.urlencode({"grant_type": "ig_refresh_token", "access_token": sec["token"]})
            with urllib.request.urlopen(f"{GRAPH}/refresh_access_token?{q}", timeout=30) as r:
                new = json.loads(r.read())
            if new.get("access_token"):
                sec["token"] = new["access_token"]
                sec["refreshed"] = datetime.now(timezone.utc).isoformat()
                kv.set_record("ig", sec)
                log(f"인스타 토큰 갱신 완료 (만료 {new.get('expires_in', 0)//86400}일 뒤)")
        except Exception as e:                               # noqa: BLE001
            log(f"토큰 갱신 실패(계속 진행): {e}")
    if not sec.get("user_id"):
        sec["user_id"] = str(load_json(HERE / "settings.json", {}).get("ig_user_id", ""))
    return sec


# ------------------------- 인스타 게시 -------------------------
def _post(path, params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{GRAPH}/{path}", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())


def _get(path, params):
    with urllib.request.urlopen(f"{GRAPH}/{path}?{urllib.parse.urlencode(params)}", timeout=60) as r:
        return json.loads(r.read())


def wait_url(url, tries=24, delay=5):
    """이미지가 공개 URL 로 접근 가능해질 때까지 대기(인스타가 직접 가져가야 함)."""
    for i in range(tries):
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=25) as r:
                if r.status == 200:
                    return True
        except Exception:                                    # noqa: BLE001
            pass
        time.sleep(delay)
    return False


def publish_single(sec, image_url, caption):
    r = _post(f"{sec['user_id']}/media", {"image_url": image_url, "caption": caption,
                                          "access_token": sec["token"]})
    cid = r["id"]
    pub = _post(f"{sec['user_id']}/media_publish", {"creation_id": cid, "access_token": sec["token"]})
    return pub["id"]


def publish_carousel(sec, image_urls, caption):
    children = []
    for u in image_urls:
        r = _post(f"{sec['user_id']}/media", {"image_url": u, "is_carousel_item": "true",
                                              "access_token": sec["token"]})
        children.append(r["id"])
        log(f"  item {r['id']} ← {u}")
    r = _post(f"{sec['user_id']}/media", {"media_type": "CAROUSEL", "children": ",".join(children),
                                          "caption": caption, "access_token": sec["token"]})
    cid = r["id"]
    for _ in range(10):
        st = _get(cid, {"fields": "status_code", "access_token": sec["token"]}).get("status_code")
        if st == "FINISHED":
            break
        if st == "ERROR":
            raise SystemExit(f"컨테이너 오류: {cid}")
        time.sleep(3)
    pub = _post(f"{sec['user_id']}/media_publish", {"creation_id": cid, "access_token": sec["token"]})
    return pub["id"]


def permalink(sec, media_id):
    try:
        return _get(media_id, {"fields": "permalink", "access_token": sec["token"]}).get("permalink")
    except Exception:                                        # noqa: BLE001
        return None


# ------------------------- 콘텐츠 생성기 -------------------------
def gen_supply_radar(c, dry):
    """수급 레이더 — 카드 1장을 만들고 레포에 커밋해 raw URL 로 게시."""
    import gen_radar
    today = datetime.now(KST).strftime("%Y-%m-%d")
    out = CARDS / f"radar-{today}.png"
    gen_radar.warm()
    data = gen_radar.fetch_radar()
    if not (data.get("buy") or []):
        log("수급 데이터가 비어 있어 오늘은 건너뜁니다."); return None
    top_n = int((c.get("slots", {}).get("supply_radar", {}) or {}).get("top_n", 7))
    gen_radar.render(gen_radar.build_html(data, top_n), str(out))
    caption = gen_radar.make_caption(data, top_n)
    return {"kind": "supply_radar", "images": [str(out)], "caption": caption,
            "urls": [f"{RAW_BASE}/{out.name}"], "needs_commit": True}


def gen_market_review(c, dry):
    """주간 마켓 리뷰 — quant-dashboard 가 만들어 둔 10장을 그대로 게시(이미지는 Render 공개 URL)."""
    base = (c.get("quant_base") or "https://quant-dashboard-8ddy.onrender.com").rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/api/cards", timeout=60) as r:
            man = json.loads(r.read())
    except Exception as e:                                   # noqa: BLE001
        log(f"카드뉴스 매니페스트를 못 읽었습니다: {e}"); return None
    weeks = man.get("weeks") or []
    target = next((w for w in weeks if not w.get("published")), None)
    if not target:
        log("게시할 미게시 주차가 없습니다 — 오늘은 건너뜁니다."); return None
    urls = [f"{base}/{target['folder']}/{n}" for n in target["images"]]
    return {"kind": "market_review", "images": [], "caption": target.get("caption", ""),
            "urls": urls, "needs_commit": False, "week_id": target.get("id")}


def gen_trend_top3(c, dry):
    """인스타 급성장 계정 TOP3 — 기존 automation.py 를 그대로 실행(자체 게시까지 수행)."""
    env = dict(os.environ)
    if dry:
        env["PREVIEW"] = "1"
    log("automation.py 실행 (트렌드 TOP3)")
    p = subprocess.run([sys.executable, str(HERE / "automation.py")], env=env,
                       capture_output=True, text=True, timeout=1800)
    sys.stdout.write(p.stdout[-4000:])
    if p.returncode != 0:
        sys.stderr.write(p.stderr[-2000:])
        raise SystemExit(f"automation.py 실패 (exit {p.returncode})")
    return {"kind": "trend_top3", "self_published": True}


GENERATORS = {"supply_radar": gen_supply_radar,
              "market_review": gen_market_review,
              "trend_top3": gen_trend_top3}


# ------------------------- 부가 -------------------------
def git_commit(paths, msg):
    def g(*a):
        subprocess.run(["git", *a], cwd=str(HERE), check=True, capture_output=True, text=True)
    try:
        g("config", "user.name", "github-actions[bot]")
        g("config", "user.email", "github-actions[bot]@users.noreply.github.com")
        g("add", *[str(p) for p in paths])
        st = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=str(HERE))
        if st.returncode == 0:
            log("커밋할 변경 없음"); return True
        g("commit", "-m", msg)
        g("push")
        log("커밋·푸시 완료")
        return True
    except subprocess.CalledProcessError as e:
        log(f"git 실패: {e.stderr[:300] if e.stderr else e}")
        return False


def telegram(msg):
    tok, chat = os.environ.get("TELEGRAM_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    if not (tok and chat):
        return
    try:
        d = urllib.parse.urlencode({"chat_id": chat, "text": msg,
                                    "disable_web_page_preview": "true"}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage", data=d), timeout=20).read()
    except Exception:                                        # noqa: BLE001
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", default=None, help="요일 대신 강제로 지정")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="오늘 이미 발행했어도 재발행")
    a = ap.parse_args()

    c = cfg()
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    wd = WD[now.weekday()]
    slot = a.slot or (c.get("week", {}) or {}).get(wd, "off")
    label = ((c.get("slots", {}) or {}).get(slot, {}) or {}).get("label", slot)
    log(f"오늘 {today}({wd}) → 슬롯 '{slot}' ({label})")

    if slot in ("off", None, ""):
        log("오늘은 발행하지 않는 요일입니다."); return

    hist = load_json(LOG, {"posts": []})
    if not a.force and any(p["date"] == today and p.get("status") == "published" for p in hist["posts"]):
        log("오늘 이미 발행했습니다 — 중복 방지로 종료 (--force 로 재발행 가능)"); return

    gen = GENERATORS.get(slot)
    if not gen:
        raise SystemExit(f"알 수 없는 슬롯: {slot}")

    res = gen(c, a.dry_run)
    if res is None:
        log("생성할 내용이 없어 종료합니다."); return

    # automation.py 는 자체적으로 게시까지 끝냄
    if res.get("self_published"):
        hist["posts"].append({"date": today, "weekday": wd, "slot": slot,
                              "status": "preview" if a.dry_run else "published",
                              "at": now.isoformat()})
        save_json(LOG, hist)
        if not a.dry_run:
            git_commit([LOG], f"chore(log): {today} {slot} 발행 기록")
        telegram(f"✅ {today}({wd}) {label} 발행 완료")
        return

    if a.dry_run:
        log(f"[dry-run] 게시 생략. 이미지 {len(res['urls'])}장\n" + "\n".join("  " + u for u in res["urls"]))
        log("캡션 미리보기:\n" + (res.get("caption") or "")[:400])
        return

    # 이미지가 레포에 있어야 하는 경우 먼저 커밋(→ raw URL 공개)
    if res.get("needs_commit") and res.get("images"):
        if not git_commit(res["images"], f"feat(card): {today} {slot} 카드"):
            raise SystemExit("이미지 커밋 실패 — 게시 중단")
        if not wait_url(res["urls"][0]):
            raise SystemExit("이미지가 공개 URL 로 확인되지 않아 게시를 중단합니다.")

    sec = ig_secrets()
    log(f"인스타 게시 시작 — {len(res['urls'])}장")
    media_id = (publish_carousel(sec, res["urls"], res["caption"]) if len(res["urls"]) > 1
                else publish_single(sec, res["urls"][0], res["caption"]))
    link = permalink(sec, media_id)
    log(f"✅ 게시 완료 media_id={media_id} {link or ''}")

    hist["posts"].append({"date": today, "weekday": wd, "slot": slot, "status": "published",
                          "mediaId": media_id, "permalink": link, "at": now.isoformat(),
                          **({"weekId": res["week_id"]} if res.get("week_id") else {})})
    save_json(LOG, hist)
    git_commit([LOG], f"chore(log): {today} {slot} 발행 기록")
    telegram(f"✅ {today}({wd}) {label} 발행 완료\n{link or ''}")


if __name__ == "__main__":
    main()
