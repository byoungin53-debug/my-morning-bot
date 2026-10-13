# -*- coding: utf-8 -*-
"""
유튜브 요약 한 통 — 내가 보는 채널의 새 영상 자막을 Claude에게 한 번 요약시키고, 텔레그램으로 보낸다.

순서
  1) 채널 새 영상 찾기 (돈 안 듦)            find_new_videos()   ← 유튜브 RSS
  2) 자막 가져오기 (돈 안 듦)                get_transcript()    ← youtube-transcript-api
  3) AI 요약 1회 (돈 나감)                   summarize()         ← 여기서만 돈이 나간다
  4) 텔레그램 보내기 (돈 안 듦)              send_telegram()
  5) 보낸 영상 번호 저장 (다음 날 중복 방지)  yt_sent.json

★ 꼭 알아 둘 것 — 어디서 돌리느냐에 따라 결과가 다르다
  - 노트북에서 돌리면: 자막을 가져와서 3줄 요약이 온다.
  - 깃허브(클라우드)에서 돌리면: 유튜브가 클라우드 컴퓨터의 자막 요청을 막는 경우가 많다.
    막힌 영상은 AI를 부르지 않고 "제목 + 영상 링크"만 보낸다(돈 0원). 지어낸 요약을 보내지 않는다.

비밀값 3개 — 코드에 적지 않는다.
  ANTHROPIC_API_KEY / BOT_TOKEN / CHAT_ID
  깃허브: Settings → Secrets 에 넣는다.
  노트북: 같은 폴더의 secrets.local.json 에 넣는다(.gitignore 로 깃허브에 안 올라감).
"""
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# 윈도우에서 기록 파일로 출력을 보낼 때 한글·이모지·줄표(—) 때문에 멈추지 않게 한다
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))

# ─────────────────────────────────────────────────────────────
# 1. 설정 — 바꿔도 되는 곳 (채널은 channels.txt 에서 바꾼다)
# ─────────────────────────────────────────────────────────────
# 모델. 실제 금액은 첫 실행 뒤 Console의 분석 → 비용 화면에서 확인한다.
MODEL = "claude-haiku-5-5"
MAX_TOKENS = 4500          # AI가 쓸 수 있는 최대 길이(토큰). 넘으면 발송하지 않고 멈춘다.

HOURS = 30                 # 최근 몇 시간 영상만 (하루에 깃허브가 늦게 도는 여유를 더함. 이미 보낸 영상은 다시 안 보냄)
PER_CHANNEL = 3            # 채널 하나에서 최대 몇 편
MAX_VIDEOS = 10            # 한 번에 최대 몇 편 (많을수록 AI 값이 오른다)
MAX_CHANNELS = 20          # channels.txt 에서 읽는 최대 채널 수
SKIP_SHORTS = True         # 쇼츠(짧은 영상)는 건너뜀
TRANSCRIPT_CHARS = 12000   # 영상 하나 자막 최대 글자 수. 넘으면 앞 9천 자 + 뒤 3천 자만
TOTAL_CHARS = 40000        # AI에게 보내는 자막 전체 최대 글자 수 = 1통 값의 상한
MIN_TRANSCRIPT = 200       # 이보다 짧은 자막은 요약 재료로 안 씀
LANGS = ["ko", "en"]       # 자막 언어 우선순위

TELEGRAM_LIMIT = 4096      # 텔레그램 한 통 최대 글자 수
STATE_FILE = os.path.join(HERE, "yt_sent.json")
STATE_KEEP = 500           # 보낸 영상 번호를 최근 몇 개까지 기억할지

# 받는 글에 들어가면 안 되는 안내 문구 — 이 낱말이 든 줄은 발송 전에 지운다.
BANNED = ["확인 필요", "추가 확인", "재료 없음", "AI가 작성", "투자 권유가 아닙니다", "참고용입니다",
          "이번 영상에서는", "영상에 따르면", "구독", "좋아요"]

KST = dt.timezone(dt.timedelta(hours=9))
WEEK = "월화수목금토일"
UA = {"User-Agent": "Mozilla/5.0 (yt-summary-bot)", "Accept-Language": "ko"}


# ─────────────────────────────────────────────────────────────
# 2. 비밀값 — 환경변수 → (노트북이면) secrets.local.json. 둘 다 없으면 멈춘다(예비값 없음)
# ─────────────────────────────────────────────────────────────
def need(name):
    v = (os.environ.get(name) or "").strip()
    if not v:
        f = os.path.join(HERE, "secrets.local.json")
        if os.path.exists(f):
            with open(f, encoding="utf-8-sig") as fh:
                v = str(json.load(fh).get(name) or "").strip()
    if not v:
        sys.exit("멈춤: 비밀값 {} 이(가) 없습니다. 깃허브는 Settings → Secrets, 노트북은 secrets.local.json 에 넣어 주세요.".format(name))
    return v


# ─────────────────────────────────────────────────────────────
# 3. 채널 목록·새 영상 찾기 (돈 안 듦)
# ─────────────────────────────────────────────────────────────
def _get(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def read_channels(path=None):
    path = path or os.path.join(HERE, "channels.txt")
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for ln in f:
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                out.append(ln)
    return out[:MAX_CHANNELS]


def channel_id(line):
    """channels.txt 한 줄 → 채널 ID(UC…24글자). 주소면 채널 페이지에서 찾아낸다."""
    m = re.search(r"(UC[\w-]{22})", line)
    if m:
        return m.group(1)
    url = line if line.startswith("http") else "https://www.youtube.com/" + (line if line.startswith("@") else "@" + line)
    url = urllib.parse.quote(url, safe=":/?=&@%")      # 한글 채널 주소(@한글이름)도 되게
    try:
        page = _get(url).decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        raise ValueError("그런 채널 주소가 없습니다(HTTP {}). 채널 페이지 주소창을 그대로 복사했는지 보세요".format(e.code))
    m = (re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', page)
         or re.search(r'"externalId":"(UC[\w-]{22})"', page))
    if not m:
        raise ValueError("채널 ID를 못 찾음")
    return m.group(1)


NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "m": "http://search.yahoo.com/mrss/"}


def channel_videos(cid):
    root = ET.fromstring(_get("https://www.youtube.com/feeds/videos.xml?channel_id=" + cid))
    name = root.findtext("a:title", default="", namespaces=NS).strip()
    vids = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", default="", namespaces=NS)
        link_el = e.find("a:link", NS)
        link = link_el.get("href") if link_el is not None else ""
        try:
            pub = dt.datetime.fromisoformat(e.findtext("a:published", default="", namespaces=NS))
        except ValueError:
            continue
        if vid:
            vids.append({"id": vid, "title": e.findtext("a:title", default="", namespaces=NS).strip(),
                         "channel": name, "published": pub, "shorts": "/shorts/" in link,
                         "url": "https://youtu.be/" + vid})
    return name, vids


def find_new_videos(channels, sent, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    picked, report = [], []
    for line in channels:
        try:
            cid = channel_id(line)
            name, vids = channel_videos(cid)
        except Exception as e:
            report.append((line, "실패: " + str(e)[:80]))
            continue
        n = 0
        for v in sorted(vids, key=lambda x: x["published"], reverse=True):
            if (now - v["published"]).total_seconds() > HOURS * 3600:
                break
            if v["id"] in sent or (SKIP_SHORTS and v["shorts"]):
                continue
            picked.append(v)
            n += 1
            if n >= PER_CHANNEL:
                break
        report.append((name or line, "새 영상 {}편".format(n)))
    picked.sort(key=lambda x: x["published"])            # 오래된 것부터
    return picked[-MAX_VIDEOS:], report


# ─────────────────────────────────────────────────────────────
# 4. 자막 가져오기 (돈 안 듦) — 클라우드에서는 막힐 수 있다
# ─────────────────────────────────────────────────────────────
def get_transcript(video_id):
    """성공하면 (자막 글, None), 실패하면 (None, 이유)."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return None, "youtube-transcript-api 설치 안 됨"
    try:
        parts = YouTubeTranscriptApi().fetch(video_id, languages=LANGS)
        text = re.sub(r"\s+", " ", " ".join(p.text for p in parts)).strip()
    except Exception as e:                       # RequestBlocked / IpBlocked = 유튜브가 막음, NoTranscriptFound = 자막 없음
        return None, type(e).__name__
    if len(text) < MIN_TRANSCRIPT:
        return None, "자막이 너무 짧음({}자)".format(len(text))
    if len(text) > TRANSCRIPT_CHARS:
        text = text[:9000] + " …(중략)… " + text[-3000:]
    return text, None


# 유튜브가 "요청이 너무 많다"며 이 컴퓨터를 막았다는 뜻의 오류 이름들
BLOCKED = ("IpBlocked", "RequestBlocked", "TooManyRequests", "YouTubeRequestFailed")
GAP_SECONDS = 3            # 자막 요청 사이에 쉬는 시간(초). 짧게 연달아 요청하면 유튜브가 막는다


def fetch_transcripts(videos, label="{}자"):
    """영상마다 자막을 가져온다. 한 번 막히면 남은 영상은 요청하지 않는다(더 두드리면 더 오래 막힌다)."""
    import time
    ok, no_sub, blocked = [], [], False
    for i, v in enumerate(videos):
        if blocked:
            why, text = "앞 영상에서 막혀 요청 안 함", None
        else:
            if i:
                time.sleep(GAP_SECONDS)
            text, why = get_transcript(v["id"])
            blocked = why in BLOCKED
        print("자막 {} | {} | {}".format(v["id"], v["title"][:30], label.format(len(text)) if text else "실패: " + why))
        if text:
            v["transcript"] = text
            ok.append(v)
        else:
            no_sub.append(v)
    if blocked:
        print("→ 유튜브가 이 컴퓨터의 자막 요청을 막았습니다. 몇 시간 뒤에 다시 하세요. 여러 번 다시 누르면 더 오래 막힙니다.")
    return ok, no_sub


def fit_budget(items):
    """자막 전체를 TOTAL_CHARS 안으로 줄인다(영상마다 같은 몫) — 1통 값의 상한."""
    if not items:
        return items
    share = TOTAL_CHARS // len(items)
    for it in items:
        if len(it["transcript"]) > share:
            head = int(share * 0.75)
            it["transcript"] = it["transcript"][:head] + " …(중략)… " + it["transcript"][-(share - head):]
    return items


# ─────────────────────────────────────────────────────────────
# 5. AI 요약 1회 (여기서만 돈이 나간다)
# ─────────────────────────────────────────────────────────────
def load_prompt():
    with open(os.path.join(HERE, "yt_prompt.txt"), encoding="utf-8") as f:
        return f.read().strip()


def videos_text(items):
    return "\n\n".join("[{}] 채널: {} | 제목: {}\n자막: {}".format(i, it["channel"], it["title"], it["transcript"])
                       for i, it in enumerate(items, 1))


def summarize(items, api_key):
    import anthropic  # 여기서만 불러온다 — 영상 찾기·자막·점검은 이 줄을 지나지 않는다

    client = anthropic.Anthropic(api_key=api_key, max_retries=1)
    user = "영상 {}편입니다. 영상마다 [번호] 아래 요약 3줄을 써 주세요.\n\n[영상 목록]\n{}".format(len(items), videos_text(items))
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        thinking={"type": "disabled"},          # 생각 단계 끔 → 토큰 절약
        output_config={"effort": "low"},
        system=load_prompt(),
        messages=[{"role": "user", "content": user}],
        # tools 없음 = 웹검색 안 씀
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


def parse_summary(text, n):
    """AI 글 → {번호: [3줄]}. 영상마다 정확히 3줄이 아니면 발송하지 않는다(코드로 센다)."""
    blocks, cur = {}, None
    for ln in text.splitlines():
        ln = ln.strip()
        m = re.fullmatch(r"\[(\d{1,2})\]", ln)
        if m:
            cur = int(m.group(1))
            blocks[cur] = []
        elif ln and cur is not None:
            blocks[cur].append(re.sub(r"^([-•*·]|\d+[.)])\s*", "", ln))
    bad = [i for i in range(1, n + 1) if len(blocks.get(i, [])) != 3]
    if bad:
        sys.exit("멈춤: 요약 3줄이 맞지 않는 영상 번호 {} (줄 수 {}). 발송하지 않습니다.".format(
            bad, [len(blocks.get(i, [])) for i in bad]))
    return blocks


# ─────────────────────────────────────────────────────────────
# 6. 텔레그램 메시지 만들기·보내기
# ─────────────────────────────────────────────────────────────
def tg_len(s):
    """텔레그램이 세는 글자 수(UTF-16 기준). 이모지는 2로 센다."""
    return len(s.encode("utf-16-le")) // 2


def clean_lines(lines):
    kept = [ln for ln in lines if not any(b in ln for b in BANNED)]
    if len(kept) != len(lines):
        print("안내 문구 줄 {}개 지움".format(len(lines) - len(kept)))
    return kept


def build_messages(summarized, blocks, no_sub, now_kst=None):
    """영상마다  제목: [채널] 영상 제목 / 요약 3줄 / 영상링크 : 주소.
    자막을 못 가져온 영상은 제목·링크만. 4096자를 넘으면 영상 단위로 나눈다(문장 중간을 자르지 않는다)."""
    now_kst = now_kst or dt.datetime.now(KST)
    total = len(summarized) + len(no_sub)
    head = "{}/{}({}) 유튜브 요약 {}편".format(now_kst.month, now_kst.day, WEEK[now_kst.weekday()], total)
    units = []
    for i, it in enumerate(summarized, 1):
        lines = clean_lines(blocks[i])
        units.append("제목: [{}] {}\n{}\n영상링크 : {}".format(it["channel"], it["title"], "\n".join(lines), it["url"]))
    for it in no_sub:
        units.append("제목: [{}] {}\n영상링크 : {}".format(it["channel"], it["title"], it["url"]))
    msgs, cur = [], head
    for u in units:
        while tg_len(u) > TELEGRAM_LIMIT - 10:            # 한 덩어리가 너무 긴 극단적인 경우만 강제로 자름
            msgs.append(cur)
            cur, u = u[:TELEGRAM_LIMIT // 2], u[TELEGRAM_LIMIT // 2:]
        cand = cur + "\n\n" + u
        if tg_len(cand) > TELEGRAM_LIMIT:
            msgs.append(cur)
            cur = u
        else:
            cur = cand
    msgs.append(cur)
    return msgs


def send_telegram(token, chat_id, text):
    data = json.dumps({"chat_id": chat_id, "text": text,
                       "link_preview_options": {"is_disabled": True}}).encode("utf-8")
    req = urllib.request.Request("https://api.telegram.org/bot{}/sendMessage".format(token),
                                 data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.exit("멈춤: 텔레그램 발송 실패 {} — {}".format(e.code, e.read().decode("utf-8", "ignore")[:200]))
    if not res.get("ok"):
        sys.exit("멈춤: 텔레그램 발송 실패 — {}".format(str(res)[:200]))


# ─────────────────────────────────────────────────────────────
# 7. 보낸 영상 기억하기 (중복 방지)
# ─────────────────────────────────────────────────────────────
def load_sent():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return list(json.load(f).get("sent", []))
    except (FileNotFoundError, ValueError):
        return []


def save_sent(old, new_ids):
    ids = (old + [i for i in new_ids if i not in old])[-STATE_KEEP:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"sent": ids}, f, ensure_ascii=False, indent=1)
        f.write("\n")


# ─────────────────────────────────────────────────────────────
# 8. 실행 순서
# ─────────────────────────────────────────────────────────────
def main():
    # 비밀값부터 확인 — 하나라도 없으면 아무것도 하지 않고 멈춘다
    api_key = need("ANTHROPIC_API_KEY")
    token = need("BOT_TOKEN")
    chat_id = need("CHAT_ID")

    sent = load_sent()
    videos, report = find_new_videos(read_channels(), set(sent))
    for name, r in report:
        print("채널 {}: {}".format(name, r))
    if not videos:
        print("새 영상 0편 — AI를 부르지 않고, 보내지도 않습니다(돈 0원).")
        return

    ok, no_sub = fetch_transcripts(videos)
    if no_sub:
        print("자막 못 가져온 영상 {}편 → AI 없이 제목·링크만 보냅니다.".format(len(no_sub)))

    blocks = {}
    if ok:
        fit_budget(ok)
        print("AI에게 보낼 자막 합계 {}자 (상한 {}자)".format(sum(len(v["transcript"]) for v in ok), TOTAL_CHARS))
        blocks = parse_summary(summarize(ok, api_key), len(ok))
    else:
        print("요약할 자막이 0편 — AI를 부르지 않았습니다(돈 0원).")

    msgs = build_messages(ok, blocks, no_sub)
    for m in msgs:
        send_telegram(token, chat_id, m)
    print("발송 완료: {}통, 글자 수 {}".format(len(msgs), [tg_len(m) for m in msgs]))
    save_sent(sent, [v["id"] for v in videos])          # 보낸 뒤에만 저장 → 실패하면 다음 날 다시 시도


def find_id_cli(lines):
    """python yt_bot.py --find-id 채널주소 … → 채널 ID와 이름만 출력(돈 0원)."""
    for line in lines:
        try:
            cid = channel_id(line)
            name, _ = channel_videos(cid)
            print("{}  →  {}  ({})".format(line, cid, name))
        except Exception as e:
            print("{}  →  실패: {}".format(line, str(e)[:80]))


def check_cli():
    """python yt_bot.py --check → 최근 3일 영상을 찾아 자막이 되는지만 본다.
    AI 안 부름·텔레그램 안 보냄·비밀값 필요 없음·yt_sent.json 안 바꿈(돈 0원).
    깃허브에서 돌리면 '이 클라우드 컴퓨터에서 자막이 막히는지'를 돈 없이 확인할 수 있다."""
    chs = read_channels()
    if not chs:
        sys.exit("멈춤: channels.txt 에 채널이 한 줄도 없습니다(# 로 시작하는 줄은 무시됩니다).")
    global HOURS
    keep, HOURS = HOURS, 72
    try:
        videos, report = find_new_videos(chs, set())
    finally:
        HOURS = keep
    for name, r in report:
        print("채널 {}: {}".format(name, r.replace("새 영상", "최근 3일 영상")))
    ok, _ = fetch_transcripts(videos, label="성공 {}자")
    got = len(ok)
    print("결과: 영상 {}편 중 자막 성공 {}편".format(len(videos), got))
    if videos and not got:
        print("→ 이 컴퓨터에서는 자막이 막혀 있습니다. 이대로 켜 두면 요약 없이 제목·링크만 옵니다.")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--find-id":
        find_id_cli(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == "--check":
        check_cli()
    else:
        main()
