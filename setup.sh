#!/bin/bash
# 一鍵把 104 自動打卡裝起來。
# 流程大概是：檢查環境 → 填帳密 → 測一下登入 → 看公司有沒有開裝置綁定 → 裝 crontab。
# 可以重複跑，每次只會動自己 marker 包起來那段，不會弄到你其他的 cron，放心。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

MARK_BEGIN="# >>> checkin-bot (managed by setup.sh) >>>"
MARK_END="# <<< checkin-bot <<<"

if [ -t 1 ]; then
  C_RED='\033[0;31m'; C_GRN='\033[0;32m'; C_YEL='\033[1;33m'; C_BLU='\033[0;34m'; C_OFF='\033[0m'
else
  C_RED=''; C_GRN=''; C_YEL=''; C_BLU=''; C_OFF=''
fi

step() { printf "\n${C_BLU}▶${C_OFF} %s\n" "$*"; }
ok()   { printf "${C_GRN}✓${C_OFF} %s\n" "$*"; }
warn() { printf "${C_YEL}⚠${C_OFF} %s\n" "$*"; }
err()  { printf "${C_RED}✗${C_OFF} %s\n" "$*" >&2; }
ask()  {
  local prompt="$1" default="${2:-N}" ans
  read -r -p "$prompt [y/N] " ans || true
  ans="${ans:-$default}"
  [[ "$ans" =~ ^[Yy]$ ]]
}

# ---------- 1. Python version ----------
step "檢查 Python"
if ! command -v python3 >/dev/null 2>&1; then
  err "找不到 python3"
  exit 1
fi
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')
PY_OK=$(python3 -c 'import sys; print(int(sys.version_info >= (3, 9)))')
if [ "$PY_OK" != "1" ]; then
  err "需要 Python 3.9+（zoneinfo），目前 $PY_VER"
  exit 1
fi
ok "Python $PY_VER"

# ---------- 2. config.json ----------
step "檢查 config/config.json"
mkdir -p config data logs
if [ ! -f config/config.json ]; then
  if [ ! -f config/config.example.json ]; then
    err "找不到 config/config.example.json，無法初始化"
    exit 1
  fi
  cp config/config.example.json config/config.json
  ok "已從範本建立 config/config.json"
else
  ok "config/config.json 已存在（不覆蓋）"
fi
chmod 600 config/config.json
ok "config/config.json chmod 600"

# 還是範本裡的 placeholder 的話，就直接問他 email + 密碼，免得還要自己開檔案改。
NEEDS_INPUT=$(python3 - <<'PY'
import json
cfg = json.load(open("config/config.json", encoding="utf-8"))
u = cfg.get("username", "")
p = cfg.get("password", "")
if (not u) or u == "your-email@example.com" or (not p) or p == "your-password":
    print("yes")
PY
)
if [ "$NEEDS_INPUT" = "yes" ]; then
  echo
  echo "  📝 第一次設定，請填入你的 104 帳號資訊（不會上傳，只存在 config/config.json）"
  read -r -p "    104 email: " NEW_EMAIL
  # -s 是讓密碼不要顯示在螢幕上，後面補個 echo 換行才不會擠在一起。
  read -r -s -p "    104 密碼: " NEW_PWD
  echo
  NEW_EMAIL="$NEW_EMAIL" NEW_PWD="$NEW_PWD" python3 - <<'PY'
import json, os
cfg = json.load(open("config/config.json", encoding="utf-8"))
cfg["username"] = os.environ["NEW_EMAIL"].strip()
cfg["password"] = os.environ["NEW_PWD"]
with open("config/config.json", "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write("\n")
PY
  ok "已寫入 config/config.json"
fi

# 檢查欄位有沒有填齊，順便把上下班時間撈出來。
# config 裡的時間一律當台北時間；cron 只負責定期喚醒，真正的時區判斷交給 checkin.py --auto。
CFG_JSON=$(python3 - <<'PY'
import json, sys, datetime as dt
cfg = json.load(open("config/config.json", encoding="utf-8"))
need = ["username", "password", "latitude", "longitude", "checkin_time", "checkout_time"]
missing = [k for k in need if not cfg.get(k) and cfg.get(k) != 0]
if missing:
    print("MISSING:" + ",".join(missing))
    sys.exit(1)
def parse_time(name, fallback=None):
    hhmm = str(cfg.get(name) or fallback or "")
    try:
        h, m = (int(x) for x in hhmm.split(":"))
        dt.time(h, m)
    except Exception:
        print("BADTIME:" + name)
        sys.exit(1)
    return hhmm

def parse_int(name, default):
    try:
        value = int(cfg.get(name, default))
    except (TypeError, ValueError):
        print("BADINT:" + name)
        sys.exit(1)
    if value < 1:
        print("BADINT:" + name)
        sys.exit(1)
    return value

ci = parse_time("checkin_time")
co = parse_time("checkout_time")
deadline = parse_time("auto_checkin_deadline", "08:59")
window = parse_int("auto_window_minutes", 30)
h, m = (int(x) for x in deadline.split(":"))
before_nine = "1" if dt.time(h, m) < dt.time(9, 0) else "0"
print("|".join([ci, co, deadline, str(window), before_nine]))
PY
) || {
  case "$CFG_JSON" in
    MISSING:*) err "config.json 欄位不完整：${CFG_JSON#MISSING:}" ;;
    BADTIME:*) err "config.json 時間格式錯誤：${CFG_JSON#BADTIME:}，請用 HH:MM" ;;
    BADINT:*) err "config.json 數字欄位錯誤：${CFG_JSON#BADINT:}" ;;
    *) err "config.json 檢查失敗：$CFG_JSON" ;;
  esac
  exit 1
}

IFS='|' read -r CI_TIME CO_TIME CI_DEADLINE AUTO_WINDOW DEADLINE_BEFORE_NINE <<< "$CFG_JSON"
ok "打卡時間（台北）：上班 ${CI_TIME}（最晚 ${CI_DEADLINE}）　下班 ${CO_TIME}"
ok "自動模式窗口：上班卡只跑到 ${CI_DEADLINE}；下班卡從 ${CO_TIME} 起 ${AUTO_WINDOW} 分鐘內可補打"
if [ "$DEADLINE_BEFORE_NINE" != "1" ]; then
  warn "auto_checkin_deadline 不是 9 點前，這樣可能會變成遲到卡；建議設成 08:59 或更早。"
fi

# ---------- 3. Smoke test login + deviceId check ----------
step "測試登入流程（不會打卡）"
if python3 - <<'PY'
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(".").resolve()))
import checkin
cfg = checkin.load_config()   # 沒填 deviceId 的話，這一步會自動生一個寫回 config.json
info = checkin.full_login(cfg["username"], cfg["password"])
import datetime as dt
print(f"  pid={info['pid']} cid={info['cid']} uno={info['uno']}")
print(f"  deviceId = {cfg['deviceId']}")
print(f"  token expire = {dt.datetime.fromtimestamp(info['expire_ts']).isoformat()}")
state = checkin.load_state()
state.update(info)
checkin.save_state(state)
PY
then
  ok "登入成功，prohrm token 已快取到 data/state.json"
else
  err "登入失敗，請檢查帳密或網路"
  exit 1
fi

# ---------- 3b. getCardSetting 自動判斷裝置綁定 ----------
step "檢查公司裝置綁定政策（getCardSetting）"
CHECK_OUT=$(python3 checkin.py --check 2>&1)
CHECK_RC=$?
# 把 logging 那幾行濾掉，只留 --check 自己印的那塊，畫面乾淨一點。
printf '%s\n' "$CHECK_OUT" | grep -v '^[0-9].*\[INFO\]' | grep -v '^CHECK_VERDICT=' | sed 's/^/  /'
VERDICT=$(printf '%s\n' "$CHECK_OUT" | awk -F= '/^CHECK_VERDICT=/{print $2}')

case "$VERDICT" in
  safe)
    ok "裝置綁定檢查通過（safe）"
    ;;
  will_bind)
    warn "首次打卡會把目前 deviceId 永久綁到你的帳號"
    if ! ask "確定要用目前 deviceId 繼續？（綁了 iPhone App 就再也打不了）"; then
      err "已中止。請改填你 iPhone 的 deviceId 後重跑（看 README 的「deviceId 取得方式」）"
      exit 1
    fi
    ;;
  wrong_device)
    err "目前 deviceId 不是你帳號已綁的那台，打卡會被擋下"
    err "請用 Proxyman 抓你 iPhone 上的 deviceId 替換到 config/config.json 後重跑"
    exit 1
    ;;
  *)
    if [ "$CHECK_RC" != "0" ]; then
      err "--check 失敗，退出"
      exit 1
    fi
    warn "無法解析裝置綁定狀態，繼續但請手動確認"
    ;;
esac

# ---------- 4. Crontab ----------
step "設定 crontab"
CRON_BLOCK=$(cat <<EOF
$MARK_BEGIN
# 104 企業大師自動打卡 — config 的時間都是台北時間
# cron 每 5 分鐘叫醒一次；checkin.py --auto 會判斷台北時間窗口、假日請假和同日防重複。
*/5 * * * * cd $ROOT && /usr/bin/python3 checkin.py --auto --jitter 60 >> logs/checkin.log 2>&1
$MARK_END
EOF
)

EXISTING=$(crontab -l 2>/dev/null || true)
# 第一步：把上次自己裝的那段（marker 包起來的）整段砍掉，別人的 cron 不碰。
CLEANED=$(printf '%s\n' "$EXISTING" | awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
  $0 == b {skip=1; next}
  $0 == e {skip=0; next}
  !skip
')

# 第二步：再把「沒被 marker 包到」的本專案殘留行也清掉（你手動加的、或舊版裝的），
#         不然會跟新的並存，變成一天打兩次卡。用本專案絕對路徑當特徵，不會誤砍到別專案的 checkin.py。
CRON_SIG="cd $ROOT && /usr/bin/python3 checkin.py"
STRAY=$(printf '%s\n' "$CLEANED" | grep -F "$CRON_SIG" || true)
if [ -n "$STRAY" ]; then
  warn "crontab 裡有沒被管理到的本專案打卡行（這樣會重複打卡），順手幫你清掉："
  printf '%s\n' "$STRAY" | sed 's/^/    /'
  CLEANED=$(printf '%s\n' "$CLEANED" | grep -Fv "$CRON_SIG" || true)
fi

NEW_CRON="$(printf '%s\n%s\n' "$CLEANED" "$CRON_BLOCK" | awk 'NF || p {print; p=1}' )"

echo "----- 即將寫入的 crontab block -----"
printf '%s\n' "$CRON_BLOCK"
echo "------------------------------------"

if ask "確認套用到 crontab？"; then
  printf '%s\n' "$NEW_CRON" | crontab -
  ok "crontab 已更新（保留你原有的其他 entry）"
  echo
  echo "目前完整 crontab："
  crontab -l | sed 's/^/    /'
else
  warn "略過 crontab 寫入"
fi

# ---------- 5. macOS reminders ----------
step "其他事項"
echo "  ◦ macOS 需要授權 cron 完整磁碟取用權，否則 cron 跑不起來："
echo "    系統設定 → 隱私權與安全性 → 完整磁碟取用權 → 加入 /usr/sbin/cron"
echo
echo "  ◦ 想立刻試一次（會真的記一筆打卡）："
echo "    cd $ROOT && /usr/bin/python3 checkin.py --force"
echo
echo "  ◦ 查 log："
echo "    tail -f $ROOT/logs/checkin.log"
echo
echo "  ◦ 中止排程："
echo "    crontab -r       # 整份砍掉（包含其他 cron entry）"
echo "    或編輯 crontab -e 把 $MARK_BEGIN ~ $MARK_END 之間刪掉"
echo

ok "完成"
