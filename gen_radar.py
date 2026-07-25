# -*- coding: utf-8 -*-
"""개인 수급 레이더 카드 생성기 (인스타 1장, 1080×1350).

quant-dashboard 의 /api/radar (개인 순매수/순매도 추정 랭킹)를 읽어
마켓 리뷰 카드뉴스와 같은 디자인 언어로 카드 1장을 만든다.

  python gen_radar.py                 # cards/radar-<날짜>.png 생성
  python gen_radar.py --out out.png

환경변수:
  QUANT_BASE      기본 https://quant-dashboard-8ddy.onrender.com
  QUANT_PASSWORD  quant-dashboard 의 APP_PASSWORD (Basic 인증). /api/radar 가 보호되어 있어 필요.
  RENDER_SCALE    기본 2 (2160×2700 출력)
"""
import os, sys, json, base64, argparse, pathlib, subprocess, tempfile, shutil
import urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                            # noqa: BLE001
    pass

HERE = pathlib.Path(__file__).resolve().parent
KST = timezone(timedelta(hours=9))
BASE = os.environ.get("QUANT_BASE", "https://quant-dashboard-8ddy.onrender.com").rstrip("/")
W, H = 1080, 1350


# ---------------- 데이터 ----------------
def fetch_radar():
    req = urllib.request.Request(f"{BASE}/api/radar")
    pw = os.environ.get("QUANT_PASSWORD", "").strip()
    if pw:
        tok = base64.b64encode(f"x:{pw}".encode()).decode()
        req.add_header("Authorization", "Basic " + tok)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise SystemExit("[radar] 401 — QUANT_PASSWORD(=quant-dashboard APP_PASSWORD) 가 필요합니다.")
        raise SystemExit(f"[radar] HTTP {e.code}: {e.read().decode()[:200]}")


def warm():
    """Render 무료 플랜 슬립 대비 — 먼저 깨운다."""
    for _ in range(8):
        try:
            with urllib.request.urlopen(f"{BASE}/api/health", timeout=25) as r:
                if r.status == 200:
                    return True
        except Exception:                                    # noqa: BLE001
            pass
    return False


# ---------------- 렌더 ----------------
CSS = """
*{margin:0;padding:0;box-sizing:border-box}
:root{--ink:#F3F6FB;--ink2:#AEBAD0;--muted:#6B7280;--up:#D94A4A;--up-b:#F26D6D;
      --down:#2E6FD9;--down-b:#5D93F2;--line:rgba(255,255,255,.10);--surface:rgba(255,255,255,.045)}
html,body{width:1080px;height:1350px}
body{font-family:'Noto Sans KR','Noto Sans CJK KR','Malgun Gothic',sans-serif;
 background:radial-gradient(1200px 700px at 78% -12%,rgba(46,111,217,.22),transparent 60%),
            radial-gradient(900px 600px at 8% 108%,rgba(217,74,74,.13),transparent 60%),
            linear-gradient(160deg,#0C1528 0%,#0B1322 55%,#090F1C 100%);
 color:var(--ink);-webkit-font-smoothing:antialiased;overflow:hidden}
.page{width:1080px;height:1350px;padding:76px 72px 64px;display:flex;flex-direction:column}
.head{display:flex;align-items:center;justify-content:space-between}
.kick{font-size:21px;letter-spacing:.14em;font-weight:700;color:var(--ink2);text-transform:uppercase}
.kick .dot{color:var(--down-b)}
.pg{font-size:20px;letter-spacing:.14em;color:var(--muted);font-weight:600}
.body{flex:1;display:flex;flex-direction:column;justify-content:center}
.tag{display:inline-flex;align-items:center;gap:10px;font-size:22px;font-weight:700;color:var(--up-b);margin-bottom:20px}
.tag::before{content:"";width:30px;height:3px;border-radius:2px;background:var(--up-b)}
h1{font-size:50px;line-height:1.26;font-weight:800;letter-spacing:-.01em;margin-bottom:12px}
.lead{font-size:25px;line-height:1.55;color:var(--ink2);margin-bottom:30px}
.lead b{color:var(--ink);font-weight:700}
.rows{display:flex;flex-direction:column;gap:11px}
.row{display:grid;grid-template-columns:52px 1fr auto auto;align-items:center;gap:16px;
     background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:20px 26px}
.row.top{background:linear-gradient(90deg,rgba(217,74,74,.14),var(--surface))}
.rk{font-size:24px;font-weight:800;color:var(--muted);font-variant-numeric:tabular-nums}
.row.top .rk{color:var(--up-b)}
.nm{font-size:29px;font-weight:700;color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sub{font-size:18px;color:var(--muted);font-weight:600;margin-top:3px}
.amt{font-size:29px;font-weight:800;color:var(--up-b);font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
.st{font-size:19px;color:var(--ink2);font-weight:700;text-align:right;white-space:nowrap;min-width:96px}
.foot{font-size:17px;color:var(--muted);line-height:1.5}
"""


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_html(data, top_n=7):
    buy = (data.get("buy") or [])[:top_n]
    as_of = data.get("asOf") or ""
    rows = ""
    for i, r in enumerate(buy, 1):
        streak = r.get("streak") or 0
        st = f"{streak}일 연속" if streak >= 2 else "&nbsp;"
        cap = r.get("capPct")
        sub = f"시총 대비 {cap}%" if cap else (r.get("code") or "")
        rows += (f'<div class="row{" top" if i == 1 else ""}">'
                 f'<div class="rk">{i:02d}</div>'
                 f'<div><div class="nm">{esc(r.get("name") or r.get("code"))}</div>'
                 f'<div class="sub">{esc(sub)}</div></div>'
                 f'<div class="amt">+{r.get("indivEok", 0):,.1f}억</div>'
                 f'<div class="st">{st}</div></div>')
    head = buy[0] if buy else {}
    return f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"><style>{CSS}</style></head>
<body><div class="page">
<div class="head"><div class="kick">Supply Radar <span class="dot">·</span> 개인 수급</div>
<div class="pg">{esc(as_of)}</div></div>
<div class="body">
  <div class="tag">오늘 개인이 가장 많이 담은 종목</div>
  <h1>개인 순매수 상위 <span style="color:var(--up-b)">{len(buy)}</span>종목</h1>
  <p class="lead">코스닥 거래대금 상위 종목 중 <b>개인 순매수 추정액</b> 기준입니다.
     1위는 <b>{esc(head.get("name", "-"))}</b> <b style="color:var(--up-b)">+{head.get("indivEok", 0):,.1f}억</b>.</p>
  <div class="rows">{rows}</div>
</div>
<div class="foot">개인 순매수 = −(기관+외국인) 추정치 · 코스닥 거래대금 상위 60 · 출처: 네이버 금융<br>
정보 제공 목적이며 특정 종목의 매매를 권유하지 않습니다.</div>
</div></body></html>"""


def find_chrome():
    env = os.environ.get("CHROME_BIN")
    if env and os.path.exists(env):
        return env
    for c in ("google-chrome", "chromium", "chromium-browser", "chrome",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
        p = shutil.which(c) if os.sep not in c else (c if os.path.exists(c) else None)
        if p:
            return p
    return None


def render(html, out_png, scale=None):
    scale = scale or os.environ.get("RENDER_SCALE", "2")
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="radar_"))
    hp = tmp / "card.html"
    hp.write_text(html, encoding="utf-8")
    out_png = os.path.abspath(out_png)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = b.new_context(viewport={"width": W, "height": H}, device_scale_factor=float(scale))
            pg = ctx.new_page()
            pg.goto(hp.resolve().as_uri(), wait_until="load", timeout=60000)
            pg.wait_for_timeout(250)
            pg.screenshot(path=out_png)
            ctx.close(); b.close()
    except ImportError:
        chrome = find_chrome()
        if not chrome:
            raise SystemExit("playwright 도 Chrome 도 없습니다. `pip install playwright && playwright install chromium`")
        subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
                        "--disable-dev-shm-usage", f"--user-data-dir={tmp/'prof'}",
                        f"--force-device-scale-factor={scale}", f"--window-size={W},{H}",
                        f"--screenshot={out_png}", hp.resolve().as_uri()],
                       check=True, capture_output=True, timeout=120)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not os.path.exists(out_png) or os.path.getsize(out_png) == 0:
        raise SystemExit("[radar] 렌더 실패")
    return out_png


def make_caption(data, top_n=7):
    buy = (data.get("buy") or [])[:top_n]
    as_of = data.get("asOf") or ""
    lines = [f"📡 개인 수급 레이더 | {as_of}", "",
             "오늘 개인이 가장 많이 담은 종목은?", ""]
    for i, r in enumerate(buy, 1):
        s = r.get("streak") or 0
        tail = f" ({s}일 연속)" if s >= 2 else ""
        lines.append(f"{i}. {r.get('name')} +{r.get('indivEok', 0):,.1f}억{tail}")
    lines += ["", "개인 순매수 = −(기관+외국인) 추정치",
              "코스닥 거래대금 상위 60종목 기준 · 출처: 네이버 금융", "",
              "매일 아침 수급·트렌드 브리핑 📌", "",
              "※ 정보 제공 목적이며 특정 종목의 매매를 권유하지 않습니다. 투자 판단의 책임은 투자자 본인에게 있습니다.",
              "", "#주식 #코스닥 #수급 #개인순매수 #단타 #스윙 #주식투자 #증시 #재테크 #투자공부"]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--top", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    warm()
    data = fetch_radar()
    buy = data.get("buy") or []
    print(f"[radar] asOf={data.get('asOf')} universe={data.get('universe')} buy={len(buy)}")
    if not buy:
        raise SystemExit("[radar] 순매수 데이터가 비어 있습니다 — 오늘은 발행하지 않습니다.")

    today = datetime.now(KST).strftime("%Y-%m-%d")
    out = a.out or str(HERE / "cards" / f"radar-{today}.png")
    pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
    cap = make_caption(data, a.top)

    if a.dry_run:
        print("[dry-run] 카드/캡션만 확인하고 종료\n" + "-" * 40 + f"\n{cap[:400]}")
        render(build_html(data, a.top), out)
        print(f"[radar] 미리보기 저장 → {out}")
        return

    render(build_html(data, a.top), out)
    print(f"[radar] 카드 저장 → {out} ({os.path.getsize(out)} bytes)")
    meta = {"kind": "supply_radar", "date": today, "image": out, "caption": cap,
            "asOf": data.get("asOf")}
    print("RADAR_META=" + json.dumps(meta, ensure_ascii=False))
    return meta


if __name__ == "__main__":
    main()
