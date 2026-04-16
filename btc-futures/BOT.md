# BTC Futures Auto-Bot — Design Document

> Tài liệu thiết kế chi tiết cho hệ thống trading bot tự động chạy trên OKX Futures.  
> Đọc kỹ toàn bộ trước khi bắt đầu code.

---

## 1. Tổng quan hệ thống

Bot chạy theo chu kỳ 2 giờ, tự động phân tích thị trường và quản lý position BTC-USDT Futures trên OKX. Mỗi chu kỳ bot quyết định một trong ba hành động:

- **Không làm gì** — tín hiệu không đủ mạnh, chờ chu kỳ tiếp theo
- **Mở lệnh mới** — tín hiệu đủ điều kiện, đặt limit order + algo TP/SL
- **Quản lý position hiện tại** — thêm TP/SL nếu thiếu, hoặc đóng sớm nếu nguy hiểm

Sau mỗi chu kỳ, bot gửi báo cáo qua Telegram.

---

## 2. Kiến trúc hệ thống

```
┌─────────────────────────────────────────────────────────┐
│                    Scheduler (2h cron)                  │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│                   Bot Main Loop                         │
│                                                         │
│  0. Startup reconcile (OKX ↔ state.json)               │
│  1. Load state.json                                     │
│  2. Fetch OKX private data (position, balance, orders)  │
│  3. Fetch OKX public data (price, funding, OI)          │
│  4. Circuit breaker check                               │
│  5. Route to state machine                              │
│  6. Execute action (dry run: log only)                  │
│  7. Save state.json (atomic write)                      │
│  8. Send Telegram report                                │
└─────────────────────────────────────────────────────────┘
                         │
           ┌─────────────┼─────────────┐
           ▼             ▼             ▼
    [NO POSITION]  [HAS POSITION]  [CIRCUIT BREAK]
    Analyze signal  Manage position  Skip + alert
    Place if OK     Add TP/SL        
                    Close if danger  
```

### Cấu trúc thư mục bot

```
btc-futures/
├── btc.py                    # CLI gốc (giữ nguyên)
├── commands/
│   ├── trade.py              # Phân tích multi-TF (dùng lại)
│   ├── trade_agent.py        # Vibe-trading agent (dùng lại)
│   └── ...
├── bot/
│   ├── __init__.py
│   ├── main.py               # Entrypoint: parse args, run loop
│   ├── scheduler.py          # APScheduler wrapper (2h interval)
│   ├── state.py              # Load/save state.json (atomic)
│   ├── reconciler.py         # Startup: đồng bộ OKX ↔ state.json
│   ├── okx_private.py        # OKX private API: balance, orders, positions
│   ├── okx_errors.py         # Error classification + retry logic
│   ├── order_manager.py      # Place order, set algo TP/SL, close position
│   ├── pending_order.py      # Quản lý vòng đời pending order
│   ├── position_guard.py     # Kiểm tra position nguy hiểm
│   ├── circuit_breaker.py    # Max daily loss, max open hours
│   ├── telegram_bot.py       # Send alerts, receive commands
│   └── report.py             # Format báo cáo Telegram
├── state.json                # Runtime state (auto-generated)
├── state.json.bak            # Backup trước mỗi lần ghi
├── bot.log                   # Log file
├── BOT.md                    # Tài liệu này
├── Dockerfile                # Docker packaging
└── docker-compose.yml        # Docker Compose config
```

---

## 3. State Machine

### 3.1 States

```
IDLE                — Không có position, đang chờ
ANALYZING           — Đang chạy phân tích multi-TF + agent
PENDING_ENTRY       — Đã đặt limit order, chưa fill
IN_POSITION         — Position đang mở, có TP/SL
IN_POSITION_NO_SL   — Position đang mở, CHƯA có algo TP/SL
CLOSING             — Đang đóng position (market order)
CIRCUIT_BREAK       — Dừng giao dịch tạm thời
```

### 3.2 Flow đầy đủ

```
┌─────────────────────────────────────────────────────────────────┐
│  Chu kỳ bắt đầu                                                │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                    [Circuit breaker?]
                    YES ──► Log + Telegram alert → END
                    NO  ──► Tiếp tục
                           │
                    [Có position trên OKX?]
                           │
          ┌────────────────┴───────────────────┐
          YES                                  NO
          │                                    │
   [Có algo TP/SL?]                    [Pending order tồn tại?]
          │                                    │
    NO ──► Thêm TP/SL                   YES ──► Quản lý pending order
    YES──► [Position nguy hiểm?]         NO ──► Phân tích tín hiệu
          │
    YES──► Close position → Phân tích lại
    NO ──► Báo cáo trạng thái → END
                                         │
                               [Tín hiệu đủ điều kiện?]
                                         │
                            NO ──► Báo cáo "no trade" → END
                            YES──► Đặt limit order + algo TP/SL
                                         │
                                   Báo cáo → END
```

### 3.3 Điều kiện "position nguy hiểm"

Position bị đánh dấu nguy hiểm nếu thoả MỘT trong các điều kiện:

| Điều kiện | Mô tả |
|-----------|-------|
| **Giá chạm vào vùng SL** | Giá hiện tại cách SL < `DANGER_SL_ATR_MULT × ATR_1H` |
| **Tín hiệu đảo chiều mạnh** | Multi-TF direction flip, confidence ≥ 70% ngược chiều position |
| **Funding rate cực đoan** | Funding > 0.1%/8h VÀ đang LONG (hoặc < -0.1% VÀ đang SHORT) |
| **Giữ quá lâu** | Position mở > `MAX_OPEN_HOURS` giờ |
| **Floating loss quá lớn** | Unrealized PnL < -`DANGER_LOSS_PCT`% của balance |

Khi nguy hiểm → đóng bằng market order → Telegram alert → phân tích lại ngay.

---

## 4. Cấu hình .env

### 4.1 File mẫu đầy đủ

```env
# ── OKX API (private) ─────────────────────────────────────────────
OKX_API_KEY=your_api_key_here
OKX_SECRET_KEY=your_secret_key_here
OKX_API_PASSPHRASE=your_passphrase_here
OKX_DEMO_MODE=false              # true = paper trading (testnet)

# ── Vibe-Trading Agent ────────────────────────────────────────────
LANGCHAIN_MODEL_NAME=gemini-2.5-flash
GOOGLE_API_KEY=your_google_api_key

# ── Telegram ──────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN=1234567890:ABCdef...
TELEGRAM_CHAT_ID=-100123456789   # Group chat ID (negative) hoặc user ID

# ── Bot Behaviour ─────────────────────────────────────────────────
BOT_SYMBOL=BTC-USDT-SWAP         # OKX perpetual swap symbol
BOT_INTERVAL_HOURS=2             # Chu kỳ chạy (giờ)
DRY_RUN=true                     # true = chỉ log, không đặt lệnh thật

# ── Position Sizing ───────────────────────────────────────────────
ACCOUNT_BALANCE_USDT=100         # Số dư tài khoản (chỉ dùng khi dry_run)
RISK_PCT=1.0                     # % balance risk mỗi lệnh (default 1%)
LEVERAGE=5                       # Leverage (1x - 20x)
ORDER_TYPE=limit                 # limit | market

# ── Signal Quality Filter ─────────────────────────────────────────
MIN_CONFIDENCE=60                # Minimum confidence % để mở lệnh (0-100)
MIN_NET_SCORE=0.3                # Minimum |net_score| (0.0-1.0)
MIN_AGREEING_TF=2                # Minimum timeframes đồng thuận (1H/4H/1D)
REQUIRE_AGENT=false              # true = bắt buộc có agent output, false = dùng local fallback

# ── TP/SL ─────────────────────────────────────────────────────────
SL_ATR_MULT=1.5                  # Stop loss = entry ± SL_ATR_MULT × ATR_blend
TP1_ATR_MULT=2.0                 # Take profit 1 = entry ± TP1_ATR_MULT × ATR_blend
TP2_ATR_MULT=3.5                 # Take profit 2 = entry ± TP2_ATR_MULT × ATR_blend
MIN_SL_PCT=0.3                   # SL tối thiểu cách entry (%, reject nếu nhỏ hơn)

# ── Circuit Breaker ───────────────────────────────────────────────
MAX_DAILY_LOSS_PCT=3.0           # Dừng bot nếu ngày lỗ > 3% balance
MAX_OPEN_HOURS=48                # Tự đóng position nếu giữ > 48h
DANGER_SL_ATR_MULT=0.5          # "Nguy hiểm" nếu giá cách SL < 0.5 × ATR_1H
DANGER_LOSS_PCT=2.0              # "Nguy hiểm" nếu floating loss > 2% balance
DANGER_SIGNAL_FLIP=true          # Kích hoạt danger khi signal flip mạnh

# ── Pending Order ─────────────────────────────────────────────────
PENDING_CANCEL_HOURS=6           # Huỷ limit order nếu chưa fill sau 6h
PENDING_CANCEL_DRIFT_PCT=0.5    # Huỷ nếu giá đi xa entry > 0.5%
```

### 4.2 Giải thích chi tiết

**`OKX_DEMO_MODE=true`**: Dùng OKX paper trading endpoint. Cho phép test toàn bộ flow mà không mất tiền thật. **Nên bật khi chạy lần đầu.**

**`DRY_RUN=true`**: Bot phân tích bình thường nhưng không gọi API đặt lệnh. Chỉ log và Telegram thông báo "would place order". Dùng để test logic mà không cần OKX demo account.

**`REQUIRE_AGENT=false`**: Nếu `true`, bot sẽ bỏ qua chu kỳ khi agent fail thay vì fallback về local analysis. Nên để `false` để đảm bảo bot luôn có quyết định.

**`MIN_CONFIDENCE=60`**: Confidence tính từ agreeing timeframes. Dưới 60% = NO TRADE. Tăng lên 70-75% nếu muốn bot ít giao dịch hơn nhưng chất lượng hơn.

**`LEVERAGE=5`**: Leverage dùng để tính position sizing. Bot sẽ **kiểm tra leverage thực tế trên OKX khi khởi động** và cảnh báo nếu khác với config. Cần set leverage thủ công trên OKX hoặc thêm API call trong bước init.

**`PENDING_CANCEL_HOURS=6`**: Sau khi đặt limit order, nếu sau 6h (3 chu kỳ) vẫn chưa fill thì huỷ. Tránh tình huống limit order treo lơ lửng mãi.

**`PENDING_CANCEL_DRIFT_PCT=0.5`**: Huỷ order nếu giá thị trường đã đi xa khỏi entry price > 0.5% (thị trường đã bỏ lại lệnh của mình).

---

## 5. OKX Private API

### 5.1 Authentication

OKX V5 dùng HMAC-SHA256 signature:

```
timestamp + method + requestPath + body
```

Header bắt buộc:
- `OK-ACCESS-KEY`: API Key
- `OK-ACCESS-SIGN`: base64(HMAC-SHA256(secret, prehash))
- `OK-ACCESS-TIMESTAMP`: ISO 8601 UTC timestamp
- `OK-ACCESS-PASSPHRASE`: Passphrase
- `x-simulated-trading: 1` (chỉ khi demo mode)

### 5.2 Endpoints cần dùng

| Endpoint | Method | Mục đích |
|----------|--------|----------|
| `/api/v5/account/balance` | GET | Lấy số dư USDT |
| `/api/v5/account/positions` | GET | Danh sách position đang mở |
| `/api/v5/account/config` | GET | Kiểm tra leverage hiện tại |
| `/api/v5/account/set-leverage` | POST | Set leverage (nếu cần) |
| `/api/v5/trade/order` | POST | Đặt order (market/limit) |
| `/api/v5/trade/orders-pending` | GET | Danh sách pending orders |
| `/api/v5/trade/cancel-order` | POST | Huỷ pending order |
| `/api/v5/trade/close-position` | POST | Đóng position (market) |
| `/api/v5/trade/order-algo` | POST | Đặt algo TP/SL (attachAlgoOrds) |
| `/api/v5/trade/orders-algo-pending` | GET | Kiểm tra algo orders có tồn tại |
| `/api/v5/trade/cancel-algos` | POST | Huỷ algo order |
| `/api/v5/trade/fills` | GET | Lịch sử fill (reconcile) |

### 5.3 Kiểm tra leverage khi khởi động

```python
def verify_leverage():
    config = okx.get_account_config(instId=BOT_SYMBOL)
    current_leverage = int(config["lever"])
    
    if current_leverage != LEVERAGE:
        logger.warning(f"Leverage mismatch: OKX={current_leverage}, config={LEVERAGE}")
        telegram(f"⚠️ Leverage OKX ({current_leverage}x) ≠ config ({LEVERAGE}x). Tự động điều chỉnh...")
        okx.set_leverage(instId=BOT_SYMBOL, lever=LEVERAGE, mgnMode="cross")
```

### 5.4 Flow đặt lệnh mới

```
1. POST /trade/order
   {
     "instId": "BTC-USDT-SWAP",
     "tdMode": "cross",          # cross margin
     "side": "buy" | "sell",
     "ordType": "limit",
     "sz": "<contracts>",         # số hợp đồng
     "px": "<entry_price>"
   }
   → trả về ordId

2. POST /trade/order-algo         # Gắn TP/SL vào order vừa đặt
   {
     "instId": "BTC-USDT-SWAP",
     "tdMode": "cross",
     "algoType": "oco",
     "tpTriggerPx": "<tp1_price>",
     "tpOrdPx": "-1",             # -1 = market order khi chạm TP
     "slTriggerPx": "<sl_price>",
     "slOrdPx": "-1",
     "attachAlgoOrds": true,
     "ordId": "<ordId từ bước 1>"
   }

3. Lưu ordId vào state.json ngay sau bước 1 (trước bước 2)
   → Nếu crash giữa chừng, reconciler biết có pending order
```

### 5.5 Position sizing

```
Position size (USDT) = balance × RISK_PCT / 100 / SL_PCT

Trong đó SL_PCT = |entry - sl| / entry

Contracts = Position size × LEVERAGE / entry_price / contract_size
```

OKX BTC-USDT-SWAP: 1 contract = 0.01 BTC (contract_size = 0.01).

```python
# Ví dụ
balance    = 100 USDT
risk_pct   = 1.0    # 1% = 1 USDT risk
sl_pct     = 0.015  # SL 1.5% từ entry
leverage   = 5

usdt_at_risk   = 100 × 0.01 = 1 USDT
position_usdt  = 1 / 0.015  = 66.67 USDT
with_leverage  = 66.67 × 5  = 333.33 USDT (notional)
contracts      = 333.33 / entry_price / 0.01
```

---

## 6. Signal Quality Filter

### 6.1 Điều kiện mở lệnh mới

Bot chỉ mở lệnh khi TẤT CẢ điều kiện sau đều đúng:

| Điều kiện | Nguồn | Ngưỡng |
|-----------|-------|--------|
| `direction != 0` | Local multi-TF | LONG hoặc SHORT |
| `confidence >= MIN_CONFIDENCE` | Local multi-TF | ≥ 60% |
| `net_score >= MIN_NET_SCORE` | Local multi-TF | ≥ 0.3 |
| `agreeing_tfs >= MIN_AGREEING_TF` | 1H/4H/1D | ≥ 2 TFs |
| `abs(entry - sl) / entry >= MIN_SL_PCT/100` | Agent/local | ≥ 0.3% |
| Không có pending order nào | OKX API | — |
| Không có position nào | OKX API | — |
| Circuit breaker chưa kích hoạt | state.json | — |

### 6.2 Điều kiện từ chối (NO TRADE)

| Trường hợp | Lý do |
|------------|-------|
| `direction == 0` | Không đồng thuận |
| Confidence thấp | Tín hiệu yếu |
| Counter-trend mạnh | 1D ngược 4H, không giao dịch |
| SL quá sát entry | Slippage sẽ ăn hết buffer |
| Funding cực đoan | Carry cost quá cao |

---

## 7. Position Management

### 7.1 Kiểm tra chu kỳ

Mỗi chu kỳ 2h, với position đang mở:

```
1. Lấy position info từ OKX (liveSize, avgPx, unrealizedPnl)
2. Lấy algo orders pending → có TP/SL chưa?
3. Nếu chưa có TP/SL → đặt algo TP/SL ngay
4. Tính danger score
5. Nếu nguy hiểm → close_position() → phân tích lại
6. Nếu an toàn → log status → báo cáo Telegram
```

### 7.2 Xử lý partial fill

Khi TP1 hoặc SL chạm và fill một phần (OKX có thể fill từng phần với large positions):

```python
def sync_position_size():
    live = okx.get_position(instId=BOT_SYMBOL)
    live_size = float(live["pos"])          # contracts còn lại trên OKX
    
    if live_size == 0:
        # Position đã đóng hoàn toàn (TP hoặc SL hit)
        state["position"]["active"] = False
        record_realized_pnl(live)
        telegram("✅ Position đã đóng hoàn toàn (TP/SL hit)")
    elif live_size != state["position"]["size_contracts"]:
        # Partial fill đã xảy ra
        old_size = state["position"]["size_contracts"]
        state["position"]["size_contracts"] = live_size
        logger.info(f"Partial fill detected: {old_size} → {live_size} contracts")
        telegram(f"⚡ Partial fill: còn {live_size} contracts")
```

Nguyên tắc: **OKX là source of truth** — luôn dùng `live_size` từ OKX, không tin state.json nếu có sai lệch.

### 7.3 Danger score

```python
danger = False
reasons = []

# Điều kiện 1: Giá gần SL
atr_1h = fetch_atr(interval="1H")
sl_distance = abs(current_price - sl_price)
if sl_distance < DANGER_SL_ATR_MULT * atr_1h:
    danger = True
    reasons.append(f"Giá cách SL {sl_distance:.0f} < {DANGER_SL_ATR_MULT}×ATR={DANGER_SL_ATR_MULT*atr_1h:.0f}")

# Điều kiện 2: Signal flip
tf_result = run_local_analysis()
if tf_result.direction != position_direction and tf_result.confidence >= 70:
    danger = True
    reasons.append(f"Signal flip: {tf_result.direction} @ {tf_result.confidence}% confidence")

# Điều kiện 3: Funding cực đoan
funding_rate = fetch_funding_rate()
if position_direction == LONG and funding_rate > 0.001:
    danger = True
    reasons.append(f"Funding cực đoan: {funding_rate*100:.3f}%/8h (LONG)")
if position_direction == SHORT and funding_rate < -0.001:
    danger = True
    reasons.append(f"Funding cực đoan: {funding_rate*100:.3f}%/8h (SHORT)")

# Điều kiện 4: Giữ quá lâu
open_hours = (now - position_open_time).total_seconds() / 3600
if open_hours > MAX_OPEN_HOURS:
    danger = True
    reasons.append(f"Giữ {open_hours:.1f}h > {MAX_OPEN_HOURS}h")

# Điều kiện 5: Floating loss quá lớn
pnl_pct = unrealized_pnl / account_balance * 100
if pnl_pct < -DANGER_LOSS_PCT:
    danger = True
    reasons.append(f"Floating loss {pnl_pct:.2f}% < -{DANGER_LOSS_PCT}%")
```

### 7.4 Đóng position

Khi cần đóng:
1. Huỷ tất cả algo TP/SL orders (`/trade/cancel-algos`)
2. Huỷ tất cả pending limit orders (`/trade/cancel-order`)
3. Đóng bằng market order (`/trade/close-position`)
4. Ghi vào state.json: `close_reason`, `close_price`, `realized_pnl`
5. Gửi Telegram: kết quả lãi/lỗ + balance mới

---

## 8. Pending Order Management

### 8.1 Vòng đời pending order

```
PLACED → (chờ) → FILLED     → position mở, chuyển sang IN_POSITION
               → CANCELLED   → state reset về IDLE
               → EXPIRED     → bot chủ động huỷ (timeout / price drift)
```

### 8.2 Logic kiểm tra mỗi chu kỳ

```python
def manage_pending_order(state, current_price):
    order_id = state["pending_order"]["order_id"]
    placed_at = parse_datetime(state["pending_order"]["placed_at"])
    entry_price = state["pending_order"]["entry_price"]
    
    # Lấy trạng thái thực tế từ OKX
    order = okx.get_order(ordId=order_id)
    
    if order["state"] == "filled":
        # Order đã fill → chuyển sang IN_POSITION
        activate_position(order)
        return
    
    if order["state"] == "canceled":
        # Bị huỷ bên ngoài bot (user huỷ tay trên app)
        logger.warning(f"Order {order_id} bị huỷ từ bên ngoài")
        state["pending_order"]["active"] = False
        telegram("⚠️ Pending order bị huỷ từ bên ngoài. Reset về IDLE.")
        return
    
    # Kiểm tra timeout
    hours_open = (now_utc() - placed_at).total_seconds() / 3600
    if hours_open >= PENDING_CANCEL_HOURS:
        okx.cancel_order(ordId=order_id)
        state["pending_order"]["active"] = False
        telegram(f"⏰ Huỷ limit order sau {hours_open:.1f}h chưa fill")
        return
    
    # Kiểm tra price drift
    drift_pct = abs(current_price - entry_price) / entry_price * 100
    if drift_pct >= PENDING_CANCEL_DRIFT_PCT:
        okx.cancel_order(ordId=order_id)
        state["pending_order"]["active"] = False
        telegram(f"📉 Huỷ limit order: giá đã drift {drift_pct:.2f}% khỏi entry")
        return
    
    # Vẫn đang chờ → báo cáo bình thường
    logger.info(f"Pending order {order_id}: chờ fill ({hours_open:.1f}h, drift {drift_pct:.2f}%)")
```

### 8.3 Partial fill của pending order

Nếu limit order fill một phần và phần còn lại bị huỷ:
- Xem phần đã fill là position hợp lệ
- Điều chỉnh `size_contracts` theo số contracts đã fill thực tế
- Đặt algo TP/SL ngay cho phần đã fill
- Log và Telegram thông báo partial fill

---

## 9. Circuit Breaker

### 9.1 Logic

```python
# state.json lưu daily_loss_usdt, reset mỗi ngày UTC 00:00
daily_loss_usdt = state["daily_loss_usdt"]
account_balance = fetch_balance()

if daily_loss_usdt / account_balance * 100 >= MAX_DAILY_LOSS_PCT:
    state["circuit_break"] = True
    state["circuit_break_until"] = tomorrow_midnight_utc
    telegram("🔴 Circuit breaker: đã lỗ {:.1f}% hôm nay. Dừng giao dịch đến 00:00 UTC.")
    return
```

### 9.2 Reset

Circuit breaker tự reset lúc 00:00 UTC. State:
```json
{
  "circuit_break": false,
  "circuit_break_until": null,
  "daily_loss_usdt": 0.0,
  "daily_reset_date": "2026-04-15"
}
```

---

## 10. Error Handling

### 10.1 Phân loại lỗi OKX API

Không phải mọi lỗi đều xử lý giống nhau. Phân loại rõ để tránh retry sai chỗ hoặc bỏ qua lỗi nghiêm trọng:

| HTTP / OKX Code | Loại | Hành động |
|-----------------|------|-----------|
| `429` Too Many Requests | Rate limit | Retry sau `Retry-After` giây, tối đa 3 lần |
| `5xx` Server Error | OKX tạm lỗi | Retry với exponential backoff (2s, 4s, 8s) |
| `Network timeout` | Kết nối | Retry tối đa 3 lần, sau đó skip chu kỳ + alert |
| `50102` Invalid signature | Auth lỗi | **Dừng bot ngay**, alert Telegram, không retry |
| `51000` Parameter error | Bug code | Log ERROR, skip action, alert Telegram |
| `51008` Insufficient margin | Tài khoản | Kích hoạt circuit break, alert |
| `51010` Order not exist | Order đã cancel | Cập nhật state, tiếp tục |
| `51020` Position not exist | Đã đóng | Sync state, tiếp tục |

### 10.2 Retry wrapper

```python
def okx_request_with_retry(fn, *args, max_retries=3, **kwargs):
    for attempt in range(max_retries):
        try:
            result = fn(*args, **kwargs)
            return result
        except RateLimitError as e:
            wait = e.retry_after or (2 ** attempt)
            logger.warning(f"Rate limit, chờ {wait}s (attempt {attempt+1})")
            time.sleep(wait)
        except ServerError:
            wait = 2 ** attempt
            logger.warning(f"OKX server error, retry sau {wait}s")
            time.sleep(wait)
        except NetworkTimeout:
            logger.warning(f"Timeout, retry {attempt+1}/{max_retries}")
            time.sleep(2)
        except InvalidSignatureError:
            logger.critical("Invalid API signature — dừng bot")
            telegram("🚨 API signature lỗi. Bot dừng. Kiểm tra API key ngay!")
            raise SystemExit(1)
        except InsufficientMarginError:
            logger.error("Insufficient margin")
            trigger_circuit_break("insufficient_margin")
            raise
    
    # Hết retry → skip action
    logger.error(f"Hết {max_retries} lần retry — skip action này")
    telegram(f"⚠️ OKX API không phản hồi sau {max_retries} lần thử. Skip chu kỳ.")
    raise SkipCycleError()
```

### 10.3 Nguyên tắc chung

- **Không crash bot** vì lỗi tạm thời — skip chu kỳ, alert, tiếp tục
- **Crash có kiểm soát** chỉ khi lỗi auth (invalid signature) hoặc lỗi logic nghiêm trọng không thể recover
- **Luôn log full context**: request params, response body, timestamp
- **Không log API key** — mask trước khi log

---

## 11. Startup Reconciliation

### 11.1 Vấn đề

Bot có thể crash giữa chu kỳ (OOM, network drop, SIGKILL). Khi restart:
- state.json có thể lỗi thời
- OKX có thể có position/order mà state.json không biết
- Nguy cơ: bot đặt thêm lệnh trong khi đã có position

### 11.2 Logic reconcile khi khởi động

```python
def reconcile_on_startup():
    state = load_state()
    
    # Lấy trạng thái thực tế từ OKX
    live_position = okx.get_position(instId=BOT_SYMBOL)
    live_pending  = okx.get_pending_orders(instId=BOT_SYMBOL)
    live_algos    = okx.get_algo_pending(instId=BOT_SYMBOL)
    
    # Case 1: OKX có position, state.json không biết
    if live_position and not state["position"]["active"]:
        logger.warning("Reconcile: phát hiện position không có trong state.json")
        state["position"] = build_position_from_live(live_position)
        state["position"]["active"] = True
        state["position"]["reconciled"] = True
        telegram("⚠️ Reconcile: phát hiện position đang mở, đã đồng bộ vào state.")
    
    # Case 2: state.json nghĩ có position, OKX không có
    if state["position"]["active"] and not live_position:
        logger.warning("Reconcile: state có position nhưng OKX không có — position đã đóng")
        finalize_closed_position(state)
        telegram("ℹ️ Reconcile: position trong state đã đóng trên OKX (TP/SL hit?).")
    
    # Case 3: state.json nghĩ có pending order, OKX không có
    if state["pending_order"]["active"] and not live_pending:
        logger.warning("Reconcile: pending order không còn trên OKX")
        # Kiểm tra fill history
        fills = okx.get_fills(instId=BOT_SYMBOL, limit=5)
        if any(f["ordId"] == state["pending_order"]["order_id"] for f in fills):
            logger.info("Reconcile: order đã fill — kích hoạt position")
            activate_position_from_fill(fills, state)
        else:
            logger.info("Reconcile: order bị huỷ — reset về IDLE")
            state["pending_order"]["active"] = False
    
    # Case 4: OKX có pending order, state.json không biết
    if live_pending and not state["pending_order"]["active"]:
        logger.warning("Reconcile: phát hiện pending order không có trong state.json")
        state["pending_order"] = build_pending_from_live(live_pending[0])
        telegram("⚠️ Reconcile: phát hiện pending order, đã đồng bộ vào state.")
    
    save_state(state)
    logger.info("Reconcile hoàn tất")
```

### 11.3 Atomic state write

Để tránh corrupt state.json khi crash giữa lúc ghi:

```python
def save_state(state: dict, path="state.json"):
    # Backup bản cũ
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    
    # Ghi vào file tạm trước
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2, default=str)
    
    # Atomic rename (trên cùng filesystem, rename là atomic)
    os.replace(tmp_path, path)
```

---

## 12. State File (state.json)

```json
{
  "version": 1,
  "last_run": "2026-04-15T14:00:00Z",
  "next_run": "2026-04-15T16:00:00Z",

  "position": {
    "active": true,
    "side": "long",
    "entry_price": 84000.0,
    "size_contracts": 2,
    "open_time": "2026-04-15T12:00:00Z",
    "sl_price": 82740.0,
    "tp1_price": 85680.0,
    "tp2_price": 86940.0,
    "algo_order_id": "123456789",
    "entry_order_id": "987654321",
    "reconciled": false,
    "dry_run": false
  },

  "pending_order": {
    "active": false,
    "order_id": null,
    "entry_price": null,
    "placed_at": null,
    "side": null
  },

  "circuit_break": false,
  "circuit_break_until": null,

  "daily_loss_usdt": 0.0,
  "daily_realized_pnl": 0.0,
  "daily_trades": 0,
  "daily_reset_date": "2026-04-15",

  "last_action": "placed_order",
  "last_signal": {
    "direction": 1,
    "confidence": 72,
    "net_score": 0.48,
    "source": "agent"
  },

  "bot_paused": false,
  "pending_close_confirm": false
}
```

---

## 13. Telegram Integration

### 13.1 Notifications (bot gửi)

**Chu kỳ báo cáo thường (mỗi 2h):**
```
📊 BTC-USDT-SWAP | 15:00 UTC

💰 Balance: $100.23 USDT
📈 Position: LONG 84,000 → hiện tại 84,520 (+$1.04 / +1.0%)
🎯 TP1: 85,680 | SL: 82,740

Tín hiệu: LONG 72% confidence
Funding: +0.008% (neutral)
⏱ Mở được: 3h

✅ Trạng thái: An toàn, tiếp tục giữ
```

**Đặt lệnh mới:**
```
🔔 Đặt lệnh mới

Direction : LONG
Entry     : $84,000 (limit)
TP1       : $85,680 (+2.0%)
TP2       : $86,940 (+3.5%)
SL        : $82,740 (-1.5%)
Size      : 2 contracts (risk $1.00)
Confidence: 72% | Score: 0.48

Source: agent (gemini-2.5-flash)
```

**Đóng lệnh:**
```
✅ Đóng lệnh — TP1 chạm

Entry    : $84,000
Close    : $85,680
PnL      : +$1.68 (+1.68%)
Hold     : 6h 30m
Balance  : $101.91 USDT (+1.68%)
```

**Circuit breaker:**
```
🔴 Circuit Breaker kích hoạt

Lỗ hôm nay: $3.21 (3.2% balance)
Giới hạn  : 3.0%

Bot tạm dừng đến 00:00 UTC ngày mai.
Gõ /status để kiểm tra trạng thái.
```

**Cảnh báo nguy hiểm:**
```
⚠️ Position nguy hiểm — đóng ngay

Lý do: Giá cách SL < 0.5 × ATR_1H
Entry : $84,000
Hiện  : $83,100 (-1.1%)
SL    : $82,740
ATR1H : $320

→ Đóng market order $83,080
PnL   : -$0.92 (-0.92%)
```

**NO TRADE:**
```
🔍 Phân tích xong — No Trade

Lý do: Confidence 48% < 60%
Tín hiệu: LONG nhẹ (score 0.19)
4H vs 1D: Counter-trend

→ Chờ chu kỳ tiếp theo (16:00 UTC)
```

### 13.2 Commands (user gửi cho bot)

| Lệnh | Mô tả | Yêu cầu confirm? |
|------|-------|-----------------|
| `/status` | Trạng thái hiện tại: balance, position, PnL | Không |
| `/analyze` | Chạy phân tích ngay (không đặt lệnh) | Không |
| `/close` | Đóng position ngay bằng market order | **Có** |
| `/pause` | Tạm dừng bot (không đặt lệnh mới) | Không |
| `/resume` | Tiếp tục bot sau khi pause | Không |
| `/pnl` | PnL hôm nay: realized + unrealized | Không |
| `/config` | Hiển thị config hiện tại (ẩn API keys) | Không |
| `/dryrun on\|off` | Bật/tắt dry run mode | Không |
| `/help` | Danh sách commands | Không |

### 13.3 Confirmation flow cho /close

Lệnh `/close` tác động trực tiếp đến tài khoản thật — cần 2 bước xác nhận:

```
User: /close

Bot:  ⚠️ Xác nhận đóng position?

      Side   : LONG
      Size   : 2 contracts
      Entry  : $84,000
      Hiện   : $84,520 (+$1.04)

      Gõ /close confirm để xác nhận.
      Lệnh hết hạn sau 60 giây.

User: /close confirm

Bot:  ✅ Đang đóng position...
      → Đóng $84,480 (market slippage)
      PnL: +$0.96
```

Nếu sau 60 giây không có `/close confirm` → bot bỏ qua, reset `pending_close_confirm = false`.

---

## 14. Dry Run Mode

Khi `DRY_RUN=true`:
- Tất cả OKX private API calls bị SKIP
- Thay vào đó: in/log "DRY RUN: would place order {...}"
- State vẫn được lưu (position.active = true) nhưng có flag `dry_run: true`
- Balance lấy từ `ACCOUNT_BALANCE_USDT` trong .env (không gọi API)
- Telegram vẫn hoạt động bình thường (để test notification)
- PnL được tính giả lập dựa trên giá thị trường thực
- Reconcile khi startup bị bỏ qua (không có gì để sync)

---

## 15. Docker Deployment

### 15.1 Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Không copy .env vào image — dùng env_file trong compose
CMD ["python", "bot/main.py"]
```

### 15.2 docker-compose.yml

```yaml
version: "3.9"

services:
  btc-bot:
    build: .
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - ./state.json:/app/state.json
      - ./state.json.bak:/app/state.json.bak
      - ./bot.log:/app/bot.log
    environment:
      - TZ=UTC
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

### 15.3 Chạy bot

```bash
# Build và start
docker compose up -d

# Xem log real-time
docker compose logs -f btc-bot

# Dừng bot
docker compose stop btc-bot

# Restart bot (sau khi đổi .env)
docker compose restart btc-bot

# Kiểm tra health
docker compose ps
```

### 15.4 requirements.txt (bổ sung)

```
# Existing
okx-python-sdk  # hoặc requests trực tiếp
langchain
langchain-google-genai
langgraph

# Bot additions
apscheduler>=3.10
python-telegram-bot>=20.0
python-dotenv>=1.0
```

---

## 16. Kế hoạch build (thứ tự)

### Phase 1 — OKX Private API wrapper
- `bot/okx_private.py`: get_balance, get_position, get_pending_orders, get_algo_orders, get_fills
- `bot/okx_errors.py`: error classification, retry wrapper
- Test: dry-run gọi từng endpoint, in ra kết quả
- Không làm gì write API trước

### Phase 2 — State machine + state.json
- `bot/state.py`: load_state, save_state (atomic), reset_daily
- `bot/circuit_breaker.py`: check_circuit_break, update_daily_loss
- Test: mock state transitions

### Phase 3 — Startup Reconciler
- `bot/reconciler.py`: reconcile OKX ↔ state.json khi khởi động
- Xử lý 4 case mismatch (xem Section 11.2)
- Test: mock các trường hợp crash giữa chừng

### Phase 4 — Order Manager
- `bot/order_manager.py`: place_order, set_algo_tp_sl, cancel_order, close_position
- `bot/pending_order.py`: manage_pending_order, timeout logic, drift check
- Chỉ dùng khi `DRY_RUN=false`
- Test bắt buộc với `OKX_DEMO_MODE=true` trước

### Phase 5 — Position Guard
- `bot/position_guard.py`: is_dangerous, danger_reasons, sync_position_size
- Reuse multi-TF analysis từ `commands/trade.py`
- Test: mock position data + price data
- Bao gồm xử lý partial fill

### Phase 6 — Telegram Bot
- `bot/telegram_bot.py`: send_message, setup_handlers (commands)
- `bot/report.py`: format tất cả các loại message
- Confirmation flow cho `/close` (2-step)
- Test: gửi thật vào chat ID thật

### Phase 7 — Scheduler + Main Loop
- `bot/scheduler.py`: APScheduler 2h interval
- `bot/main.py`: kết nối tất cả phases, verify leverage khi startup
- Test end-to-end với `DRY_RUN=true`, `OKX_DEMO_MODE=true`

### Phase 8 — Docker packaging
- Dockerfile + docker-compose.yml
- Test: build image, chạy 1 chu kỳ hoàn chỉnh
- Verify atomic state write trong container

---

## 17. Lưu ý quan trọng

### An toàn
- **KHÔNG bao giờ hardcode** API key trong code
- Validate tất cả OKX API response trước khi dùng
- Nếu OKX API trả lỗi → phân loại lỗi, retry có kiểm soát, sau đó skip (không crash)
- Circuit breaker là hàng rào cuối cùng — đừng bypass
- `/close` command bắt buộc có confirmation 2 bước

### Idempotent
- Mỗi chu kỳ phải idempotent: chạy 2 lần không gây đặt 2 lệnh
- Luôn check state.json + OKX live state trước khi action
- Nếu state.json và OKX không đồng bộ → ưu tiên OKX (source of truth)
- Reconcile bắt buộc mỗi lần khởi động

### Leverage
- Bot kiểm tra leverage OKX khi startup và tự điều chỉnh nếu lệch với config
- Không bao giờ đặt lệnh khi chưa confirm leverage đúng

### Logging
- Log mọi action với timestamp UTC
- Log request/response của mọi OKX API call (ẩn keys)
- Log level: INFO cho actions bình thường, WARNING cho bất thường, ERROR cho lỗi
- Docker logging giới hạn 10MB × 3 files để tránh đầy disk

### Dry Run First
- **Chạy dry_run=true ít nhất 24h trước khi bật lệnh thật**
- Kiểm tra xem Telegram notifications có đến đúng không
- Kiểm tra logic circuit breaker qua state.json
- Xác nhận analysis output hợp lý trước khi tin tưởng bot

---

*Cập nhật lần cuối: 2026-04-16*  
*Trạng thái: Design document — chưa implement*
