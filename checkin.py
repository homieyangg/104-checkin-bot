#!/usr/bin/env python3
"""104 企業大師自動打卡。

登入拿 token、POST 打卡，遇到台灣假日就自動跳過。
登入要走三段（user token → 換 cookie → prohrm token），有點繞，但 HAR 攔出來就長這樣，照做就對了。
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import gzip
import http.cookiejar
import io
import json
import logging
import pathlib
import random
import sys
import time
import urllib.error
import urllib.request
import uuid
import zoneinfo

# getCardSetting 這支是吃 JSESSIONID、不是 Bearer，
# 所以得跟 /token/cookies 拿到的 cookie 共用同一個 jar，不然會被擋掉。
_COOKIE_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(_COOKIE_JAR)
)

TZ_TAIPEI = zoneinfo.ZoneInfo("Asia/Taipei")

ROOT = pathlib.Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_PATH = DATA_DIR / "state.json"
HOLIDAY_CACHE = DATA_DIR / "holidays_cache.json"
LOG_PATH = LOG_DIR / "checkin.log"

BASE = "https://pro.104.com.tw"
USER_TOKEN_URL = f"{BASE}/user/api/token"
USER_COOKIES_URL = f"{BASE}/user/api/token/cookies"
PROHRM_TOKEN_URL = f"{BASE}/prohrm/api/login/token"
CARD_GPS_URL = f"{BASE}/prohrm/api/app/card/gps"
CARD_SETTING_URL = f"{BASE}/hrm/psc/apis/public/getCardSetting.action"

# 下面這幾個是從 HAR 抓出來的固定值：104 App 內建的 OAuth client 帳密跟 app token。
# 不是你的帳密，是 App 自己的，不會變，照抄就好。
BASIC_AUTH = (
    "Basic ZjU1MGUwNDUtOTljNS01MjJiLWI4YzQtYjVmMTk0OWNhYTVkOjc5MThhZGU2"
    "MGY3MWU4NmJjYjM4MDVmYTUzNDViMzM1YjUwOGY4Y2M="
)
APP_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzb3VyY2UiOiJhcHAtcHJvZCIsImNpZCI6MCwiaWF0IjoxNTUzNzUzMTQwfQ."
    "ieJiJtNsseSO5fxNH1XTa6bqHZ0zUyoPVUYPNtOj4TM"
)
USER_AGENT = (
    "104%E4%BC%81%E6%A5%AD%E5%A4%A7%E5%B8%AB/253250002 "
    "CFNetwork/3860.600.12 Darwin/25.5.0"
)

TAIWAN_CALENDAR_URL = (
    "https://cdn.jsdelivr.net/gh/ruyut/TaiwanCalendar/data/{year}.json"
)


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"找不到設定檔 {CONFIG_PATH}。請複製 config/config.example.json 為 config/config.json 後填值。"
        )
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    # deviceId 沒填的話就自動生一個 UUID 寫回去，之後都固定用這個。
    if not cfg.get("deviceId"):
        new_id = str(uuid.uuid4()).upper()
        cfg["deviceId"] = new_id
        save_config(cfg)
        logging.info("config 沒有 deviceId，已自動生成並寫回: %s", new_id)
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    try:
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass


def _parse_hhmm(s: str) -> dt.time:
    hh, mm = s.strip().split(":")
    return dt.time(int(hh), int(mm))


def auto_label(config: dict, now: dt.datetime) -> str:
    """看現在台北時間離上班還是下班比較近，順便決定要標 in 還是 out。"""
    ci = config.get("checkin_time")
    co = config.get("checkout_time")
    if not ci or not co:
        return ""
    today = now.date()
    ci_dt = dt.datetime.combine(today, _parse_hhmm(ci), tzinfo=now.tzinfo)
    co_dt = dt.datetime.combine(today, _parse_hhmm(co), tzinfo=now.tzinfo)
    return "in" if abs((now - ci_dt).total_seconds()) <= abs((now - co_dt).total_seconds()) else "out"


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def http_request(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    body: object = None,
    timeout: int = 30,
) -> tuple[int, str]:
    """送個 request，回 (status, text)。server 回 gzip 的話會自己解掉。"""
    data: bytes | None = None
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
        elif isinstance(body, bytes):
            data = body
        else:
            data = str(body).encode("utf-8")
    req_headers = dict(headers or {})
    req_headers.setdefault("Accept-Encoding", "gzip")
    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    with _OPENER.open(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding", "").lower() == "gzip":
            raw = gzip.decompress(raw)
        return resp.status, raw.decode("utf-8", errors="replace")


def _maybe_decode(text: str) -> dict:
    """response 可能是純 JSON、也可能被 base64 包過，兩種都吃。"""
    text = text.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(base64.b64decode(text).decode("utf-8"))


def _jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    pad = "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(parts[1] + pad).decode())


def _jwt_expire_ts(token: str) -> int:
    """從 JWT 裡撈 expire（秒）。撈不到就先當作 50 分鐘後過期。"""
    payload = _jwt_payload(token)
    expire = payload.get("expire")
    if isinstance(expire, (int, float)):
        # prohrm 這個 expire 是毫秒不是秒，太大的話要除 1000。
        return int(expire) // 1000 if expire > 10**12 else int(expire)
    exp = payload.get("exp")
    if isinstance(exp, (int, float)):
        return int(exp)
    return int(time.time()) + 3000


def full_login(username: str, password: str) -> dict:
    """跑完整三段登入，回一包 {access, refresh, expire_ts, uno, pid, cid}。"""
    logging.info("執行完整登入流程")

    # 第一段：拿 user token。
    headers = {
        "Host": "pro.104.com.tw",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "Authorization": BASIC_AUTH,
        "Cache-Control": "no-cache",
        "Accept-Language": "zh-TW,zh-Hant;q=0.9",
    }
    body = {"grant_type": "password", "password": password, "username": username}
    _, text = http_request(USER_TOKEN_URL, "POST", headers, body)
    payload = _maybe_decode(text)
    if payload.get("code") != 200 or "access_token" not in payload:
        raise RuntimeError(f"/user/api/token 失敗: {payload}")
    user_access = payload["access_token"]
    user = payload.get("user", {})
    pid = user.get("pid")
    cid = user.get("cid")
    companies = user.get("companies") or {}
    uno = ""
    if companies:
        first = companies[next(iter(companies))]
        uno = first.get("invoice", "")
    logging.info("使用者 token OK (pid=%s cid=%s uno=%s)", pid, cid, uno)

    # 第二段：拿 user token 去換 cookie，沒 body，成功是回 204。
    headers_b = {
        "Host": "pro.104.com.tw",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {user_access}",
        "Accept-Language": "zh-TW,zh-Hant;q=0.9",
    }
    try:
        http_request(USER_COOKIES_URL, "POST", headers_b, b"")
    except urllib.error.HTTPError as e:
        # 正常 204 不會進這裡。真的錯了也不致命，繼續往下試 prohrm token 就好。
        logging.warning("/user/api/token/cookies 警告: %s", e)

    # 第三段：用 cookie + 內建 app token 換 prohrm 的 token，這個才是打卡要用的。
    headers_c = {
        "Host": "pro.104.com.tw",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-TW,zh-Hant;q=0.9",
        "Cache-Control": "no-cache",
    }
    body_c = {"uno": uno, "acc": username, "pwd": password, "token": APP_TOKEN}
    _, text = http_request(PROHRM_TOKEN_URL, "POST", headers_c, body_c)
    payload = _maybe_decode(text)
    if payload.get("code") != 200 or "data" not in payload:
        raise RuntimeError(f"/prohrm/api/login/token 失敗: {payload}")
    data = payload["data"]
    access = data["access"]
    refresh = data["refresh"]
    expire_ts = _jwt_expire_ts(access)
    logging.info(
        "Prohrm token OK (expire=%s)",
        dt.datetime.fromtimestamp(expire_ts).isoformat(),
    )
    return {
        "access": access,
        "refresh": refresh,
        "expire_ts": expire_ts,
        "uno": uno,
        "pid": pid,
        "cid": cid,
    }


def ensure_token(config: dict, state: dict, force: bool = False) -> str:
    """回一個還能用的 prohrm token。過期或根本沒有就重登一次再寫回 state。"""
    now = int(time.time())
    if (
        not force
        and state.get("access")
        and state.get("expire_ts", 0) - 60 > now
    ):
        return state["access"]
    info = full_login(config["username"], config["password"])
    state.update(info)
    save_state(state)
    return info["access"]


def check_card_setting(config: dict, cid: str, pid: str) -> dict:
    """查公司有沒有開裝置綁定，回 data[0]（裡面有 mappingDevice / haveDeviceId / bindDevice）。

    這支也是吃 JSESSIONID，所以一定要在同個 process 先跑過 full_login() 再叫它，不然沒 cookie。
    """
    headers = {
        "Host": "pro.104.com.tw",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-TW,zh-Hant;q=0.9",
        "Cache-Control": "no-cache",
    }
    body = {"cid": str(cid), "pid": str(pid), "deviceId": config["deviceId"]}
    _, text = http_request(CARD_SETTING_URL, "POST", headers, body)
    payload = _maybe_decode(text)
    if not payload.get("success"):
        raise RuntimeError(f"getCardSetting 失敗: {payload}")
    data = payload.get("data") or []
    if not data:
        raise RuntimeError(f"getCardSetting 回應沒 data: {payload}")
    return data[0]


def interpret_card_setting(setting: dict) -> tuple[str, str]:
    """把 getCardSetting 的結果翻成 (verdict, msg)。

    verdict 有四種：safe / will_bind / wrong_device / unknown，setup.sh 會照這個分流。
    """
    mapping = setting.get("mappingDevice", False)
    have = setting.get("haveDeviceId", False)
    bind = setting.get("bindDevice", False)
    if not mapping:
        return "safe", "公司沒開裝置綁定，隨便哪個 deviceId 都能打，自動生的 UUID 就很夠用了。"
    if mapping and not have:
        return (
            "will_bind",
            "公司有開裝置綁定，但你帳號現在還沒綁任何裝置。"
            "下次一打卡，就會把現在 config 裡這個 deviceId 永久綁到你帳號上，"
            "以後就只認這支了，要想清楚。",
        )
    if mapping and have and bind:
        return "safe", "公司有開綁定，不過現在這個 deviceId 剛好就是你綁的那支，放心用。"
    if mapping and have and not bind:
        return (
            "wrong_device",
            "公司有開綁定，而且你帳號綁的是另一支 deviceId（多半是你的 iPhone）。"
            "用現在這個 deviceId 打會被擋下來。"
            "你得用 Proxyman 攔 iPhone 上的 104 App，把原本那個 deviceId 撈出來填進 config/config.json。",
        )
    return "unknown", f"判不出來: mappingDevice={mapping} haveDeviceId={have} bindDevice={bind}"


def _format_card_time_taipei(card_time_str: str) -> str:
    """server 回的時間是 UTC，轉成台北時間給人看比較直覺。"""
    if not card_time_str:
        return ""
    try:
        # Python 3.9 的 fromisoformat 不認 Z 結尾，先換成 +00:00 它才讀得懂。
        iso = card_time_str.replace("Z", "+00:00")
        utc_dt = dt.datetime.fromisoformat(iso)
        return utc_dt.astimezone(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return card_time_str


def send_telegram(config: dict, text: str) -> None:
    """發個 Telegram 通知。bot_token 或 chat_id 沒填就直接跳過，不會擋到打卡。"""
    token = str(config.get("telegram_bot_token") or "").strip()
    chat_id = str(config.get("telegram_chat_id") or "").strip()
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    headers = {"Content-Type": "application/json"}
    try:
        http_request(url, "POST", headers, body, timeout=10)
        logging.info("Telegram 通知已發送")
    except Exception as e:
        # 通知掛了就算了，反正不影響打卡，記個 log 就好。
        logging.warning("Telegram 通知失敗（不影響打卡）: %s", e)


def punch(config: dict, access_token: str) -> tuple[int, dict]:
    now = dt.datetime.now(dt.timezone.utc)
    iso_now = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    body = {
        "deviceId": config["deviceId"],
        "latitude": float(config["latitude"]),
        "longitude": float(config["longitude"]),
        "callApiTime": int(time.time() * 1000),
        "punchTimeISO": iso_now,
    }
    headers = {
        "Host": "pro.104.com.tw",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {access_token}",
        "Accept-Language": "zh-TW,zh-Hant;q=0.9",
    }
    status, text = http_request(CARD_GPS_URL, "POST", headers, body)
    return status, _maybe_decode(text)


def fetch_taiwan_calendar(year: int) -> list[dict]:
    req = urllib.request.Request(
        TAIWAN_CALENDAR_URL.format(year=year),
        headers={"User-Agent": "checkin-bot/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding", "").lower() == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))


def is_taiwan_holiday(date: dt.date) -> bool:
    """查台灣行事曆（含補班補假）。抓不到資料（沒網路之類）就退一步看是不是週末。"""
    year = date.year
    cache: dict = {}
    if HOLIDAY_CACHE.exists():
        try:
            cache = json.loads(HOLIDAY_CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}
    if str(year) not in cache:
        try:
            cache[str(year)] = fetch_taiwan_calendar(year)
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            HOLIDAY_CACHE.write_text(
                json.dumps(cache, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logging.warning("抓不到 %s 年行事曆 (%s)，退回用週末判斷", year, e)
            return date.weekday() >= 5
    target = date.strftime("%Y%m%d")
    for item in cache[str(year)]:
        if item.get("date") == target:
            return bool(item.get("isHoliday"))
    # 行事曆裡找不到這天就當上班日。其實不太會發生，它整年的日期都有。
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="104 企業大師自動打卡")
    parser.add_argument(
        "--force", action="store_true", help="不管假日週末，硬打一筆"
    )
    parser.add_argument(
        "--jitter",
        type=int,
        default=0,
        help="打卡前隨機等 0~N 秒，免得每天分秒不差太像機器人",
    )
    parser.add_argument(
        "--label",
        default="",
        help="log 標籤 in/out。不給的話就照 config 的上下班時間自動判斷",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="不打卡，只登入 + 查裝置綁定政策然後結束",
    )
    args = parser.parse_args()

    setup_logging()
    now_tpe = dt.datetime.now(TZ_TAIPEI)
    today = now_tpe.date()

    try:
        config = load_config()
    except Exception as e:
        logging.error("讀取設定檔失敗: %s", e)
        return 1

    if args.check:
        state = load_state()
        try:
            # 這裡一定要現場 full_login 補 cookie，不能拿 state 裡 cache 的 token，不然沒 cookie 查不了。
            info = full_login(config["username"], config["password"])
            state.update(info)
            save_state(state)
            setting = check_card_setting(config, info["cid"], info["pid"])
        except Exception as e:
            logging.error("check 失敗: %s", e)
            return 1
        verdict, msg = interpret_card_setting(setting)
        print("=" * 60)
        print(f"deviceId      : {config['deviceId']}")
        print(f"mappingDevice : {setting.get('mappingDevice')}")
        print(f"haveDeviceId  : {setting.get('haveDeviceId')}")
        print(f"bindDevice    : {setting.get('bindDevice')}")
        print(f"verdict       : {verdict}")
        print("-" * 60)
        print(msg)
        print("=" * 60)
        # 把 verdict 印到 stdout，setup.sh 會抓這行來分流。
        print(f"CHECK_VERDICT={verdict}")
        return 0 if verdict in ("safe", "will_bind") else 2

    label = args.label or auto_label(config, now_tpe)
    tag = f"[{label}] " if label else ""

    if not args.force and is_taiwan_holiday(today):
        logging.info("%s今天 %s 是假日或週末，跳過打卡", tag, today.isoformat())
        return 0

    if args.jitter > 0:
        delay = random.randint(0, args.jitter)
        logging.info("%s延遲 %d 秒後再打卡", tag, delay)
        time.sleep(delay)

    state = load_state()
    try:
        token = ensure_token(config, state)
    except Exception as e:
        logging.error("%s登入失敗: %s", tag, e)
        send_telegram(config, f"❌ <b>104 打卡失敗</b> {tag}\n登入失敗: {e}")
        return 1

    try:
        status, payload = punch(config, token)
    except urllib.error.HTTPError as e:
        # token 有可能被 server 端作廢了，那就強制重登一次再打一次。
        logging.warning("%s打卡 HTTPError %s，強制重新登入", tag, e)
        try:
            token = ensure_token(config, state, force=True)
            status, payload = punch(config, token)
        except Exception as e2:
            logging.error("%s重新登入後仍失敗: %s", tag, e2)
            send_telegram(config, f"❌ <b>104 打卡失敗</b> {tag}\n重新登入後仍失敗: {e2}")
            return 1
    except Exception as e:
        logging.error("%s打卡失敗: %s", tag, e)
        send_telegram(config, f"❌ <b>104 打卡失敗</b> {tag}\n{e}")
        return 1

    if payload.get("code") == 200:
        data = payload.get("data", {}) or {}
        card_time = data.get("cardTime")
        logging.info(
            "%s打卡成功 cardTime=%s timeStart=%s timeEnd=%s",
            tag,
            card_time,
            data.get("timeStart"),
            data.get("timeEnd"),
        )
        local_now = now_tpe.strftime("%Y-%m-%d %H:%M:%S")
        server_local = _format_card_time_taipei(card_time)
        send_telegram(
            config,
            f"✅ <b>104 打卡成功</b> {tag}\n"
            f"本機時間: {local_now}\n"
            f"server 時間: {server_local}\n"
            f"班表: {data.get('timeStart')} - {data.get('timeEnd')}",
        )
        return 0
    logging.error("%s打卡回應錯誤 status=%s payload=%s", tag, status, payload)
    send_telegram(
        config,
        f"❌ <b>104 打卡失敗</b> {tag}\nstatus={status}\npayload={payload}",
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
