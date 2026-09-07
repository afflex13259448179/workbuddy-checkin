# WorkBuddy 每日自动签到

Python3 标准库 + GitHub Actions，每天自动领取 WorkBuddy 签到积分，零第三方依赖。

## 接口

| 用途 | 地址 | 方法 |
| --- | --- | --- |
| 查询状态 | `https://www.codebuddy.cn/v2/billing/meter/checkin-status` | POST |
| 领取签到 | `https://www.codebuddy.cn/v2/billing/meter/daily-checkin` | POST |

> 注意：`copilot.tencent.com` 的旧地址已 404，请使用 `www.codebuddy.cn` + `/v2/` 路径。

## 多账号签到（本地）

脚本会自动扫描本地 auth 目录下**所有** `.info` 登录态文件（含历史备份），按 uid 去重后对每个账号签到。

实测要点（2026-09 验证）：

- **退出登录不会使旧 token 失效**——服务端不主动回收，token 为长效 JWT，各自独立过期
- 同一账号不同时期的 token 有效期不同：失效时脚本会自动从新到旧降级尝试该账号的其他历史 token
- 因此多个账号只需在本机各自登录过一次，之后**无需来回切换登录**即可全部签到

```bash
python checkin.py
```

也可以复制 `config.example.json` 为 `config.json` 填入 accessToken 后运行（`config.json` 已被 .gitignore 排除）。

## 部署到 GitHub Actions（多账号，电脑关机也能签）

1. 在 GitHub 新建**私有**仓库（例如 `workbuddy-checkin`），不要勾选自动生成 README。

2. 配置 Secret：仓库 **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `WORKBUDDY_ACCOUNTS`
   - Secret 值: 打开本地 `secrets-WORKBUDDY_ACCOUNTS.json` 的内容，整段复制粘贴进去
   （该文件包含 5 个账号的有效 token，已被 .gitignore 排除，不会进入仓库）

3. 推送代码（不要推 config.json 和 secrets 文件）：

```bash
git init
git add .gitignore README.md checkin.py config.example.json .github/workflows/checkin.yml
git commit -m "init: WorkBuddy GitHub Actions 多账号每日签到"
git branch -M main
git remote add origin https://github.com/<你的用户名>/workbuddy-checkin.git
git push -u origin main
```

4. Actions 页面手动 **Run workflow** 验证一次，成功后每天北京时间 09:05 自动执行，日志中会显示 5 个账号的逐一签到结果。

## Token 过期后（多账号）

token 各自独立过期。某天 Actions 日志出现「所有候选 token 均无效」时：

1. 在本机 WorkBuddy 客户端登录该账号一次（auth 目录会生成新 token）
2. 本地跑 `python checkin.py` 确认该账号恢复成功
3. 重新生成本地 auth 有效 token 并更新 GitHub Secret `WORKBUDDY_ACCOUNTS`

不用改代码，不用重新部署。

## 安全提醒

- `accessToken` 等同登录态，不要提交到仓库、不要发到群里
- 推送 workflow 文件时 Classic PAT 需勾选 `repo` + `workflow` 两项权限
