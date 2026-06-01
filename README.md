# 104 企業大師自動打卡

幫你週一到週五自動上下班打卡，遇到台灣國定假日或你有請假會自己跳過。
純 Python stdlib 寫的，零套件依賴，clone 下來就能跑。

## 安裝

```bash
cd checkin-bot
bash setup.sh
```

就這樣，剩下它會問你 104 帳號密碼，然後全自動：測登入 → 看公司有沒有開裝置綁定 → 把 crontab 裝好。
之後想改設定，改完再跑一次 `bash setup.sh` 就好，可以重複跑，它只會動自己那段 cron。

> macOS 的話 cron 要能跑，得先去開權限：
> 系統設定 → 隱私權與安全性 → 完整磁碟取用權 → 把 `/usr/sbin/cron` 加進去。

## 設定（config/config.json）

| 欄位 | 說明 |
|---|---|
| `username` / `password` | 你的 104 帳密 |
| `latitude` / `longitude` | 打卡的座標，預設是公司辦公室 |
| `checkin_time` / `checkout_time` | 上下班時間，**填台北時間就對了**。cron 只負責叫醒程式，真正判斷都在 Python 裡做 |
| `auto_checkin_deadline` | 上班卡最晚自動打卡時間，預設 `08:59`，避免 9 點後才補成遲到卡 |
| `auto_window_minutes` | 自動模式補打窗口；下班卡會從 `checkout_time` 起算這麼多分鐘內補打 |
| `deviceId` | 留空就好，第一次跑會自動生一個 |
| `telegram_bot_token` / `telegram_chat_id` | 想要打卡完收 Telegram 通知再填，兩個都填才會發 |

## 平常會用到的指令

```bash
python3 checkin.py            # 打卡，會自己判斷現在是上班還下班
python3 checkin.py --auto     # 給 cron 用：只在台北時間窗口內打卡，並避免同一天重複打
python3 checkin.py --force    # 不管假日週末，硬打一筆
python3 checkin.py --jitter N # 打卡前隨機等 0~N 秒，比較不像機器人
python3 checkin.py --check    # 只看公司裝置綁定政策，不打卡
python3 checkin.py --off      # 暫停自動打卡（cron 照跑但會直接跳過）
python3 checkin.py --on       # 恢復自動打卡
python3 checkin.py --status   # 看現在是開還是關
tail -f logs/checkin.log      # 看它跑得怎樣
```

## 幾個要注意的點

- **時區**：config 填的是台北時間。cron 現在只會每 5 分鐘喚醒一次，`checkin.py --auto` 會自己用 `Asia/Taipei` 判斷現在是不是該打卡，所以電腦時區改掉也不需要重算 crontab。
- **上班卡不補遲到**：預設只會在 `checkin_time` 到 `auto_checkin_deadline` 之間打上班卡，`auto_checkin_deadline` 預設是 `08:59`。
- **deviceId 綁定**：大部分公司沒開綁定，自動生的 UUID 直接能用，不用管。
  如果公司有開、而且你已經用 iPhone 打過卡了，`--check` 會跟你說 `wrong_device`，這時候才需要用 Proxyman 把 iPhone 的 deviceId 撈出來填進 config。
- **假日**：用 [`ruyut/TaiwanCalendar`](https://github.com/ruyut/TaiwanCalendar) 的資料（連補班補假都算進去）。
  萬一抓不到（沒網路之類），就退一步只看是不是週末；那種情況國定假日要自己手動關 cron。
- **安全**：`config/config.json` 裡是明文密碼（已經 chmod 600 了），別 commit、別亂傳給人。
  哪天改了密碼，記得回來改這裡。
- **請假**：每次打卡前會去 104 查當天有沒有核准的假，有就跳過、發 Telegram 通知，你不用做任何事。
  半天假也判得對——它讀請假的起訖時刻，只跳落在區間內的那一筆。早上請假就只跳上班卡、傍晚照打下班卡，反之亦然。
  查請假這支萬一連不上，不會擋打卡，就跟沒這功能一樣照常打。
- 加班、調班那些還是得自己進 App 用。
