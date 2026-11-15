#!/usr/bin/env python3
"""
米游社扫码登录工具 —— 重新获取 SToken 并写入 GitHub Secret
=========================================================

【什么时候需要跑这个脚本？】

    GitHub Actions 日志里出现下面任意一条：

        ERROR  签到失败，你的 cookie 可能已过期，请重新设置 cookie
        ERROR  无 Stoken 请手动填入 stoken
        ERROR  账号 Stoken 有问题

    说明登录凭据失效了。跑一遍本脚本即可恢复，全程约 30 秒。

【为什么需要它？】

    米游社 BBS 的签到接口 signIn 使用「强认证」，只认 SToken（App 通道令牌）。
    网页登录（浏览器 F12 复制 cookie）只能拿到 ltoken_v2 / cookie_token_v2，
    不含 SToken，表现就是「能查角色、签到却报 -100」。

    本脚本走米游社 Passport 的 App 通道扫码登录，确认后直接签发 SToken，
    再用 SToken 换取旧版 cookie_token（signIn 真正需要的那个），
    组装成完整 cookie 写入配置与 GitHub Secret。

    不需要安卓模拟器，不需要手机抓包。

【用法】

    # 1. 装依赖
    pip install pyyaml
    pip install qrcode pillow        # 可选，仅用于生成二维码图片

    # 2. 准备 PAT（需 Secrets: Read and write 权限）
    export GITHUB_PAT=github_pat_xxxxxxxx

    # 3. 运行
    python tools/miyoushe_qr_login.py --repo <用户名>/MihoyoAutoScript

    # 只取凭据、不写 secret（本地调试用）
    python tools/miyoushe_qr_login.py --no-upload --output /tmp/config.yaml

【安全说明】

    - 本文件不含任何密钥，可安全提交到公开仓库
    - 生成的 config/config.yaml 含真实 cookie，已被 .gitignore 忽略（config/*.yaml）
    - 终端输出只打印 token 长度，不打印内容
"""

import argparse
import base64
import json
import os
import re
import secrets
import sys
import time
import uuid
import urllib.error
import urllib.request

# ---- 米游社 Passport App 通道参数（公开值，非密钥）----
APP_ID = "bll8iq97cem8"
APP_VERSION = "2.90.1"
CLIENT_TYPE = "3"
USER_AGENT = f"Mozilla/5.0 miHoYoBBS/{APP_VERSION} Capture/2.2.0"

CREATE_URL = "https://passport-api.mihoyo.com/account/ma-cn-passport/app/createQRLogin"
QUERY_URL = "https://passport-api.mihoyo.com/account/ma-cn-passport/app/queryQRLoginStatus"
COOKIE_TOKEN_URL = "https://passport-api.mihoyo.com/account/auth/api/getCookieAccountInfoBySToken"
LTOKEN_URL = "https://passport-api.mihoyo.com/account/auth/api/getLTokenBySToken"

DEFAULT_OUTPUT = os.path.join("config", "config.yaml")
QR_IMAGE = "miyoushe_login_qr.png"


# ============================================================
#  1. 扫码登录
# ============================================================

def _headers(device_id):
    return {
        "User-Agent": USER_AGENT,
        "x-rpc-app_id": APP_ID,
        "x-rpc-client_type": CLIENT_TYPE,
        "x-rpc-app_version": APP_VERSION,
        "x-rpc-device_id": device_id,
        "x-rpc-sys_version": "12",
        "x-rpc-channel": "miyousheluodi",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _post(url, body, device_id):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST", headers=_headers(device_id)
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def _get(url, stoken, uid, mid):
    req = urllib.request.Request(url, headers={
        "Cookie": f"stuid={uid};stoken={stoken};mid={mid}",
        "User-Agent": USER_AGENT,
        "Referer": "https://app.mihoyo.com",
        "Accept": "application/json",
        "x-rpc-app_id": APP_ID,
        "x-rpc-client_type": CLIENT_TYPE,
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def create_qr():
    """创建扫码会话，token_types=1 表示确认后签发 SToken"""
    device_id = str(uuid.uuid4()).upper()
    d = _post(CREATE_URL, {"token_types": [1]}, device_id)
    if d.get("retcode") != 0:
        raise RuntimeError(f"创建二维码失败: retcode={d.get('retcode')} {d.get('message')}")
    return device_id, d["data"]["url"], d["data"]["ticket"]


def render_qr(qr_url, path=QR_IMAGE):
    """渲染二维码图片；缺少 qrcode 库时返回 False，由调用方降级处理"""
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
    except ImportError:
        return False

    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=12, border=4)
    qr.add_data(qr_url)
    qr.make(fit=True)
    qr.make_image(fill_color="black", back_color="white").convert("RGB").save(path)
    return True


def wait_confirm(device_id, ticket, timeout=120, on_status=None):
    """轮询扫码状态：Created -> Scanned -> Confirmed"""
    start, last = time.time(), None
    while time.time() - start < timeout:
        d = _post(QUERY_URL, {"ticket": ticket}, device_id)
        data = d.get("data") or {}
        status = data.get("status")
        if status != last:
            if on_status:
                on_status(status, int(time.time() - start))
            last = status
        if status == "Confirmed":
            return data
        if d.get("retcode") not in (0, None):
            if d.get("retcode") in (-106, -3003):
                raise TimeoutError("二维码已过期，请重新运行脚本")
            raise RuntimeError(f"查询扫码状态失败: {d.get('retcode')} {d.get('message')}")
        time.sleep(3)
    raise TimeoutError("等待扫码超时")


# ============================================================
#  2. 换取凭据
# ============================================================

def exchange_tokens(stoken, uid, mid):
    """用 SToken 换取旧版 cookie_token 与 ltoken

    signIn 接口需要的是 40 位旧版 cookie_token，不是 cookie_token_v2，
    所以这一步不能省。
    """
    d1 = _get(f"{COOKIE_TOKEN_URL}?stoken={stoken}&uid={uid}", stoken, uid, mid)
    d2 = _get(f"{LTOKEN_URL}?stoken={stoken}&uid={uid}", stoken, uid, mid)

    if d1.get("retcode") != 0:
        raise RuntimeError(
            f"SToken 无效，无法换取 cookie_token: {d1.get('message')}"
        )

    out = {
        "stoken": stoken,
        "uid": str(uid),
        "mid": str(mid),
        "cookie_token": (d1.get("data") or {}).get("cookie_token", ""),
        "ltoken": "",
    }
    if d2.get("retcode") == 0:
        d = d2.get("data") or {}
        out["ltoken"] = d.get("ltoken") or d.get("token") or ""
    return out


# ============================================================
#  3. 组装配置
# ============================================================

def parse_cookie(cookie, key):
    m = re.search(rf"(?:^|;\s*){key}=([^;]*)", cookie or "")
    return m.group(1) if m else ""


def load_existing_cookie(path):
    """读取已有配置里的 cookie，用于复用设备指纹（降低风控概率）"""
    if not path or not os.path.exists(path):
        return ""
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return ((cfg.get("account") or {}).get("cookie") or "").strip()
    except Exception:
        return ""


def device_fingerprint(old_cookie):
    """优先复用旧设备指纹；没有则生成一套新的，避免空值触发风控"""
    fp = {
        "uuid": parse_cookie(old_cookie, "_MHYUUID"),
        "seed_id": parse_cookie(old_cookie, "DEVICEFP_SEED_ID"),
        "seed_time": parse_cookie(old_cookie, "DEVICEFP_SEED_TIME"),
        "fp": parse_cookie(old_cookie, "DEVICEFP"),
    }
    reused = all(fp.values())
    if not fp["uuid"]:
        fp["uuid"] = str(uuid.uuid4())
    if not fp["seed_time"]:
        fp["seed_time"] = str(int(time.time() * 1000))
    if not fp["seed_id"]:
        fp["seed_id"] = secrets.token_hex(8)
    if not fp["fp"]:
        fp["fp"] = secrets.token_hex(7)[:13]
    return fp, reused


def build_cookie(creds, fp):
    parts = [
        f"_MHYUUID={fp['uuid']}",
        f"DEVICEFP_SEED_ID={fp['seed_id']}",
        f"DEVICEFP_SEED_TIME={fp['seed_time']}",
        f"DEVICEFP={fp['fp']}",
        f"account_mid_v2={creds['mid']}",
        f"account_id_v2={creds['uid']}",
        f"ltmid_v2={creds['mid']}",
        f"ltuid_v2={creds['uid']}",
        f"account_id={creds['uid']}",
        f"ltuid={creds['uid']}",
        f"cookie_token={creds['cookie_token']}",
        f"ltoken={creds['ltoken']}",
        f"stoken={creds['stoken']}",
    ]
    return "; ".join(p for p in parts if not p.endswith("="))


def build_config(creds, cookie):
    """生成 version 15 完整配置结构（字段与 template.yml / config.py 对齐）"""
    UA_MOBILE = (
        "Mozilla/5.0 (Linux; Android 12; Unspecified Device) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Version/4.0 Chrome/103.0.5060.129 Mobile Safari/537.36"
    )
    return {
        "enable": True,
        "version": 15,
        "push": "",
        "account": {
            "cookie": cookie,
            "stuid": creds["uid"],
            "stoken": creds["stoken"],
            "mid": creds["mid"],
        },
        "device": {
            "name": "Xiaomi MI 6",
            "model": "Mi 6",
            "id": parse_cookie(cookie, "_MHYUUID"),
            "fp": parse_cookie(cookie, "DEVICEFP"),
        },
        "mihoyobbs": {
            "enable": True, "checkin": True, "checkin_list": [5, 2],
            "read": True, "like": True, "cancel_like": True, "share": True,
        },
        "games": {
            "cn": {
                "enable": True, "useragent": UA_MOBILE, "retries": 3,
                "genshin": {"checkin": False, "black_list": []},
                "honkai2": {"checkin": False, "black_list": []},
                "honkai3rd": {"checkin": False, "black_list": []},
                "tears_of_themis": {"checkin": False, "black_list": []},
                "honkai_sr": {"checkin": True, "black_list": []},
                "zzz": {"checkin": False, "black_list": []},
            },
            "os": {
                "enable": False, "cookie": "", "lang": "zh-cn",
                "genshin": {"checkin": False, "black_list": []},
                "honkai3rd": {"checkin": False, "black_list": []},
                "tears_of_themis": {"checkin": False, "black_list": []},
                "honkai_sr": {"checkin": False, "black_list": []},
                "zzz": {"checkin": False, "black_list": []},
            },
        },
        "cloud_games": {
            "cn": {
                "enable": False,
                "genshin": {"enable": False, "token": ""},
                "zzz": {"enable": False, "token": ""},
            },
            "os": {
                "enable": False, "lang": "zh-cn",
                "genshin": {"enable": False, "token": ""},
            },
        },
        "competition": {
            "enable": False,
            "genius_invokation": {
                "enable": False, "account": [], "checkin": False, "weekly": False,
            },
        },
        "web_activity": {"enable": False, "activities": []},
    }


# ============================================================
#  4. 写入 GitHub Secret
# ============================================================

def upload_secret(repo, content_bytes, token):
    """用仓库公钥加密后写入 CONFIG_YAML secret"""
    try:
        from nacl import encoding, public
    except ImportError:
        sys.exit("缺少依赖 libsodium 绑定，请先执行: pip install pynacl")

    api = "https://api.github.com"

    def req(method, url, body=None):
        data = json.dumps(body).encode() if body else None
        r = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "miyoushe-qr-login",
        })
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode() or "{}")
            except Exception:
                return e.code, {}

    st, pk = req("GET", f"{api}/repos/{repo}/actions/secrets/public-key")
    if st != 200:
        raise RuntimeError(
            f"获取仓库公钥失败（HTTP {st}）：{pk.get('message')}\n"
            "请确认 PAT 具备 Secrets: Read and write 权限，且仓库名正确"
        )

    box = public.SealedBox(public.PublicKey(pk["key"].encode(), encoding.Base64Encoder()))
    encrypted = base64.b64encode(box.encrypt(content_bytes)).decode()
    st, r = req("PUT", f"{api}/repos/{repo}/actions/secrets/CONFIG_YAML",
                {"encrypted_value": encrypted, "key_id": pk["key_id"]})
    if st not in (201, 204):
        raise RuntimeError(f"写入 secret 失败（HTTP {st}）：{r.get('message')}")
    return st


# ============================================================
#  主流程
# ============================================================

def main():
    ap = argparse.ArgumentParser(
        description="米游社扫码登录，重新获取 SToken 并写入 GitHub Secret",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPO", ""),
                    help="owner/repo，用于写入 CONFIG_YAML secret")
    ap.add_argument("--output", default=DEFAULT_OUTPUT,
                    help=f"配置输出路径（默认 {DEFAULT_OUTPUT}）")
    ap.add_argument("--no-upload", action="store_true", help="只获取凭据，不写 secret")
    ap.add_argument("--timeout", type=int, default=120, help="等待扫码秒数（默认 120）")
    args = ap.parse_args()

    old_cookie = load_existing_cookie(args.output)

    print("[1/5] 创建 App 通道二维码 …")
    device_id, qr_url, ticket = create_qr()
    print(f"      ticket = {ticket}")

    if render_qr(qr_url):
        print(f"      二维码图片已生成: {QR_IMAGE}")
    else:
        print("      (未安装 qrcode，无法生成图片，请用下面的链接自行生成二维码)")
    print(f"      二维码内容: {qr_url}")

    print(f"\n>>> 请用米游社 App 扫码并确认登录（{args.timeout} 秒内有效）\n")

    print("[2/5] 等待扫码确认 …")
    data = wait_confirm(device_id, ticket, timeout=args.timeout,
                        on_status=lambda s, t: print(f"      [{t:3d}s] {s}"))

    info = data.get("user_info") or {}
    uid, mid = str(info.get("aid")), str(info.get("mid"))
    stoken = next((t["token"] for t in data.get("tokens", [])
                   if t.get("token_type") == 1), "")
    if not stoken:
        sys.exit("未取得 SToken（token_type=1），请重新运行")

    print(f"      登录成功 | uid={uid} | mid={mid}")
    print(f"      SToken 长度 = {len(stoken)}（不打印内容）")

    print("[3/5] 用 SToken 换取 cookie_token / ltoken …")
    creds = exchange_tokens(stoken, uid, mid)
    print(f"      cookie_token = {len(creds['cookie_token'])} 位")
    print(f"      ltoken       = {len(creds['ltoken'])} 位")

    print("[4/5] 组装配置 …")
    fp, reused = device_fingerprint(old_cookie)
    print(f"      设备指纹: {'复用旧值' if reused else '已重新生成'}")
    cookie = build_cookie(creds, fp)
    cfg = build_config(creds, cookie)

    try:
        import yaml
    except ImportError:
        sys.exit("缺少依赖 PyYAML，请先执行: pip install pyyaml")

    text = yaml.dump(cfg, Dumper=yaml.Dumper, sort_keys=False, allow_unicode=True)

    out_dir = os.path.dirname(args.output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.chmod(args.output, 0o600)
    except OSError:
        pass
    print(f"      已写入 {args.output}（权限 600，已被 .gitignore 忽略）")

    if args.no_upload:
        print("\n[5/5] 已跳过上传（--no-upload）")
        print("      把上面的 cookie 内容填进仓库 Settings → Secrets → CONFIG_YAML 即可")
        return

    token = os.environ.get("GITHUB_PAT", "")
    if not token:
        sys.exit("缺少环境变量 GITHUB_PAT")
    if not args.repo:
        sys.exit("缺少 --repo 参数，例如 --repo wdx-cup/MihoyoAutoScript")

    print(f"[5/5] 写入 {args.repo} 的 CONFIG_YAML secret …")
    st = upload_secret(args.repo, text.encode(), token)
    print(f"      完成（HTTP {st}）")

    print("\n✅ 全部完成。")
    print("   到仓库 Actions 页面手动触发一次「米游社每日签到」验证即可，")
    print("   之后它会按 cron 每天自动执行。")


if __name__ == "__main__":
    main()
