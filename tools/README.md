# 扫码重新登录（SToken 续期工具）

> **Actions 日志出现「cookie 可能已过期」「无 Stoken 请手动填入」时，跑这个脚本。**
> 不用抓包，不用安卓模拟器，全程约 30 秒。

---

## 什么时候需要跑

签到失败的日志里出现下面任意一条，说明登录凭据失效：

```
ERROR  签到失败，你的 cookie 可能已过期，请重新设置 cookie
ERROR  无 Stoken 请手动填入 stoken
ERROR  账号 Stoken 有问题
```

## 三步搞定

第一步，安装依赖（qrcode 和 pillow 可选，仅用于生成二维码图片）：

```bash
pip install pyyaml qrcode pillow
```

第二步，准备一个 fine-grained token，授予 Secrets 的 Read and write 权限，然后设为环境变量：

```bash
export GITHUB_PAT=填你的令牌
```

第三步，运行脚本（仓库名换成你自己的）：

```bash
python tools/miyoushe_qr_login.py --repo 你的用户名/MihoyoAutoScript
```

终端会生成 `miyoushe_login_qr.png`，用**手机米游社 App**扫码并确认登录。
确认后脚本自动完成：换取 SToken、补齐 cookie_token、生成配置、更新 `CONFIG_YAML` secret。

完成后到 Actions 页面手动触发一次「米游社每日签到」验证，之后按 cron 自动执行。

## 为什么普通 cookie 不行

这是最容易踩的坑：

| 方式 | 拿到的凭证 | 能否签到 |
|------|-----------|---------|
| 浏览器 F12 复制 cookie | `ltoken_v2`、`cookie_token_v2` | 否，报 `-100 登录失效` |
| 手机 App 抓包 | `stoken` | 可以 |
| **本脚本（App 通道扫码）** | **`stoken`** | 可以 |

米游社的签到接口 `signIn` 使用**强认证**，只认 App 通道的 `SToken`。网页登录根本不下发它，
所以典型症状是「能查角色、能读任务列表，但一签到就 `-100`」。

脚本拿到 SToken 后，还会用它换一个 **40 位旧版 `cookie_token`**，这才是 `signIn`
真正需要的字段，而不是 `cookie_token_v2`。

## 常见问题

### 提示 -3001 或 -3005 参数不合法

用的是网页通道端点。本脚本走的是 App 通道
`/account/ma-cn-passport/app/createQRLogin`，且 `app_id = bll8iq97cem8`、
`x-rpc-client_type = 3`，三个必须同时正确。

### 二维码已过期或等待扫码超时

二维码有效期约 120 秒。重新运行脚本即可，也可加 `--timeout 300` 延长等待。

### 写入 secret 失败 403

令牌缺少 Secrets 的 Read and write 权限。fine-grained token 还需要在
`Repository access` 里勾选目标仓库，并在页面底部点 **Update token** 保存。

### 只想拿配置，不想自动写 secret

```bash
python tools/miyoushe_qr_login.py --no-upload --output config/config.yaml
```

然后把 `config/config.yaml` 的内容填进仓库 Settings 里的 Secrets 页面。

### 生成的 config.yaml 会被误提交吗

不会。`.gitignore` 已包含 `config/*.yaml`，且脚本写入后会把权限设为 600。
提交前可以 `git status` 确认。

## 安全说明

- 本脚本不含任何密钥，可安全放在公开仓库
- 终端输出只打印 token 长度，不打印内容
- 生成的 `config/config.yaml` 含真实 cookie，已被 `.gitignore` 忽略
- 扫码 ticket 与 SToken 均不落盘（`--no-upload` 模式除外，会写入你指定的文件）
