import os
import requests
import yfinance as yf
from datetime import datetime

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_TO = os.environ.get('TELEGRAM_TO')
FRED_API_KEY = os.environ.get('FRED_API_KEY')


# ──────────────────────────────────────────────
# 데이터 수집
# ──────────────────────────────────────────────
def get_fred_series(series_id, limit=90):
    """FRED에서 최근 데이터를 리스트로 가져오기 (최신순)"""
    url = (
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={series_id}&api_key={FRED_API_KEY}&file_type=json"
        f"&sort_order=desc&limit={limit}"
    )
    try:
        obs = requests.get(url, timeout=20).json()['observations']
        return [float(o['value']) for o in obs if o['value'] != '.']
    except Exception as e:
        print(f"FRED Error ({series_id}): {e}")
        return []


def get_yf_series(ticker, period="3mo"):
    """야후 파이낸스 종가 리스트 (최신순)"""
    try:
        hist = yf.Ticker(ticker).history(period=period)['Close'].dropna()
        return list(hist)[::-1]
    except Exception as e:
        print(f"Yahoo Error ({ticker}): {e}")
        return []


def val(series, idx=0):
    """리스트에서 안전하게 값 꺼내기 (idx=0이 최신, 5면 약 1주 전, 20이면 약 1달 전)"""
    if series and len(series) > idx:
        return series[idx]
    return None


def delta_text(now, past, unit="%p", digits=2):
    """변화량을 화살표와 함께 문자열로"""
    if now is None or past is None:
        return ""
    d = now - past
    arrow = "▲" if d > 0 else ("▼" if d < 0 else "－")
    return f"{arrow}{abs(d):.{digits}f}{unit}"


# ──────────────────────────────────────────────
# 지표별 해석 로직
# 각 함수는 (신호등, 상태라벨, 한줄해석, 위험점수 0~25) 반환
# ──────────────────────────────────────────────
def judge_hy(now, m1_ago):
    """하이일드 스프레드 = 정크본드 금리 - 국채 금리.
    시장이 '기업이 망할 확률'을 얼마로 보는지의 온도계. 낮으면 낙관, 급등하면 공포."""
    if now is None:
        return "⚪", "데이터없음", "-", 0

    widening = (m1_ago is not None) and (now - m1_ago >= 0.5)

    if now < 3.0:
        light, label, score = "🟢", "안전(과열/낙관)", 0
        comment = "신용시장 매우 안정. 다만 너무 낮으면 시장이 위험을 잊은 상태라 반전 시 낙폭이 큼."
    elif now < 4.0:
        light, label, score = "🟡", "보통", 6
        comment = "평균 수준. 아직 주식에 부정적 신호는 아님."
    elif now < 5.0:
        light, label, score = "🟠", "경계", 15
        comment = "기업 신용 우려 확대 국면. 주식 비중 점검 구간."
    else:
        light, label, score = "🔴", "위험", 25
        comment = "신용경색 신호. 과거 조정·침체 국면에서 나타난 레벨."

    if widening:
        score = min(25, score + 8)
        comment += " ★한 달 새 0.5%p 이상 급확대 → 절대수준보다 '속도'가 더 중요한 경고."
    return light, label, comment, score


def judge_curve(now, w1_ago, m1_ago, series):
    """장단기 금리차(10년-2년). 마이너스(역전)는 침체 예고,
    역전 뒤 다시 플러스로 가파르게 서는 국면이 역사적으로 실제 하락장 직전이었음."""
    if now is None:
        return "⚪", "데이터없음", "-", 0

    was_inverted = any(v < 0 for v in series[:250]) if series else False
    steepening = (m1_ago is not None) and (now - m1_ago >= 0.25)

    if now < 0:
        light, label, score = "🔴", "역전(경기침체 예고)", 20
        comment = "단기금리가 장기보다 높음. 통상 12~18개월 뒤 침체와 연결된 신호."
    elif now < 0.5:
        light, label, score = "🟡", "정상화 초기", 8
        comment = "역전에서 막 벗어난 구간. 과거엔 이 시점부터 몇 달 내 변동성이 커졌음."
    elif now < 1.5:
        light, label, score = "🟢", "정상", 3
        comment = "무난한 우상향 곡선. 특별한 경고 없음."
    else:
        light, label, score = "🟡", "가파른 정상화", 10
        comment = "금리차가 크게 벌어짐. 금리인하 기대 또는 경기둔화 반영일 수 있음."

    if was_inverted and steepening:
        score = min(25, score + 10)
        comment += " ★역전 해소 후 한 달 새 0.25%p 이상 가팔라짐 → 가장 주의해야 할 패턴."
    return light, label, comment, score


def judge_funding(sofr, iorb):
    """SOFR 자체보다 'SOFR - 지준부리(IORB)' 격차가 자금시장 스트레스 척도.
    은행끼리 하루짜리 돈 빌리는 값이 기준선보다 튀면 = 돈줄이 마름."""
    if sofr is None or iorb is None:
        return "⚪", "데이터없음", "-", 0, None

    spread = (sofr - iorb) * 100  # bp
    if spread < 5:
        light, label, score = "🟢", "원활", 0
        comment = "단기자금 시장 정상. 유동성 문제 없음."
    elif spread < 15:
        light, label, score = "🟡", "약간 빡빡", 7
        comment = "월말·분기말엔 흔한 수준이나 지속되면 관찰 필요."
    elif spread < 25:
        light, label, score = "🟠", "경색 조짐", 16
        comment = "레포시장 압박. 연준 유동성 조치가 논의될 수 있는 레벨."
    else:
        light, label, score = "🔴", "자금경색", 25
        comment = "2019년 레포 발작급. 위험자산에 즉각적 악재."
    return light, label, comment, score, spread


def judge_move(now, m1_ago):
    """MOVE = 채권판 공포지수. 채권이 흔들리면 며칠~몇 주 뒤 주식이 따라 흔들리는 경우가 많음."""
    if now is None:
        return "⚪", "데이터없음", "-", 0
    if now < 80:
        light, label, score = "🟢", "안정", 0
        comment = "채권시장 조용함. 주식에 우호적 환경."
    elif now < 100:
        light, label, score = "🟡", "보통", 5
        comment = "평범한 수준. 금리 이벤트 정도만 체크."
    elif now < 120:
        light, label, score = "🟠", "불안", 14
        comment = "금리 변동성 확대. 성장주·장기채 흔들릴 수 있음."
    else:
        light, label, score = "🔴", "패닉", 25
        comment = "채권시장 패닉. 주식 급락이 동반되는 구간."

    if m1_ago is not None and now - m1_ago >= 20:
        score = min(25, score + 5)
        comment += " ★한 달 새 20p 이상 급등 → 추세 악화."
    return light, label, comment, score


def total_judgement(score):
    """종합 점수 → 자산배분 가이드"""
    if score <= 15:
        return "🟢 평온", "위험자산 유지 국면. 정기매수 계획대로 진행해도 무난."
    if score <= 35:
        return "🟡 관찰", "특이 신호는 없으나 추세 점검 필요. 신규 레버리지는 자제."
    if score <= 55:
        return "🟠 경계", "현금 비중 확대 검토. QQQ 등 고베타 자산의 추가 매수는 신중히."
    if score <= 75:
        return "🔴 위험", "방어 전환 구간. 주식 비중 축소·헤지 수단 점검 권고."
    return "🚨 비상", "복수 지표가 동시 악화. 자본 보존 우선."


# ──────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────
def main():
    hy_s = get_fred_series("BAMLH0A0HYM2")
    yc_s = get_fred_series("T10Y2Y", limit=400)
    sofr_s = get_fred_series("SOFR")
    iorb_s = get_fred_series("IORB")
    move_s = get_yf_series("^MOVE")
    vix_s = get_yf_series("^VIX")

    hy, hy_w, hy_m = val(hy_s), val(hy_s, 5), val(hy_s, 20)
    yc, yc_w, yc_m = val(yc_s), val(yc_s, 5), val(yc_s, 20)
    sofr, iorb = val(sofr_s), val(iorb_s)
    move, move_m = val(move_s), val(move_s, 20)
    vix = val(vix_s)

    hy_l, hy_lab, hy_c, hy_sc = judge_hy(hy, hy_m)
    yc_l, yc_lab, yc_c, yc_sc = judge_curve(yc, yc_w, yc_m, yc_s)
    fd_l, fd_lab, fd_c, fd_sc, fd_spread = judge_funding(sofr, iorb)
    mv_l, mv_lab, mv_c, mv_sc = judge_move(move, move_m)

    total = hy_sc + yc_sc + fd_sc + mv_sc
    t_label, t_action = total_judgement(total)

    today = datetime.now().strftime("%Y-%m-%d")

    m = f"📊 매크로 조기경보 ({today})\n"
    m += f"종합 위험도: {total}/100 → {t_label}\n"
    m += f"👉 {t_action}\n"
    m += "─────────────────\n\n"

    m += f"1️⃣ 하이일드 스프레드  {hy_l} {hy_lab}\n"
    m += f"   현재 {hy}%  (1주 {delta_text(hy, hy_w)} / 1달 {delta_text(hy, hy_m)})\n"
    m += "   ▷ 부실기업이 돈 빌릴 때 붙는 가산금리. '기업 부도 걱정' 온도계.\n"
    m += "   ▷ 기준: 3%↓ 안전 / 4%↑ 경계 / 5%↑ 위험\n"
    m += f"   ▷ {hy_c}\n\n"

    m += f"2️⃣ 장단기 금리차(10Y-2Y)  {yc_l} {yc_lab}\n"
    m += f"   현재 {yc}%  (1주 {delta_text(yc, yc_w)} / 1달 {delta_text(yc, yc_m)})\n"
    m += "   ▷ 10년물에서 2년물을 뺀 값. 마이너스면 '경기침체 예고등'.\n"
    m += "   ▷ 기준: 0%↓ 역전 / 0~0.5% 정상화 초기(주의) / 0.5~1.5% 정상\n"
    m += f"   ▷ {yc_c}\n\n"

    m += f"3️⃣ 단기자금 스트레스(SOFR-IORB)  {fd_l} {fd_lab}\n"
    if fd_spread is not None:
        m += f"   SOFR {sofr}% / 지준부리 {iorb}% → 격차 {fd_spread:+.1f}bp\n"
    m += "   ▷ 은행 간 하루짜리 자금 조달비용. 벌어지면 시중에 돈이 마른다는 뜻.\n"
    m += "   ▷ 기준: 5bp↓ 원활 / 15bp↑ 경색 조짐 / 25bp↑ 위험\n"
    m += f"   ▷ {fd_c}\n\n"

    m += f"4️⃣ MOVE 지수(채권 변동성)  {mv_l} {mv_lab}\n"
    m += f"   현재 {move}  (1달 {delta_text(move, move_m, unit='p', digits=1)})\n"
    m += "   ▷ 채권판 공포지수. 채권이 먼저 흔들리고 주식이 뒤따르는 경우가 많음.\n"
    m += "   ▷ 기준: 80↓ 안정 / 100↑ 불안 / 120↑ 패닉\n"
    m += f"   ▷ {mv_c}\n\n"

    if vix:
        m += f"📎 참고 VIX(주식 변동성): {vix:.1f}\n\n"

    m += "※ 이 알림은 참고용 지표 요약이며 투자 판단·결과의 책임은 본인에게 있습니다."

    tg_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(tg_url, json={"chat_id": TELEGRAM_TO, "text": m})
    print("전송 성공!" if r.status_code == 200 else f"전송 실패: {r.text}")


if __name__ == "__main__":
    main()
