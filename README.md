# AICreditPunch

![WorkBuddy Logo](https://download.codebuddy.cn/web/workbuddy/77c2617b394171f8938c8d4f0abce65e306fb458/assets/workbuddy-logo-WhgOvEF7.png)

WorkBuddy 多账号自动签到、状态和积分查询脚本。开箱即可在安装 Python 3.8+ 的电脑、服务器或青龙面板使用。

## 下载与运行

1.在 [Releases](https://github.com/tianxing226/AICreditPunch/releases/latest) 下载 `AICreditPunch-v2.2.0.zip` 并解压。

2.执行python workbuddy_checkin.py后，可以自动获取你workbuddy账号的access_token等相关信息并写入config.json文件，将config.json和workbuddy_checkin.py上传到青龙脚本或者在本地的ai添加定时任务即可实现每日的自动签到。

3.在已登录 WorkBuddy 的电脑进入解压目录，运行：

   ```bash
   python workbuddy_checkin.py --setup
   python workbuddy_checkin.py
   ```
   `config.json` 是空模板；`--setup` 会自动导入本机登录信息并合并多个账号。

## 青龙 / 定时任务

青龙命令示例：

```bash
python3 /ql/data/scripts/workbuddy/workbuddy_checkin.py --config /ql/data/scripts/workbuddy/config.json
```

Linux cron 示例：

```cron
0 5 * * * cd /opt/AICreditPunch && python3 workbuddy_checkin.py --config ./config.json >> workbuddy.log 2>&1
```

## ai / 定时任务
workbuddy任务演示:

```
角色：系统自动化运维与 Python 开发专家

任务目标：
配置并部署基于 GitHub 仓库（https://github.com/tianxing226/AICreditPunch/）的自动签到脚本 `workbuddy_checkin.py`，并设置每天 08:30 自动执行。

请按以下要求分步执行并给出操作指导：
1. 依赖与环境准备：检查或安装 Python 运行环境及项目所需的 requirements 依赖。
2. 配置文件配置：
   - 参考项目说明生成标准 `config.json` 模板。
   - 明确标注需要用户填写的敏感/关键字段（如 Cookie、Token、账号密码、Webhook 通知地址等）。
   - 提供安全保存与加载配置的最佳实践。
3. 定时任务设置：
   - Linux / macOS：提供标准的 Crontab 命令（每天 08:30 触发），包含工作目录切换、Python 绝对路径以及日志重定向（`>> checkin.log 2>&1`）。
   - Windows：提供基于 schtasks 或任务计划程序（Task Scheduler）的配置步骤。
4. 测试与验证：给出单次手动测试运行的方法及日志排查指南。
```

直接将上面的提示词扔给workbuddy或者其他agent智能体，或者手动添加定时任务。

## 包含内容

- `workbuddy_checkin.py`：签到、状态、积分和多账号配置。
- `config.json`：空数组模板，不含任何真实凭据。
- `README.md`：完整配置、token 获取和常见问题说明。

后续会沿用同一配置和定时任务结构，逐步适配更多 Agent 的自动签到脚本。

请勿把运行后生成的 `config.json` 或 `config.json.bak` 上传到公开仓库；它们包含登录凭据。
