# qbot_nikkeinformation 插件

面向《胜利女神：NIKKE》的 AstrBot 扩展，实现账号战力、工会战与联盟突袭的自动查询，并支持将结果转化为图片或文本返回。

## 功能亮点

- 账号绑定：为不同会话记录 openid 与 intl_open_id，绑定一次即可复用后续查询。
- 战力速览：调用脚本生成战力前十列表，并自动汇总装备词条要点，便于快速评估角色培养方向。
- 工会战进度：抓取 Boss 当前血量、最大血量与百分比，输出进度图或文本提醒。
- 联盟出刀统计：聚合成员刀数、总伤害与缺刀名单，一目了然掌握联盟出勤情况。
- 可选 AI 解释：在启用 LLM 后，可让机器人生成更自然的讲解与作战建议。

## 使用前准备

1. 准备有效的网页登录 Cookie，保存为 `cookie.txt` 并置于插件数据目录，或通过环境变量 `NIKKE_COOKIE_PATH` 指定自定义位置。
2. 获取待绑定账号的标识。支持 Base64 openid（同网页 URL 参数）、形如 `29080-XXXX` 的国际 open_id，或纯数字 intl_open_id。
3. 建议控制查询频率（单账号至少五分钟一次），避免触发接口限流。

## 快速上手

1. 将插件目录放入 AstrBot 的 `data/plugins` 下并在后台启用。
2. 启动 AstrBot 后，于目标群聊或私聊输入 `/nikke bind <openid>` 完成绑定。
3. 运行 `/nikke info` 获取战力前十摘要，或使用工会相关指令生成最新图表。

## 指令索引

### 普通用户

| 指令 | 功能 | 说明 |
| --- | --- | --- |
| `/nikke bind <openid>` | 绑定当前会话与 NIKKE 账号 | 支持重复绑定覆盖 |
| `/nikke info` | 输出战力前十与装备词条摘要 | 需先准备有效 Cookie |
| `/nikke namecode <code>` | 按 name_code 精准查询指定角色并返回详情摘要 | 支持一次提交多个 code，逗号或空格分隔 |
| `/nikke unionraid` | 展示工会战 Boss 血量进度图 | 失败时回退为文本列表 |
| `/nikke unionraid_members` | 统计联盟成员出刀次数与总伤害 | 首次运行会生成成员映射文件，可手动改名 |
| `/nikke unionraid_missing` | 列出未出刀或未满三刀成员 | 基于成员映射与最新抓取结果计算 |

### 管理员

| 指令 | 功能 | 说明 |
| --- | --- | --- |
| `/nikke cookie` | 查看当前 Cookie 路径与状态 | 优先使用环境变量指定的路径 |
| `/nikke update_namelist` | 更新角色名映射表 | 可在配置中自定义源地址与语言 |

## 数据与配置

- 插件在数据目录下维护 `bindings.json`、`cookie.txt`、`latest.json`、`union_raid_members_latest.json`、`union_members_map.json` 等文件，用于缓存绑定信息与抓取结果。
- 若需自定义成员显示昵称，可编辑 `union_members_map.json` 中的 `member` 字段，保存后再次查询即可生效。
- 通过 `ai_settings` 配置可启用 AI 回复；启用后，插件会把绑定或查询摘要注入系统提示词，再向 LLM 发送请求。若调用失败，会自动回退到默认文本回复。

## 常见问题

- **提示 Cookie 不存在**：确认 `cookie.txt` 已写入并与配置一致，或重新设置 `NIKKE_COOKIE_PATH`。
- **战力结果为空**：账号可能未公开或 openid 无效，建议重新确认标识后稍后再试。
- **成员名字显示异常**：在 `union_members_map.json` 内补充 `openid` 与 `member` 对应关系即可矫正。

欢迎结合 AstrBot 的定时任务或订阅功能，自动推送工会战进度与缺刀提醒。

## 功能验证建议

1. 确保 `cookie.txt` 中的登录态有效，并完成 `/nikke bind <openid>` 绑定。
2. 执行 `/nikke namecode 5155`（或替换为实际存在的 code），检查机器人是否返回指定角色的摘要。
3. 同时传入多个 code（如 `/nikke namecode 5155,5065`），确认返回内容包含所有角色并无报错。
4. 清理或损坏 `cookie.txt` 后再次执行命令，验证插件能正确提示缺少登录态。
5. 切换开启/关闭 `ai_settings.enable_ai_for_info`，确认启用时机器人会走 LLM 回复，失败时能回退到默认文本。
