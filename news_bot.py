# -*- coding: utf-8 -*-
"""
아침 뉴스 한 통 — 무료 뉴스(RSS)를 모아 Claude에게 한 번 요약시키고, 텔레그램으로 한 통 보낸다.

순서
  1) 뉴스 모으기 (돈 안 듦)        collect_articles()
  2) AI 요약 1회 (돈 나감)          summarize()      ← 여기서만 돈이 나간다
  3) 텔레그램 한 통 보내기 (돈 안 듦) send_telegram()

비밀값 3개는 깃허브 Secrets 에만 넣는다. 코드에 적지 않는다.
  ANTHROPIC_API_KEY  — 클로드 API 열쇠
  BOT_TOKEN          — 텔레그램 봇 토큰
  CHAT_ID            — 내 텔레그램 채팅 번호
"""
import datetime as dt
import email.utils
import html
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import json

# 윈도우에서 기록 파일로 출력을 보낼 때 한글·이모지·줄표(—) 때문에 멈추지 않게 한다
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────
# 1. 설정 — 수강생이 바꿔도 되는 곳
# ─────────────────────────────────────────────────────────────

# 쓸 AI 모델. Haiku 5.5: 100만 토큰당 입력 $0.10 / 출력 $0.50
#   더 잘 쓰게 하고 싶으면 "claude-sonnet-5-5" (입력 $2 / 출력 $10) — 값이 올라가니 바꾼 뒤 Usage 화면에서 확인.
MODEL = "claude-haiku-5-5"

# 안전장치: AI가 쓸 수 있는 최대 길이(토큰). 넘으면 발송하지 않고 멈춘다.
MAX_TOKENS = 3000

# 뉴스 출처 — 전부 무료 RSS. (이름, 주소)
#   구글 뉴스 검색 RSS: 검색어만 바꾸면 내가 보고 싶은 주제로 바뀐다. when:1d = 최근 하루.
def _google(q):
    return "https://news.google.com/rss/search?q=" + urllib.parse.quote(q + " when:1d") + "&hl=ko&gl=KR&ceid=KR:ko"

FEEDS = [
    ("연합뉴스 경제",      "https://www.yna.co.kr/rss/economy.xml"),
    ("한국경제 경제",      "https://www.hankyung.com/feed/economy"),
    ("구글뉴스·증시",      _google("코스피 증시")),
    ("구글뉴스·금리환율",  _google("금리 환율")),
    ("구글뉴스·부동산",    _google("부동산 대출")),
    ("구글뉴스·재테크",    _google("재테크")),
    ("구글뉴스·경제",      _google("경제")),
]

PER_FEED = 6          # 출처 하나에서 가져올 최대 기사 수 (7곳 × 6 = 최대 42 → 아래 40에서 멈춤)
MAX_ARTICLES = 40     # 전체 최대 기사 수 (많을수록 AI 값이 오른다)
HOURS = 24            # 최근 몇 시간 기사만
DESC_CHARS = 120      # 기사 설명 글자 수 상한

TELEGRAM_LIMIT = 4096   # 텔레그램 한 통 최대 글자 수(텔레그램 규칙)
SHOW_LINKS = True       # 요약 아래에 참고 기사 주소를 붙일지

# 회원에게 나가는 글에 들어가면 안 되는 안내 문구 — 이 낱말이 든 줄은 발송 전에 지운다.
BANNED = ["확인 필요", "추가 확인", "재료 없음", "AI가 작성", "투자 권유가 아닙니다", "참고용입니다"]

KST = dt.timezone(dt.timedelta(hours=9))
WEEK = "월화수목금토일"


# ─────────────────────────────────────────────────────────────
# 2. 비밀값 읽기 — 없으면 에러로 멈춘다(예비값 없음)
# ─────────────────────────────────────────────────────────────
def need(name):
    v = (os.environ.get(name) or "").strip()
    if not v:
        sys.exit("멈춤: 비밀값 {} 이(가) 없습니다. 깃허브 Settings → Secrets 에 넣어 주세요.".format(name))
    return v


# ─────────────────────────────────────────────────────────────
# 3. 뉴스 모으기 (돈 안 듦)
# ─────────────────────────────────────────────────────────────
def _clean(s):
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (news-bot)",
        "Accept": "application/rss+xml,application/xml,text/xml,*/*"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


def _parse(raw, feed_name, now):
    root = ET.fromstring(raw)
    out = []
    for it in root.iter("item"):
        title = _clean(it.findtext("title"))
        link = (it.findtext("link") or "").strip()
        src_el = it.find("source")
        src = _clean(src_el.text) if src_el is not None and src_el.text else ""
        # 구글 뉴스는 제목 끝에 " - 언론사"를 붙인다 → 떼어낸다
        while src and title.endswith(" - " + src):
            title = title[: -(len(src) + 3)].strip()
        desc = _clean(it.findtext("description"))
        if desc.startswith(title):          # 구글 뉴스 설명은 제목 반복이라 버린다
            desc = ""
        pub = None
        try:
            pub = email.utils.parsedate_to_datetime(it.findtext("pubDate") or "")
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=dt.timezone.utc)
        except Exception:
            pass
        if pub is not None and (now - pub).total_seconds() > HOURS * 3600:
            continue                         # 오래된 기사 버림
        if not title:
            continue
        out.append({"title": title, "link": link, "source": src or feed_name,
                    "desc": desc[:DESC_CHARS], "pub": pub})
    return out


def collect_articles(now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    seen, arts, report = set(), [], []
    for name, url in FEEDS:
        try:
            items = _parse(_fetch(url), name, now)
        except Exception as e:
            report.append((name, "실패: " + str(e)[:80]))
            continue
        n = 0
        for a in items:
            key = re.sub(r"\W", "", a["title"])[:40]
            if key in seen:
                continue                     # 같은 기사 중복 제거
            seen.add(key)
            arts.append(a)
            n += 1
            if n >= PER_FEED or len(arts) >= MAX_ARTICLES:
                break
        report.append((name, "{}건".format(n)))
        if len(arts) >= MAX_ARTICLES:
            break
    return arts, report


def articles_text(arts):
    lines = []
    for i, a in enumerate(arts, 1):
        when = a["pub"].astimezone(KST).strftime("%m/%d %H:%M") if a["pub"] else "시각 모름"
        line = "[{}] {} | {} | {}".format(i, a["source"], when, a["title"])
        if a["desc"]:
            line += " — " + a["desc"]
        lines.append(line)
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# 4. AI 요약 1회 (여기서만 돈이 나간다)
# ─────────────────────────────────────────────────────────────
def load_prompt():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "prompt.txt"), encoding="utf-8") as f:
        return f.read().strip()


def summarize(arts, api_key):
    import anthropic  # 여기서만 불러온다 — 뉴스 모으기·시험 검사는 이 줄을 지나지 않는다

    client = anthropic.Anthropic(api_key=api_key, max_retries=1)
    today = dt.datetime.now(KST)
    user = "오늘은 {}/{}({})입니다. 아래 기사 목록만 보고 아침 경제 뉴스 한 통을 써 주세요.\n\n[기사 목록]\n{}".format(
        today.month, today.day, WEEK[today.weekday()], articles_text(arts))
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        thinking={"type": "disabled"},          # 생각 단계 끔 → 토큰 절약
        output_config={"effort": "low"},
        system=load_prompt(),
        messages=[{"role": "user", "content": user}],
        # tools 없음 = 웹검색 안 씀(웹검색은 요금이 따로 붙는다)
    )
    u = resp.usage
    print("AI 사용량: 입력 {} 토큰, 출력 {} 토큰, 멈춘 이유 {}".format(u.input_tokens, u.output_tokens, resp.stop_reason))
    if resp.stop_reason != "end_turn":
        # max_tokens = 글이 잘림 / refusal = 거절 → 둘 다 발송하지 않는다
        sys.exit("멈춤: AI 글이 끝까지 안 써졌습니다(stop_reason={}). 발송하지 않습니다.".format(resp.stop_reason))
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        sys.exit("멈춤: AI 답이 비어 있습니다. 발송하지 않습니다.")
    print("끝 40자 확인:", text[-40:])
    return text


# ─────────────────────────────────────────────────────────────
# 5. 텔레그램 메시지 만들기·보내기
# ─────────────────────────────────────────────────────────────
def tg_len(s):
    """텔레그램이 세는 글자 수(UTF-16 기준). 이모지는 2로 센다."""
    return len(s.encode("utf-16-le")) // 2


def remove_banned(text):
    kept = [ln for ln in text.splitlines() if not any(b in ln for b in BANNED)]
    removed = len(text.splitlines()) - len(kept)
    if removed:
        print("안내 문구 줄 {}개 지움".format(removed))
    return "\n".join(kept).strip()


def remove_markdown(text):
    """텔레그램은 일반 글로 보내므로 **굵게**·__밑줄__·# 제목 기호가 그대로 보인다 → 기호만 지운다."""
    text = text.replace("**", "").replace("__", "")
    return re.sub(r"(?m)^\s*#{1,6}\s*", "", text)


def remove_title_line(text):
    """AI가 첫 줄에 제목(날짜나 '아침 경제 뉴스')을 또 쓰면 지운다 — 제목은 코드가 한 번만 붙인다."""
    lines = text.strip().splitlines()
    if lines and (re.search(r"\d{1,2}\s*/\s*\d{1,2}", lines[0]) or "아침 경제 뉴스" in lines[0]):
        print("AI가 쓴 제목 줄 지움:", lines[0][:40])
        lines = lines[1:]
    return "\n".join(lines).strip()


def join_bullets(text):
    """AI가 "•" 를 혼자 한 줄에 쓰고 내용을 다음 줄에 쓰면 → "• 내용" 한 줄로 합친다."""
    out, pending = [], None
    for ln in text.splitlines():
        if ln.strip() in ("•", "-", "·", "*"):
            pending = "•"
            continue
        if pending and ln.strip():
            ln = pending + " " + ln.strip()
            pending = None
        out.append(ln)
    return "\n".join(out).strip()


def short(s, n=30):
    return s if len(s) <= n else s[:n].rstrip() + "…"


def visible_len(html_text):
    """텔레그램이 HTML 을 해석한 뒤 화면에 보이는 글자 수(태그 빼고, &amp; 같은 표기는 1글자)."""
    return tg_len(html.unescape(re.sub(r"<[^>]+>", "", html_text)))


def _pack(lines, length, limit=TELEGRAM_LIMIT):
    """줄 목록을 한 통 limit 글자 안으로 묶는다. 줄 중간(태그 중간)은 자르지 않는다."""
    parts, cur = [], []
    for ln in lines:
        cand = cur + [ln]
        if cur and length("\n".join(cand)) > limit:
            parts.append("\n".join(cur))
            cur = [ln]
        else:
            cur = cand
    if cur:
        parts.append("\n".join(cur))
    return parts


def _split_long(line, limit=TELEGRAM_LIMIT // 2):
    """한 줄이 너무 긴 극단적인 경우만 글자 단위로 나눈다(본문 줄에만 쓰임, 태그 없음)."""
    return [line[i:i + limit] for i in range(0, len(line), limit)] or [""]


def build_messages(summary, arts, now_kst=None):
    """보낼 메시지 목록을 만든다. 각 메시지 = {"html": HTML 글, "plain": [HTML 이 안 될 때 보낼 일반 글들]}.
    - 제목은 코드가 한 번만 붙인다.
    - 맨 아래 '출처' = 본문에 쓰인 번호만, 제목을 누르면 기사가 열리는 링크(주소 글자는 안 보임).
    - 화면 글자 수 4096 안이면 한 통. 넘치면 줄 단위로 나눈다."""
    now_kst = now_kst or dt.datetime.now(KST)
    head = "{}/{}({}) 아침 경제 뉴스".format(now_kst.month, now_kst.day, WEEK[now_kst.weekday()])
    body = join_bullets(remove_title_line(remove_markdown(remove_banned(summary))))
    used = sorted({int(n) for n in re.findall(r"\[(\d{1,3})\]", body) if 1 <= int(n) <= len(arts)})

    # (html 줄, 일반 줄) 짝
    rows = [(html.escape(head, quote=False), head), ("", "")]
    for ln in body.splitlines():
        for piece in (_split_long(ln) if tg_len(ln) > TELEGRAM_LIMIT // 2 else [ln]):
            rows.append((html.escape(piece, quote=False), piece))
    if SHOW_LINKS and used:
        rows += [("", ""), ("출처", "출처")]
        for n in used:
            a = arts[n - 1]
            label = "[{}] {}".format(n, short(a["title"]))
            rows.append(('<a href="{}">{}</a>'.format(html.escape(a["link"], quote=True), html.escape(label, quote=False)),
                         "{} — {}".format(label, a["link"])))

    html_parts = _pack([h for h, _ in rows], visible_len)
    msgs, i = [], 0
    for hp in html_parts:                 # 같은 줄 묶음으로 일반 글도 만든다(HTML 실패 대비)
        n_lines = len(hp.split("\n"))
        plain_lines = [p for _, p in rows[i:i + n_lines]]
        i += n_lines
        if hp.strip():                    # 빈 줄만 남은 조각은 보내지 않는다
            msgs.append({"html": hp.strip("\n"),
                         "plain": [p.strip("\n") for p in _pack(plain_lines, tg_len) if p.strip()]})
    return msgs


def _post(token, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request("https://api.telegram.org/bot{}/sendMessage".format(token),
                                 data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")


def send_telegram(token, chat_id, msg):
    """HTML 로 보낸다. 텔레그램이 HTML 을 못 읽으면(400 can't parse entities) 같은 내용을 일반 글로 한 번 더."""
    base = {"chat_id": chat_id, "link_preview_options": {"is_disabled": True}}  # 링크 미리보기 끔
    code, res = _post(token, dict(base, text=msg["html"], parse_mode="HTML"))
    if code == 200 and isinstance(res, dict) and res.get("ok"):
        return
    if code == 400 and "parse entities" in str(res):
        print("HTML 해석 실패 → 일반 글로 다시 보냄:", str(res)[:150])
        for p in msg["plain"]:
            code2, res2 = _post(token, dict(base, text=p))
            if not (code2 == 200 and isinstance(res2, dict) and res2.get("ok")):
                sys.exit("멈춤: 텔레그램 발송 실패 {} — {}".format(code2, str(res2)[:200]))
        return
    sys.exit("멈춤: 텔레그램 발송 실패 {} — {}".format(code, str(res)[:200]))


# ─────────────────────────────────────────────────────────────
# 6. 실행 순서
# ─────────────────────────────────────────────────────────────
def main():
    # 비밀값부터 확인 — 하나라도 없으면 아무것도 하지 않고 멈춘다
    api_key = need("ANTHROPIC_API_KEY")
    token = need("BOT_TOKEN")
    chat_id = need("CHAT_ID")

    arts, report = collect_articles()
    for name, r in report:
        print("수집 {}: {}".format(name, r))
    print("모은 기사 {}건".format(len(arts)))
    if not arts:
        sys.exit("멈춤: 기사가 0건이라 AI를 부르지 않았습니다(돈 0원).")

    summary = summarize(arts, api_key)
    msgs = build_messages(summary, arts)
    for m in msgs:
        send_telegram(token, chat_id, m)
    print("발송 완료: {}통, 화면 글자 수 {}".format(len(msgs), [visible_len(m["html"]) for m in msgs]))


if __name__ == "__main__":
    main()
