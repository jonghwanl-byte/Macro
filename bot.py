import os
import requests
import yfinance as yf
from datetime import datetime

# GitHub Secrets 또는 로컬 환경변수에서 키값 불러오기
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_TO = os.environ.get('TELEGRAM_TO')
FRED_API_KEY = os.environ.get("FRED_API_KEY")

def get_fred_data(series_id):
    """FRED API에서 최신 지표 1개 가져오기"""
    url = f"https://api.stlouisfed.org/fred/series/observations?series_id={series_id}&api_key={FRED_API_KEY}&file_type=json&sort_order=desc&limit=1"
    
    try:
        response = requests.get(url).json()
        value = response['observations'][0]['value']
        return float(value) if value != '.' else None
    except Exception as e:
        print(f"FRED API Error ({series_id}): {e}")
        return None

def get_move_index():
    """Yahoo Finance에서 MOVE 지수 가져오기"""
    try:
        move = yf.Ticker("^MOVE")
        return round(move.history(period="1d")['Close'].iloc[-1], 2)
    except Exception as e:
        print(f"Yahoo Finance Error: {e}")
        return None

def main():
    # 주요 매크로 지표 시리즈 ID
    # BAMLH0A0HYM2 : 하이일드 스프레드
    # T10Y2Y : 미국 10년물 - 2년물 금리차
    # SOFR : 단기자금시장 금리 (TED 스프레드 대체)
    
    hy_spread = get_fred_data("BAMLH0A0HYM2")
    yield_curve = get_fred_data("T10Y2Y")
    sofr = get_fred_data("SOFR")
    move_index = get_move_index()
    
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    # 텔레그램 메시지 포맷팅
    msg = f"🚨 [월스트리트 매크로 조기경보] ({today_str})\n\n"
    msg += f"1. 하이일드 스프레드: {hy_spread}%\n"
    msg += f"2. 장단기 금리차(10Y-2Y): {yield_curve}%\n"
    msg += f"3. SOFR (단기자금): {sofr}%\n"
    msg += f"4. MOVE 지수 (채권변동성): {move_index}\n\n"
    
    # 간단한 알림 로직 (위험 수위 도달 시 경고)
    if hy_spread and hy_spread > 5.0:
        msg += "⚠️ 주의: 하이일드 스프레드가 5%를 초과했습니다. 신용 경색 우려!\n"
    if yield_curve and yield_curve > 0:
        msg += "⚠️ 주의: 장단기 금리차가 정상화(양수)되었습니다. 변동성에 대비하세요.\n"
    if move_index and move_index > 120:
        msg += "⚠️ 주의: MOVE 지수가 120을 돌파했습니다. 채권 시장 패닉 조짐!\n"
        
    # 텔레그램 전송
    tg_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_TO, "text": msg}
    response = requests.post(tg_url, json=payload)
    
    if response.status_code == 200:
        print("메시지 전송 성공!")
    else:
        print(f"전송 실패: {response.text}")

if __name__ == "__main__":
    main()
