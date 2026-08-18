"""
macro_alert.py — 매크로 조기경보 (단일 파일판)

두 축을 매일 아침 텔레그램으로 전송한다.

  ① 신용·자금 스트레스 (0~100점)
     하이일드 스프레드 / 장단기 금리차 / SOFR-IORB / MOVE
     → "이미 문제가 터지고 있는가"를 보는 축. 상대적으로 후행.

  ② 금리 레짐 (Rate Gravity)
     미 10년물 / 인플레 / IG 회사채 OAS / 금리하락 조건
     → "금리 레짐이 구조적으로 바뀌는가"를 보는 축. 상대적으로 선행.

두 축은 합산하지 않고 나란히 표시한다. 하이일드(HY)와 IG 스프레드는 상관이
매우 높아 한 점수에 넣으면 신용위험이 이중 계상되기 때문.

⚠️ 금리 레짐의 임계값(5.00 / 5.30 등)은 특정 애널리스트가 한 시점에 제시한
   견해이고 근거 사례는 2000년·2007년 두 번뿐이다. 통계적 검증이 아니라
   서술에 가까우므로 자동 매매 트리거가 아닌 참고 신호로 다룰 것.
"""

import os
from datetime import datetime, date, timedelta
from enum import IntEnum

import requests
import yfinance as yf

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_TO = os.environ.get('TELEGRAM_TO')
FRED_API_KEY = os.environ.get('FRED_API_KEY')


# ═══════════════════════════════════════════════════════════
# 설정 — 손볼 일이 있으면 이 블록만 고치면 된다
# ═══════════════════════════════════════════════════════════

class Level(IntEnum):
    OK = 0
    WATCH = 1
    WARNING = 2
    CRITICAL = 3


LEVEL_EMOJI = {Level.OK: "🟢", Level.WATCH: "🟡",
               Level.WARNING: "🟠", Level.CRITICAL: "🔴"}
LEVEL_KR = {Level.OK: "정상", Level.WATCH: "관찰",
            Level.WARNING: "경고", Level.CRITICAL: "위험"}

# FRED 시리즈 ID
SERIES = {
    "hy_spread":   "BAMLH0A0HYM2",          # 하이일드 OAS (일간, %)
    "yield_curve": "T10Y2Y",                # 10Y-2Y (일간, %)
    "sofr":        "SOFR",                  # 담보부 익일물 금리 (일간, %)
    "iorb":        "IORB",                  # 지준부리 (일간, %)
    "ust10y":      "DGS10",                 # 미 10년물 (일간, %)
    "ig_oas":      "BAMLC0A0CM",            # IG 회사채 OAS (일간, %)
    "sticky_cpi":  "CORESTICKM159SFRBATL",  # Sticky CPI 전년비 (월간, 이미 %)
    # ★아래는 '지수 레벨' 시리즈라 전년비를 직접 계산해야 한다 (yoy_from_index)
    # ★이 시리즈가 "주거비 제외 근원물가"와 정확히 같은 개념인지는 미검증.
    #   다르면 CPILFESL(근원)과 주거비 시리즈로 직접 산출할 것.
    "cpi_ex_shelter_index": "CUSR0000SA0L12E",
}

# 금리 레짐 임계값
UST10Y_WATCH = 4.90      # 원안 4.80은 당시 시세 바로 위라 상시 점등 위험 → 상향
UST10Y_WARNING = 5.00    # 2007년 이후 미경험 영역
UST10Y_CRITICAL = 5.30   # 2000년 이후 미경험 영역
UST10Y_FAST_RISE = 0.50  # 3개월 상승폭 %p (레벨과 별개 보조 조건)

CORE_EX_SHELTER_THRESHOLD = 3.0
STICKY_CPI_THRESHOLD = 3.5
IG_OAS_WIDEN_BP = 25.0   # 3개월 저점 대비 확대폭

# 바클레이스: 장기금리가 내려오려면 아래 4개가 충족돼야 한다.
# 자동 판정이 어려운 항목이라 직접 True/False 로 관리한다.
RELIEF_FLAGS = {
    "경기지표 지속 약화": False,
    "미국 재정적자 억제": False,
    "AI 관련 회사채 발행 둔화": False,
    "재무부 장기채 공급 조절": False,
}

# 단기 금리 변동성 유발 이벤트 (지난 날짜는 자동 제외)
EVENT_WATCHLIST = [
    (date(2026, 8, 19), "미 재무부 20년물 국채 입찰"),
    (date(2026, 8, 27), "잭슨홀 심포지엄 개막"),
]


# ═══════════════════════════════════════════════════════════
# 데이터 수집
# ═══════════════════════════════════════════════════════════

def get_fred_obs(key, limit=90):
    """FRED 관측치를 (날짜문자열, 값) 리스트로. 최신순(desc)."""
    sid = SERIES[key]
    url = (
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={sid}&api_key={FRED_API_KEY}&file_type=json"
        f"&sort_order=desc&limit={limit}"
    )
    try:
        obs = requests.get(url, timeout=20).json()['observations']
        return [(o['date'], float(o['value'])) for o in obs if o['value'] != '.']
    except Exception as e:
        print(f"FRED Error ({sid}): {e}")
        return []


def get_fred(key, limit=90):
    """값만 최신순으로."""
    return [v for _, v in get_fred_obs(key, limit)]


def get_yf(ticker, period="3mo"):
    """야후 종가. 최신순."""
    try:
        hist = yf.Ticker(ticker).history(period=period)['Close'].dropna()
        return list(hist)[::-1]
    except Exception as e:
        print(f"Yahoo Error ({ticker}): {e}")
        return []


def val(series, idx=0):
    """idx=0 최신, 5≈1주 전, 20≈1달 전, 63≈3개월 전."""
    return series[idx] if series and len(series) > idx else None


def delta_text(now, past, unit="%p", digits=2):
    if now is None or past is None:
        return "n/a"
    d = now - past
    arrow = "▲" if d > 0 else ("▼" if d < 0 else "－")
    return f"{arrow}{abs(d):.{digits}f}{unit}"


def yoy_from_index(monthly_index_desc):
    """월간 '지수 레벨'에서 전년비(%)를 계산.

    지수값(예: 330.5)을 그대로 3.0% 임계값과 비교하면 항상 위험으로 뜨는
    단위 버그가 난다. 지수 시리즈는 반드시 이 함수를 거칠 것.
    """
    if len(monthly_index_desc) < 13:
        return None
    now, year_ago = monthly_index_desc[0], monthly_index_desc[12]
    return (now / year_ago - 1) * 100 if year_ago else None


def trend_break(series_desc, threshold, lookback=20, min_ratio=0.6):
    """추세적 돌파 판정 (최신순 리스트 입력).

    최근 lookback개 중 min_ratio 이상이 임계값을 넘었고 최신값도 위에 있어야 True.
    series[0] > threshold 만 보면 국채 입찰 직후 스파이크에 오탐이 난다.
    """
    if len(series_desc) < lookback:
        return False
    window = series_desc[:lookback]
    if window[0] <= threshold:
        return False
    return sum(x > threshold for x in window) / lookback >= min_ratio


# ═══════════════════════════════════════════════════════════
# ① 신용·자금 스트레스 — 각 함수는 (신호등, 라벨, 해설, 점수 0~25)
# ═══════════════════════════════════════════════════════════

def judge_hy(now, m1_ago):
    """하이일드 스프레드 = 정크본드 금리 - 국채 금리. '기업 부도 걱정' 온도계."""
    if now is None:
        return "⚪", "데이터없음", "-", 0
    if now < 3.0:
        light, label, score = "🟢", "안전(과열/낙관)", 0
        c = "신용시장 매우 안정. 다만 너무 낮으면 시장이 위험을 잊은 상태."
    elif now < 4.0:
        light, label, score = "🟡", "보통", 6
        c = "평균 수준. 아직 주식에 부정적 신호는 아님."
    elif now < 5.0:
        light, label, score = "🟠", "경계", 15
        c = "기업 신용 우려 확대. 주식 비중 점검 구간."
    else:
        light, label, score = "🔴", "위험", 25
        c = "신용경색 신호. 과거 조정·침체 국면의 레벨."

    if m1_ago is not None and now - m1_ago >= 0.5:
        score = min(25, score + 8)
        c += " ★한 달 새 0.5%p 이상 급확대 → 절대수준보다 '속도'가 중요."
    return light, label, c, score


def judge_curve(now, m1_ago, series):
    """장단기 금리차. 역전은 침체 예고, 역전 뒤 가파른 정상화가 실제 위험 신호."""
    if now is None:
        return "⚪", "데이터없음", "-", 0
    was_inverted = any(v < 0 for v in series[:250]) if series else False

    if now < 0:
        light, label, score = "🔴", "역전(침체 예고)", 20
        c = "단기금리가 장기보다 높음. 통상 12~18개월 뒤 침체와 연결."
    elif now < 0.5:
        light, label, score = "🟡", "정상화 초기", 8
        c = "역전에서 막 벗어난 구간. 과거엔 이 시점부터 변동성이 커졌음."
    elif now < 1.5:
        light, label, score = "🟢", "정상", 3
        c = "무난한 우상향 곡선. 특별한 경고 없음."
    else:
        light, label, score = "🟡", "가파른 정상화", 10
        c = "금리차 확대. 금리인하 기대 또는 경기둔화 반영 가능."

    if was_inverted and m1_ago is not None and now - m1_ago >= 0.25:
        score = min(25, score + 10)
        c += " ★역전 해소 후 한 달 새 0.25%p 이상 가팔라짐 → 최우선 주의 패턴."
    return light, label, c, score


def judge_funding(sofr, iorb):
    """SOFR 자체보다 지준부리(IORB)와의 격차가 자금시장 스트레스 척도."""
    if sofr is None or iorb is None:
        return "⚪", "데이터없음", "-", 0, None
    spread = (sofr - iorb) * 100  # bp

    if spread < 5:
        light, label, score = "🟢", "원활", 0
        c = "단기자금 시장 정상. 유동성 문제 없음."
    elif spread < 15:
        light, label, score = "🟡", "약간 빡빡", 7
        c = "월말·분기말엔 흔하나 지속되면 관찰 필요."
    elif spread < 25:
        light, label, score = "🟠", "경색 조짐", 16
        c = "레포시장 압박. 연준 유동성 조치가 논의될 수 있는 레벨."
    else:
        light, label, score = "🔴", "자금경색", 25
        c = "2019년 레포 발작급. 위험자산에 즉각적 악재."
    return light, label, c, score, spread


def judge_move(now, m1_ago):
    """MOVE = 채권판 공포지수. 채권이 먼저 흔들리고 주식이 뒤따르는 경우가 많음."""
    if now is None:
        return "⚪", "데이터없음", "-", 0
    if now < 80:
        light, label, score = "🟢", "안정", 0
        c = "채권시장 조용함. 주식에 우호적."
    elif now < 100:
        light, label, score = "🟡", "보통", 5
        c = "평범한 수준. 금리 이벤트만 체크."
    elif now < 120:
        light, label, score = "🟠", "불안", 14
        c = "금리 변동성 확대. 성장주·장기채 흔들릴 수 있음."
    else:
        light, label, score = "🔴", "패닉", 25
        c = "채권시장 패닉. 주식 급락 동반 구간."

    if m1_ago is not None and now - m1_ago >= 20:
        score = min(25, score + 5)
        c += " ★한 달 새 20p 이상 급등 → 추세 악화."
    return light, label, c, score


def total_judgement(score):
    if score <= 15:
        return "🟢 평온", "위험자산 유지. 정기매수 계획대로 진행."
    if score <= 35:
        return "🟡 관찰", "특이 신호 없음. 신규 레버리지는 자제."
    if score <= 55:
        return "🟠 경계", "현금 비중 확대 검토. QQQ 등 고베타 추가매수는 신중히."
    if score <= 75:
        return "🔴 위험", "방어 전환 구간. 주식 비중 축소·헤지 점검."
    return "🚨 비상", "복수 지표 동시 악화. 자본 보존 우선."


# ═══════════════════════════════════════════════════════════
# ② 금리 레짐 — 각 함수는 (Level, 해설)
# ═══════════════════════════════════════════════════════════

def judge_ust10y(hist_desc):
    """금리 상승 자체가 아니라 '경험하지 못한 영역으로의 추세적 진입'이 관건."""
    latest = val(hist_desc)
    rise_3m = None
    if len(hist_desc) >= 64:
        rise_3m = hist_desc[0] - hist_desc[63]

    if trend_break(hist_desc, UST10Y_CRITICAL):
        lvl = Level.CRITICAL
        c = "2000년 이후 미경험 영역을 추세적으로 돌파. 자산배분 전제가 바뀜."
    elif trend_break(hist_desc, UST10Y_WARNING):
        lvl = Level.WARNING
        c = "2007년 이후 미경험 영역 추세 진입. 자본공급자 이탈 위험."
    elif latest is not None and latest > UST10Y_WATCH:
        lvl = Level.WATCH
        c = "임계점 접근. 다만 아직 시장이 겪어본 영역."
    elif rise_3m is not None and rise_3m >= UST10Y_FAST_RISE:
        lvl = Level.WATCH
        c = "레벨은 낮으나 3개월 상승 속도가 빠름. 절대값보다 기울기를 볼 것."
    else:
        lvl = Level.OK
        c = "금리 상승만으로는 매도 근거가 아님. 대개는 저가매수 기회 쪽."

    if rise_3m is not None:
        arrow = "▲" if rise_3m > 0 else ("▼" if rise_3m < 0 else "－")
        c += f" (3개월 {arrow}{abs(rise_3m):.2f}%p)"
    return lvl, latest, c


def judge_inflation(ex_shelter_yoy, sticky_yoy, obs_month, fresh):
    """월간 지표라 값이 한 달 내내 같다. 기준월을 붙이고 갱신분만 ★로 구분."""
    hits = []
    if ex_shelter_yoy is not None and ex_shelter_yoy >= CORE_EX_SHELTER_THRESHOLD:
        hits.append(f"주거비제외 근원 {ex_shelter_yoy:.1f}%")
    if sticky_yoy is not None and sticky_yoy >= STICKY_CPI_THRESHOLD:
        hits.append(f"sticky {sticky_yoy:.1f}%")

    if len(hits) == 2:
        lvl, c = Level.CRITICAL, "양 지표 동시 돌파 — Fed 인상 위험 구간."
    elif len(hits) == 1:
        lvl, c = Level.WARNING, f"단일 지표 돌파 ({hits[0]})."
    else:
        lvl, c = Level.OK, "일회성 요인 범위. 구조적 인플레 근거는 아직 부족."

    detail = []
    if sticky_yoy is not None:
        detail.append(f"sticky {sticky_yoy:.1f}%")
    if ex_shelter_yoy is not None:
        detail.append(f"주거비제외 근원 {ex_shelter_yoy:.1f}%")
    if detail:
        c += " [" + " / ".join(detail) + "]"
    if obs_month:
        c += f" ({obs_month} 기준{' ★신규' if fresh else ''})"
    return lvl, c


def judge_capital_supply(ig_desc, lookback=63):
    """자본 공급자가 실제로 돌아서는지는 회사채 조달 여건에서 먼저 드러난다.
    ※ 하이일드와 상관이 높아 종합점수에는 넣지 않고 표시만 한다."""
    if len(ig_desc) < lookback:
        return "⚠️ 데이터 부족으로 판정 불가 (정상이라는 뜻이 아님)"
    window = ig_desc[:lookback]
    widening_bp = (window[0] - min(window)) * 100

    if widening_bp >= IG_OAS_WIDEN_BP * 2:
        tail = "자본공급자 이탈 진행 중"
    elif widening_bp >= IG_OAS_WIDEN_BP:
        tail = "조달 여건 악화 조짐"
    else:
        tail = "조달 여건 양호"
    return f"3개월 저점 대비 {widening_bp:+.0f}bp — {tail}"


def judge_relief():
    met = sum(bool(v) for v in RELIEF_FLAGS.values())
    verdict = ("금리 하락 경로 열림" if met >= 3
               else "부분 개선" if met == 2
               else "장기금리 하방 경직")
    return f"{met}/4 충족 — {verdict}"


def gravity_action(level):
    """단계적 실행이 핵심 — 한 번에 옮기면 되돌림에 당한다."""
    return {
        Level.OK: "유지. 정기 리밸런싱 외 조정 없음.",
        Level.WATCH: "신규 납입분만 단기채 비중 상향. 기존 보유분 매도 없음.",
        Level.WARNING: "주식 → 초단기채 10~15%p 이동 (목표 이동분의 절반).",
        Level.CRITICAL: "잔여 10~15%p 추가 이동. AI 인프라/리츠 우선 축소.",
    }[level]


def upcoming_events(today, horizon_days=21):
    end = today + timedelta(days=horizon_days)
    return sorted((d, t) for d, t in EVENT_WATCHLIST if today <= d <= end)


# ═══════════════════════════════════════════════════════════
# 메인
# ═══════════════════════════════════════════════════════════

def main():
    today = date.today()

    # ---- 데이터 ----
    hy_s = get_fred("hy_spread")
    yc_s = get_fred("yield_curve", limit=400)
    sofr_s = get_fred("sofr")
    iorb_s = get_fred("iorb")
    ust10_s = get_fred("ust10y", limit=140)
    ig_s = get_fred("ig_oas", limit=90)
    move_s = get_yf("^MOVE")
    vix_s = get_yf("^VIX")

    sticky_obs = get_fred_obs("sticky_cpi", limit=24)
    ex_shelter_obs = get_fred_obs("cpi_ex_shelter_index", limit=24)

    hy, hy_w, hy_m = val(hy_s), val(hy_s, 5), val(hy_s, 20)
    yc, yc_w, yc_m = val(yc_s), val(yc_s, 5), val(yc_s, 20)
    sofr, iorb = val(sofr_s), val(iorb_s)
    move, move_m = val(move_s), val(move_s, 20)
    vix = val(vix_s)

    sticky_yoy = sticky_obs[0][1] if sticky_obs else None
    ex_shelter_yoy = yoy_from_index([v for _, v in ex_shelter_obs])
    cpi_month = sticky_obs[0][0][:7] if sticky_obs else None

    cpi_fresh = False
    if sticky_obs:
        try:
            obs_date = datetime.strptime(sticky_obs[0][0], "%Y-%m-%d").date()
            cpi_fresh = (today - obs_date) <= timedelta(days=45)
        except ValueError:
            pass

    # ---- 판정 ----
    hy_l, hy_lab, hy_c, hy_sc = judge_hy(hy, hy_m)
    yc_l, yc_lab, yc_c, yc_sc = judge_curve(yc, yc_m, yc_s)
    fd_l, fd_lab, fd_c, fd_sc, fd_spread = judge_funding(sofr, iorb)
    mv_l, mv_lab, mv_c, mv_sc = judge_move(move, move_m)

    total = hy_sc + yc_sc + fd_sc + mv_sc
    t_label, t_action = total_judgement(total)

    ust_lvl, ust_val, ust_c = judge_ust10y(ust10_s)
    inf_lvl, inf_c = judge_inflation(ex_shelter_yoy, sticky_yoy, cpi_month, cpi_fresh)
    ig_c = judge_capital_supply(ig_s)
    relief_c = judge_relief()
    g_level = max(ust_lvl, inf_lvl)   # IG·relief는 참고 항목이라 레벨 산정 제외
    events = upcoming_events(today)

    # ---- 메시지 ----
    m = f"📊 매크로 조기경보 ({today.strftime('%Y-%m-%d')})\n"
    m += f"① 신용·자금: {total}/100 → {t_label}\n"
    m += f"② 금리 레짐: {LEVEL_EMOJI[g_level]} {LEVEL_KR[g_level]}\n"
    m += f"👉 {t_action}\n"
    if g_level >= Level.WARNING and total > 35:
        m += "🚨 두 축이 동시에 경고. 더 보수적인 쪽을 따를 것.\n"
    m += "─────────────────\n\n"

    m += f"1️⃣ 하이일드 스프레드  {hy_l} {hy_lab}\n"
    m += f"   현재 {hy}%  (1주 {delta_text(hy, hy_w)} / 1달 {delta_text(hy, hy_m)})\n"
    m += "   ▷ 부실기업 가산금리. '기업 부도 걱정' 온도계.\n"
    m += "   ▷ 기준: 3%↓ 안전 / 4%↑ 경계 / 5%↑ 위험\n"
    m += f"   ▷ {hy_c}\n\n"

    m += f"2️⃣ 장단기 금리차(10Y-2Y)  {yc_l} {yc_lab}\n"
    m += f"   현재 {yc}%  (1주 {delta_text(yc, yc_w)} / 1달 {delta_text(yc, yc_m)})\n"
    m += "   ▷ 10년물 − 2년물. 마이너스면 경기침체 예고등.\n"
    m += "   ▷ 기준: 0%↓ 역전 / 0~0.5% 정상화 초기 / 0.5~1.5% 정상\n"
    m += f"   ▷ {yc_c}\n\n"

    m += f"3️⃣ 단기자금 스트레스(SOFR-IORB)  {fd_l} {fd_lab}\n"
    if fd_spread is not None:
        m += f"   SOFR {sofr}% / 지준부리 {iorb}% → 격차 {fd_spread:+.1f}bp\n"
    m += "   ▷ 은행 간 하루짜리 조달비용. 벌어지면 시중에 돈이 마른다는 뜻.\n"
    m += "   ▷ 기준: 5bp↓ 원활 / 15bp↑ 경색 조짐 / 25bp↑ 위험\n"
    m += f"   ▷ {fd_c}\n\n"

    m += f"4️⃣ MOVE 지수(채권 변동성)  {mv_l} {mv_lab}\n"
    m += f"   현재 {move}  (1달 {delta_text(move, move_m, unit='p', digits=1)})\n"
    m += "   ▷ 채권판 공포지수. 채권이 먼저 흔들리고 주식이 뒤따름.\n"
    m += "   ▷ 기준: 80↓ 안정 / 100↑ 불안 / 120↑ 패닉\n"
    m += f"   ▷ {mv_c}\n\n"

    m += f"5️⃣ 금리 레짐(Rate Gravity)  {LEVEL_EMOJI[g_level]} {LEVEL_KR[g_level]}\n"
    m += f"   미 10년물 {ust_val if ust_val is not None else 'n/a'}%\n"
    m += f"   ▷ {ust_c}\n"
    m += f"   ▷ 기준: {UST10Y_WATCH} 관찰 / {UST10Y_WARNING} 경고 / {UST10Y_CRITICAL} 위험 (추세 돌파)\n"
    m += f"   ▷ 인플레: {inf_c}\n"
    m += f"   ▷ IG 회사채 OAS: {ig_c}\n"
    m += f"   ▷ 금리하락 조건: {relief_c}\n"
    m += f"   ▷ 대응: {gravity_action(g_level)}\n"
    if events:
        m += "   📅 예정: " + " / ".join(
            f"{d.strftime('%m-%d')} {t}" for d, t in events) + "\n"
    m += "   ※ 임계값은 단일 애널리스트 견해 기반(사례 2건). 참고용.\n\n"

    if vix:
        m += f"📎 참고 VIX(주식 변동성): {vix:.1f}\n\n"

    m += "※ 참고용 지표 요약이며 투자 판단·결과의 책임은 본인에게 있습니다."

    tg_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(tg_url, json={"chat_id": TELEGRAM_TO, "text": m})
    print("전송 성공!" if r.status_code == 200 else f"전송 실패: {r.text}")


if __name__ == "__main__":
    main()
