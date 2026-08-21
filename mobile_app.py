import streamlit as st
import requests
import yfinance as yf
from fugle_marketdata import RestClient
from datetime import datetime, time, timedelta
from google import genai
import base64
import streamlit.components.v1 as components
from PIL import Image
import re  
import pandas as pd  
import urllib3
import csv
import os

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==============================================================================
# 🧠 讀取外部參數檔
# ==============================================================================
try:
    import config 
except ImportError:
    st.error("🚨 找不到外部參數檔 `config.py`，請確認檔案位置！")
    st.stop()

# ==============================================================================
# 📱 頁面設定與手機專用 CSS
# ==============================================================================
st.set_page_config(
    page_title="台股當沖決策軍師", 
    page_icon="📈",
    layout="centered", # 手機版適合 centered
    initial_sidebar_state="collapsed"
)

# 注入手機觸控優化 CSS
st.markdown("""
<style>
    /* 隱藏 Streamlit 預設頁首頁尾，營造原生 App 感 */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
    
    /* 調整手機內距與字體 */
    .block-container {
        padding-top: 1rem;
        padding-bottom: 2rem;
        padding-left: 0.8rem;
        padding-right: 0.8rem;
    }
    
    /* 大按鈕適配手指點擊 */
    .stButton>button {
        width: 100%;
        height: 3rem;
        font-size: 1.1rem !important;
        font-weight: bold;
        border-radius: 10px;
        margin-top: 5px;
    }
    
    /* 數據卡片陰影 */
    div[data-testid="stMetric"] {
        background-color: #1e222d;
        border-radius: 8px;
        padding: 8px 12px;
        border: 1px solid #2e3546;
    }
</style>
""", unsafe_allow_html=True)

# 讀取金鑰
try:
    FUGLE_API_KEY = st.secrets["FUGLE_API_KEY"]
except:
    FUGLE_API_KEY = getattr(config, 'FUGLE_API_KEY', "")

try:
    GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
except:
    GEMINI_API_KEY = getattr(config, 'GEMINI_API_KEY', "")

# ==============================================================================
# 🎯 核心量化演算法 (TICK / ATR / EV / OpenScore)
# ==============================================================================
def get_tick_size(price):
    if price < 10: return 0.01
    elif price < 50: return 0.05
    elif price < 100: return 0.1
    elif price < 500: return 0.5
    elif price < 1000: return 1
    else: return 5

def round_to_tick(price):
    tick = get_tick_size(price)
    return round(round(price / tick) * tick, 2)

def st_copy_button(text):
    b64_text = base64.b64encode(text.encode('utf-8')).decode('utf-8')
    button_html = f"""
    <body style="margin: 0; padding: 0;">
    <button id="copyBtn" onclick="copyToClipboard()" style="
        width: 100%; border: 1px solid #444; border-radius: 8px; background: #262730;
        color: #fff; padding: 10px; font-size: 14px; cursor: pointer; font-weight: bold;
    ">📋 一鍵複製 AI 戰術報告</button>
    <script>
    function copyToClipboard() {{
        const bstr = window.atob('{b64_text}');
        const bytes = new Uint8Array(bstr.length);
        for (let i = 0; i < bstr.length; i++) bytes[i] = bstr.charCodeAt(i);
        const str = new TextDecoder('utf-8').decode(bytes);
        navigator.clipboard.writeText(str).then(function() {{
            const btn = document.getElementById('copyBtn');
            btn.innerHTML = '✅ 已成功複製到剪貼簿！'; btn.style.background = '#00a67d';
            setTimeout(() => {{ btn.innerHTML = '📋 一鍵複製 AI 戰術報告'; btn.style.background = '#262730'; }}, 2000);
        }});
    }}
    </script>
    </body>
    """
    components.html(button_html, height=45)

@st.cache_data(ttl=600)
def get_us_market_data():
    try:
        tickers = yf.Tickers("MU ^SOX ^IXIC")
        hist = tickers.history(period="5d")
        if hist.empty or len(hist) < 2: return 0.0, 0.0, 0.0
        return (
            round((hist['Close']['MU'].iloc[-1] / hist['Close']['MU'].iloc[-2] - 1) * 100, 2),
            round((hist['Close']['^SOX'].iloc[-1] / hist['Close']['^SOX'].iloc[-2] - 1) * 100, 2),
            round((hist['Close']['^IXIC'].iloc[-1] / hist['Close']['^IXIC'].iloc[-2] - 1) * 100, 2)
        )
    except: return 0.0, 0.0, 0.0

@st.cache_data(ttl=300) 
def get_volume_anomaly_score(stock_code):
    now = datetime.now().time()
    if now < time(9, 0) or now > time(13, 30): return 0 
    try:
        query_target = f"{stock_code}.TW" if stock_code.isdigit() else stock_code
        hist = yf.Ticker(query_target).history(period="1mo")
        if len(hist) >= 20:
            vol20_avg = float(hist['Volume'].tail(20).mean())
            last_vol = float(hist['Volume'].iloc[-1])
            if vol20_avg > 0:
                vol_ratio = last_vol / vol20_avg
                if vol_ratio > 3: return 2
                elif vol_ratio > 2: return 1
    except: pass
    return 0

def calc_orderbook_pressure(fugle_data):
    if not fugle_data: return 0
    bid_sum = sum(b.get('size', 0) for b in fugle_data.get('bids', [])[:5])
    ask_sum = sum(a.get('size', 0) for a in fugle_data.get('asks', [])[:5])
    if bid_sum + ask_sum == 0: return 0
    ratio = bid_sum / (bid_sum + ask_sum)
    if ratio >= 0.65: return 1
    elif ratio <= 0.35: return -1
    return 0

def calculate_quant_score(stock_code, current_price, night_change, foreign_oi, mu_chg, sox_chg, ndx_chg, fugle_data):
    score = 0
    semi_stocks = getattr(config, 'SEMI_STOCKS', ["2344", "2337", "2408", "6770", "2303"])
    
    if night_change >= 120: score += 2
    elif night_change >= 50: score += 1
    elif night_change <= -120: score -= 2
    elif night_change <= -50: score -= 1
    
    if foreign_oi >= 30000: score += 2
    elif foreign_oi >= 10000: score += 1
    elif foreign_oi <= -30000: score -= 2
    elif foreign_oi <= -10000: score -= 1

    score += round(ndx_chg * 1.0)
    if stock_code in semi_stocks:
        score += round(sox_chg * 1.2) 
        if mu_chg > 1.0: score += 1
        elif mu_chg < -1.0: score -= 1
    else:
        score += round(sox_chg * 0.5)
        
    orderbook_score = calc_orderbook_pressure(fugle_data) * getattr(config, 'ORDERBOOK_WEIGHT', 1)
    score += orderbook_score
    vol_score = get_volume_anomaly_score(stock_code) * getattr(config, 'VOLUME_WEIGHT', 1)
    score += vol_score

    if score >= 6: prob_str, range_l, range_h = "強勢開高 80%", round_to_tick(current_price * 1.01), round_to_tick(current_price * 1.025)
    elif 4 <= score <= 5: prob_str, range_l, range_h = "開高 70%", round_to_tick(current_price * 1.005), round_to_tick(current_price * 1.015)
    elif 1 <= score <= 3: prob_str, range_l, range_h = "偏多 55%", round_to_tick(current_price * 1.00), round_to_tick(current_price * 1.005)
    elif score == 0: prob_str, range_l, range_h = "方向不明 50%", round_to_tick(current_price * 0.997), round_to_tick(current_price * 1.003)
    elif -3 <= score <= -1: prob_str, range_l, range_h = "偏空 55%", round_to_tick(current_price * 0.995), round_to_tick(current_price * 1.00)
    elif -5 <= score <= -4: prob_str, range_l, range_h = "開低 70%", round_to_tick(current_price * 0.985), round_to_tick(current_price * 0.995)
    else: prob_str, range_l, range_h = "弱勢開低 80%", round_to_tick(current_price * 0.975), round_to_tick(current_price * 0.985)

    quant_result_str = f"總分：{score} 分 | 預測機率：{prob_str} | 預估區間：{range_l} ~ {range_h} | 動能:{orderbook_score}分 | 量能:{vol_score}分"
    return quant_result_str, score, prob_str

def determine_scenario(night_change, foreign_oi):
    if night_change > 0 and foreign_oi > 0: return {"name": "劇本一", "condition": "夜盤上漲 + 外資偏多", "feature": "開高，續漲機率高", "action": "偏多操作，但留意追高風險。", "type": "success"}
    elif night_change > 0 and foreign_oi <= 0: return {"name": "劇本二", "condition": "夜盤上漲 + 外資偏空", "feature": "先漲，提防開高走低", "action": "外資偏空，提防主力誘多洗盤。", "type": "warning"}
    elif night_change <= 0 and foreign_oi <= 0: return {"name": "劇本三", "condition": "夜盤下跌 + 外資偏空", "feature": "開低，續跌機率高", "action": "空方主導，順勢偏空防守。", "type": "error"}
    else: return {"name": "劇本四", "condition": "夜盤下跌 + 外資偏多", "feature": "先跌，開低反彈", "action": "外資籌碼具保護力，留意低接反彈。", "type": "info"}

@st.cache_data(ttl=60)
def get_stock_realtime_data(user_input):
    symbol = user_input.strip().split(".")[0]
    stock_name, current_p, ticker_info = None, None, None
    if FUGLE_API_KEY and FUGLE_API_KEY != "你的富果API金鑰":
        try:
            client = RestClient(api_key=FUGLE_API_KEY)
            ticker_info = client.stock.intraday.ticker(symbol=symbol)
            stock_name = ticker_info.get("name") if isinstance(ticker_info.get("name"), str) else ticker_info.get("name", {}).get("zh_TW")
            quote_info = client.stock.intraday.quote(symbol=symbol)
            current_p = quote_info.get("lastPrice") or quote_info.get("closePrice")
        except: pass

    if not stock_name:
        name_mapping = {"2344": "華邦電", "1815": "富喬", "2637": "慧洋-KY"}
        stock_name = name_mapping.get(symbol, f"代號 {symbol}")
        
    if current_p is None:
        try:
            ticker = yf.Ticker(f"{symbol}.TW" if symbol.isdigit() else symbol)
            current_p = getattr(ticker.fast_info, 'last_price', None)
            if not current_p: current_p = float(ticker.history(period="5d")['Close'].iloc[-1])
        except: current_p = 100.0
        
    return symbol, stock_name, round(float(current_p), 2), ticker_info

def get_fugle_market_quote(stock_code, current_price):
    symbol = stock_code.split(".")[0]
    if FUGLE_API_KEY and FUGLE_API_KEY != "你的富果API金鑰":
        try:
            return RestClient(api_key=FUGLE_API_KEY).stock.intraday.quote(symbol=symbol), "富果 API"
        except Exception: pass
    return {"lastPrice": current_price, "change": 0, "bids": [], "asks": []}, "模擬報價"

@st.cache_data(ttl=300)
def get_local_dynamic_sr(stock_code, current_price):
    try:
        query_target = f"{stock_code}.TW" if stock_code.isdigit() else stock_code
        hist = yf.Ticker(query_target).history(period="1mo")
        fallback_atr = max(current_price * 0.015, 0.5) 
        if len(hist) < 14:
            return round_to_tick(current_price * 0.98), round_to_tick(current_price * 1.02), "資料不足", 0, 0, 0, 0, fallback_atr, fallback_atr
        
        high_low = hist['High'] - hist['Low']
        high_close = (hist['High'] - hist['Close'].shift()).abs()
        low_close = (hist['Low'] - hist['Close'].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr_14 = tr.rolling(14).mean().iloc[-1]
        if pd.isna(atr_14) or atr_14 <= 0: atr_14 = fallback_atr
        
        y_close = float(hist['Close'].iloc[-2])
        y_high, y_low = float(hist['High'].iloc[-2]), float(hist['Low'].iloc[-2])
        c_high, c_low = float(hist['High'].iloc[-1]), float(hist['Low'].iloc[-1])
        today_range = max(c_high - c_low, 0.01)
        
        f_high, f_low = float(hist['High'].tail(5).max()), float(hist['Low'].tail(5).min())
        limit_up, limit_down = round_to_tick(y_close * 1.10), round_to_tick(y_close * 0.90)
        
        ceilings = [p for p in [c_high, y_high] if p > current_price]
        floors = [p for p in [c_low, y_low] if p < current_price]
        res = min(ceilings) if ceilings else current_price * 1.02
        sup = max(floors) if floors else current_price * 0.98
        
        analysis = f"🎯 大天花板共振: {round_to_tick(res)}\n📍 大地板共振: {round_to_tick(sup)}"
        return round_to_tick(sup), round_to_tick(res), analysis, limit_up, limit_down, round_to_tick(f_high), round_to_tick(f_low), round(atr_14, 2), round(today_range, 2)
    except:
        fallback_atr = max(current_price * 0.015, 0.5)
        return round_to_tick(current_price * 0.98), round_to_tick(current_price * 1.02), "計算異常", 0, 0, 0, 0, fallback_atr, fallback_atr

def get_intraday_box(current_price, vol_score, atr_14):
    box_coef = getattr(config, 'BOX_ATR_COEF', 0.15)
    min_pct = getattr(config, 'BOX_MIN_PCT', 0.003)
    max_pct = getattr(config, 'BOX_MAX_PCT', 0.02)

    raw_half_width = atr_14 * box_coef
    half_width = max(current_price * min_pct, min(raw_half_width, current_price * max_pct))

    box_high = round_to_tick(current_price + half_width)
    box_low = round_to_tick(current_price - half_width)
    box_mid = round_to_tick((box_high + box_low) / 2)
    width_pct = round(((box_high - box_low) / current_price) * 100, 2)

    status = "盤整震盪"
    if current_price >= box_high: status = "突破天花"
    elif current_price <= box_low: status = "跌破地板"

    warning_msg = ""
    if abs(current_price - box_mid) <= (current_price * 0.003) and vol_score == 0:
        warning_msg = f"⚠️ 【洗盤警示】現價 {current_price} 位於震盪中線且無量，極易雙巴！"

    return {"box_high": box_high, "box_low": box_low, "box_mid": box_mid, "width_pct": width_pct, "status": status, "warning_msg": warning_msg}

def evaluate_trade_feasibility(stock_code, price, support, resistance, quant_score, atr_14, today_range, box_high, box_low, orderbook_score):
    win_prob = round(0.50 + 0.10 * min(1.0, abs(quant_score) / 6.0), 3)
    risk_unit = round(max(atr_14 * 0.3, box_high - box_low), 2)
    if risk_unit <= 0: risk_unit = 0.01

    long_reward = max(0, resistance - price)
    long_rr = long_reward / risk_unit if risk_unit > 0 else 0
    long_ev = win_prob * long_reward - (1 - win_prob) * risk_unit

    short_reward = max(0, price - support)
    short_rr = short_reward / risk_unit if risk_unit > 0 else 0
    short_ev = win_prob * short_reward - (1 - win_prob) * risk_unit

    if long_ev >= short_ev:
        direction, rr, ev, stop, target = "偏多 (Long)", long_rr, long_ev, round_to_tick(price - risk_unit), resistance
    else:
        direction, rr, ev, stop, target = "偏空 (Short)", short_rr, short_ev, round_to_tick(price + risk_unit), support

    rr_threshold = getattr(config, 'PREMARKET_RR', 1.2) if time(8, 30) <= datetime.now().time() < time(9, 0) else getattr(config, 'NORMAL_RR', 1.5)
    
    if rr < rr_threshold or ev <= 0: feasibility = "不建議進場"
    elif rr < rr_threshold * 1.3 or ev < risk_unit * 0.3: feasibility = "勉強可行"
    else: feasibility = "安全可行"

    return {
        "方向": direction, "進場價": price, "停損價": stop, "目標價": target,
        "風險單位": risk_unit, "RR": round(rr, 2), "勝率估計": f"{round(win_prob*100)}%",
        "期望值EV": round(ev, 3), "可行性": feasibility, "RR門檻": rr_threshold
    }

def call_gemini(prompt, images=None):
    if not GEMINI_API_KEY: return "⚠️ 無 API 金鑰。"
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        contents = [prompt]
        if images:
            for img in images[:5]: contents.append(Image.open(img))
        return client.models.generate_content(model="gemini-2.5-flash", contents=contents).text
    except Exception as e:
        return f"💡 AI 運算異常 ({e})"

@st.cache_data(ttl=600)
def get_latest_foreign_oi():
    try:
        df = pd.DataFrame(requests.get(f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanFuturesInstitutionalInvestors&data_id=TX&start_date={(datetime.now() - timedelta(days=15)).strftime('%Y-%m-%d')}", verify=False).json()["data"])
        return int(df[df['name'].astype(str).str.contains('外資', na=False)].iloc[-1]['open_interest_netlot'])
    except: return -30000

@st.cache_data(ttl=300)
def get_night_session_change():
    try:
        df = yf.Ticker("TWF=F").history(period="5d")
        return int(df['Close'].iloc[-1] - df['Close'].iloc[-2])
    except: return 0

# ==============================================================================
# 📱 手機端極簡主介面 (S25 Ultra 專用)
# ==============================================================================
st.markdown("### 📱 台股當沖 AI 軍師")

# 1. 頂部快速切換標的
col_in1, col_in2 = st.columns([2, 1])
with col_in1:
    user_input = st.text_input("個股代號", value="2344", label_visibility="collapsed")
with col_in2:
    st.write("") # 間距
    btn_refresh = st.button("🔄 刷新", use_container_width=True)

stock_code, stock_name, api_realtime_price, ticker_info = get_stock_realtime_data(user_input)

# 2. 即時現價與大盤指標摘要
night_change = get_night_session_change()
foreign_oi = get_latest_foreign_oi()
mu_chg, sox_chg, ndx_chg = get_us_market_data()
fugle_data, fugle_src = get_fugle_market_quote(stock_code, api_realtime_price)
sup, res, sr_str, l_up, l_down, f_h, f_l, atr_14, t_range = get_local_dynamic_sr(stock_code, api_realtime_price)
intraday_box = get_intraday_box(api_realtime_price, 0, atr_14)
quant_str, quant_score, prob_str = calculate_quant_score(stock_code, api_realtime_price, night_change, foreign_oi, mu_chg, sox_chg, ndx_chg, fugle_data)
eval_res = evaluate_trade_feasibility(stock_code, api_realtime_price, sup, res, quant_score, atr_14, t_range, intraday_box['box_high'], intraday_box['box_low'], 0)

# 手機雙欄指標卡片
m_c1, m_c2 = st.columns(2)
m_c1.metric(f"📈 {stock_name} ({stock_code})", f"{api_realtime_price} 元", delta=f"{intraday_box['status']}")
m_c2.metric("🤖 量化評分 / 機率", prob_str, delta=f"{quant_score} 分")

m_c3, m_c4 = st.columns(2)
feas_delta = "🔴 高風險" if eval_res['可行性'] == "不建議進場" else "🟢 條件符合"
m_c3.metric("🎯 系統判定", eval_res['可行性'], delta=feas_delta)
m_c4.metric("⚖️ 實算 RR / EV", f"RR: {eval_res['RR']}", delta=f"EV: {eval_res['期望值EV']}")

# 微觀作戰天花與地板提示
st.info(f"🔺 **天花板:** `{intraday_box['box_high']}` | 🔻 **地板:** `{intraday_box['box_low']}` | 🛡️ **停損線:** `{eval_res['停損價']}`")

st.markdown("---")

# ==============================================================================
# 🗂️ 核心功能三大 Tab (手機專用)
# ==============================================================================
tab_pre, tab_live, tab_post = st.tabs(["🌅 盤前推演", "⚡ 盤中當沖", "🌙 盤後明日劇本"])

# --- 🌅 Tab 1: 盤前推演 (08:30 前) ---
with tab_pre:
    st.caption("適用時間：08:30 前，綜合夜盤、美股連動與外資未平倉推演開盤劇本。")
    scenario = determine_scenario(night_change, foreign_oi)
    st.warning(f"🎭 **今日早盤劇本：【{scenario['name']}】**\n\n* **特徵：** {scenario['feature']}\n* **策略：** {scenario['action']}")
    
    if st.button("🚀 生成【盤前完整戰術】", key="btn_pre"):
        with st.spinner("AI 軍師盤前推演中..."):
            prompt = f"{getattr(config, 'PRE_MARKET_LOGIC', '')}\n\n{quant_str}\n標的：{stock_name} ({stock_code}) | 現價：{api_realtime_price}\n大格局防守：地板 {sup} | 天花板 {res}\n天花:{intraday_box['box_high']} | 地板:{intraday_box['box_low']}\n系統派發戰術：方向 {eval_res['方向']}, 進場 {eval_res['進場價']}, 停損 {eval_res['停損價']}, 目標 {eval_res['目標價']}, RR {eval_res['RR']}, EV {eval_res['期望值EV']}"
            res_text = call_gemini(prompt)
            st.markdown("#### 📋 戰術報告：")
            st.write(res_text)
            st_copy_button(res_text)

# --- ⚡ Tab 2: 盤中即刻當沖 (09:00~13:30) ---
with tab_live:
    st.caption("適用時間：09:00~13:30，根據即時天花板/地板突破與五檔壓力極速給出建議。")
    if intraday_box['warning_msg']:
        st.error(intraday_box['warning_msg'])
        
    if st.button("⚡ 極速分析【當下盤勢】", key="btn_live"):
        with st.spinner("AI 結合微觀天花板與地板運算中..."):
            prompt = f"{getattr(config, 'MY_TRADING_LOGIC', '')}\n\n{quant_str}\n標的：{stock_name} ({stock_code}) | 現價：{api_realtime_price}\n{sr_str}\n天花:{intraday_box['box_high']} | 地板:{intraday_box['box_low']}\n系統派發：判定【{eval_res['可行性']}】| 方向 {eval_res['方向']} | 進場 {eval_res['進場價']} | 停損 {eval_res['停損價']} | 目標 {eval_res['目標價']} | RR {eval_res['RR']} | EV {eval_res['期望值EV']}"
            res_text = call_gemini(prompt)
            st.markdown("#### 📋 即時當沖指引：")
            st.write(res_text)
            st_copy_button(res_text)

# --- 🌙 Tab 3: 盤後覆盤明日劇本 (13:30 後) ---
with tab_post:
    st.caption("適用時間：收盤後，可直接用手機上傳券商籌碼截圖，AI 自動判讀大戶動向。")
    uploaded_files = st.file_uploader("📤 點擊選取手機內的看盤截圖", type=["png", "jpg", "jpeg"], accept_multiple_files=True)
    
    if st.button("🔮 生成【明日當沖劇本】", key="btn_post"):
        with st.spinner("AI 正在判讀籌碼截圖並擬定明日劇本..."):
            prompt = f"{getattr(config, 'TOMORROW_STRATEGY_LOGIC', '')}\n\n{quant_str}\n標的：{stock_name} ({stock_code}) | 今日收盤：{api_realtime_price}\n{sr_str}\n天花:{intraday_box['box_high']} | 地板:{intraday_box['box_low']}\n系統派發：方向 {eval_res['方向']}, 進場 {eval_res['進場價']}, 停損 {eval_res['停損價']}, 目標 {eval_res['目標價']}, RR {eval_res['RR']}, EV {eval_res['期望值EV']}"
            res_text = call_gemini(prompt, images=uploaded_files)
            st.markdown("#### 📋 明日早盤劇本：")
            st.write(res_text)
            st_copy_button(res_text)