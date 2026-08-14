# MOViE MOViE《奥德赛》GitHub Actions 排片监控

这是一个最小化的只读排片提醒程序：启用后，GitHub Actions 每 5 分钟请求一次固定影院的猫眼移动端影院详情接口，发现《奥德赛》首次、新增或恢复上架的未来场次后，通过飞书群自定义机器人 Webhook 发送一条合并提醒。它不会登录、选座、提交订单或支付。

## 数据源、许可与修改说明

数据源访问和排片字段解析思路来自 `perbright/movie-movie` 项目。上游仓库标识的原作者/维护者账号为 `perbright`，原项目版权归其原作者所有。本仓库是为 GitHub Actions 长期定时任务制作的修改版本，保留原项目的 GPL-3.0 `LICENSE`；本说明不改变或替代上游版权声明。

主要修改：取消影院模糊搜索与同轮重试；固定 `cinema_id` 并验证影院名称；按上海时区过滤全部未来场次；增加首次、新增、恢复上架识别、原子状态文件、三次通知上限、持久化停源保护、GitHub Actions 状态提交和带签名的飞书群自定义机器人 Webhook 提醒。项目不保存完整接口响应。

上游项目：`https://gitee.com/perbright/movie-movie.git`

## 固定监控目标

- 影院 ID：`37534`
- 影院名称必须包含：`MOViE MOViE`
- 影片精确名称：`奥德赛`
- 明确别名：可在 `config.json` 的 `movie_aliases` 数组中人工添加
- 周期：GitHub Actions cron `3/5 * * * *`（GitHub 调度可能有数分钟延迟，不保证严格准点）

每个工作流只请求该影院一次，不重试、不切换接口、不绕过 CAPTCHA 或风控。提醒统一使用：

> 发现新排片，可能已开放购票，请立即人工打开购票平台确认。

## GitHub Secrets 与正式开关

本项目只使用飞书群 V2 自定义机器人 Webhook，不使用飞书应用机器人、Open ID、tenant token 或消息 OpenAPI。以下两项只从 GitHub Actions Secrets 读取：

- `FEISHU_WEBHOOK`
- `FEISHU_SECRET`

请在公开仓库创建后，由仓库所有者手工进入：

`GitHub 仓库 → Settings → Secrets and variables → Actions → New repository secret`

逐项添加。Webhook 只允许 `https://open.feishu.cn/open-apis/bot/v2/hook/` 命名空间，程序按飞书规则使用 `FEISHU_SECRET` 生成 HMAC-SHA256 签名。不要把本地环境变量、`.env`、Webhook、Secret、Cookie、手机号或任何凭据提交到仓库，也不要在 issue、Actions 输入或聊天中粘贴凭据。

仓库变量 `MONITOR_ENABLED` 是正式定时监控总开关。不存在或值不严格等于字符串 `true` 时，定时事件会直接跳过整个 job，不启动 runner、不访问猫眼。新仓库必须先保持为 `false`；测试消息成功后才能改为 `true`。

## 本地验证

```powershell
py -3 -m pip install -r requirements.txt
py -3 -m py_compile movie_watch.py tests\test_movie_watch.py
py -3 -m unittest discover -s tests -v
py -3 movie_watch.py --dry-run
```

`--dry-run` 只请求一次并显示影院及全部未来排片，不发送飞书、不修改正式状态。

## GitHub Actions 手动操作

`Actions → MOViE MOViE Odyssey Watch → Run workflow`：

- 默认 `dry_run=true`：只检查，不发消息、不改状态。
- 人工清除停源：设置 `dry_run=false`、`clear_source_halt=true`。该次仅清除停止状态，不访问猫眼。
- 测试飞书：勾选 `send_test_notification=true` 即可；该分支优先于默认的 `dry_run=true`，只发送一条带 `【电影监控测试】` 标识的消息，不访问猫眼、不改状态、不提交 Git。

定时事件只有在仓库变量 `MONITOR_ENABLED=true` 时才执行正式监控。工作流通过固定 concurrency 组串行运行；状态仅在实质变化时提交，提交信息为 `[skip ci] update movie watch state`。

## 停源边界

HTTP 403/418/429、CAPTCHA/滑块、非 JSON 验证页、缺少正常排片字段、明显静默风控或影院名称不匹配会将 `source_halted=true` 写入 `state/movie_watch_state.json`。工作流先把停源状态提交回仓库，确认提交步骤成功后，再以不访问猫眼的独立步骤尝试一次停源提醒并提交提醒结果。后续工作流会在访问数据源前直接退出；只有上面的人工清除输入可以恢复。

普通 DNS、连接或超时错误只让当前工作流失败；下一个 5 分钟周期可以再次请求。

## 状态与通知重试

状态文件只保存排片指纹、必要的场次展示字段、通知事件/尝试次数和停源信息，不保存请求头、Cookie、凭据或完整接口响应。写入采用同目录临时文件加原子替换。

飞书明确成功后才将场次标为已提醒。明确失败的同一事件可在后续工作流重试，最多三次；达到上限后不再调用消息接口，工作流保持失败，等待人工处理。

## 上线边界

创建公开仓库、推送和启用 Actions 都是外部操作，必须先检查公开文件与敏感信息扫描结果，再取得仓库所有者明确确认。首次上线应先运行 `dry_run=true`，再配置 Secrets、发送一次测试消息，最后观察至少两个定时周期；在这些步骤完成前不能视为线上长期监控已验收。
