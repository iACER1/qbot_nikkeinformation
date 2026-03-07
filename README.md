# qbot_nikkeinformation 插件

面向《胜利女神：NIKKE》的 AstrBot 扩展，实现账号战力、工会战与联盟突袭的自动查询，并支持将结果转化为图片或文本返回。

## 功能亮点

- 账号绑定：为不同会话记录 openid 与 intl_open_id，绑定一次即可复用后续查询。
- 战力速览：调用脚本生成战力前十列表，并自动汇总装备词条要点，便于快速评估角色培养方向。
- 工会战进度：抓取 Boss 当前血量、最大血量与百分比，输出进度图或文本提醒。
- 联盟出刀统计：聚合成员刀数、总伤害与缺刀名单，一目了然掌握联盟出勤情况。
- 名称表自动更新：更新角色名映射时，默认自动发现当前站点实际使用的最新 JSON，无需手动去 F12 复制哈希 CDN 地址。

## 使用前准备

1. 准备有效的网页登录 Cookie，保存为 `cookie.txt` 并置于插件数据目录，或通过环境变量 `NIKKE_COOKIE_PATH`
   指定自定义位置。
2. 获取待绑定账号的标识。支持 Base64 openid（同网页 URL 参数）、形如 `29080-XXXX`
   的国际 open_id，或纯数字 intl_open_id。
3. 建议控制查询频率（单账号至少五分钟一次），避免触发接口限流。

## 快速上手

1. 将插件目录放入 AstrBot 的 `data/plugins` 下并在后台启用。
2. 启动 AstrBot 后，于目标群聊或私聊输入 `/nikke bind <openid>` 完成绑定。
3. 运行 `/nikke info` 获取战力前十摘要，或使用工会相关指令生成最新图表。
4. 管理员可运行 `/nikke update_namelist` 自动刷新角色名称映射。

## 指令索引

### 普通用户

| 指令                       | 功能                                        | 说明                                   |
| -------------------------- | ------------------------------------------- | -------------------------------------- |
| `/nikke bind <openid>`     | 绑定当前会话与 NIKKE 账号                   | 支持重复绑定覆盖                       |
| `/nikke info`              | 输出战力前十与装备词条摘要                  | 需先准备有效 Cookie                    |
| `/nikke namecode <code>`   | 按 name_code 精准查询指定角色并返回详情摘要 | 支持一次提交多个 code，逗号或空格分隔  |
| `/nikke unionraid`         | 展示工会战 Boss 血量进度图                  | 失败时回退为文本列表                   |
| `/nikke unionraid_members` | 统计联盟成员出刀次数与总伤害                | 首次运行会生成成员映射文件，可手动改名 |
| `/nikke unionraid_missing` | 列出未出刀或未满三刀成员                    | 基于成员映射与最新抓取结果计算         |

### 管理员

| 指令                     | 功能                       | 说明                                              |
| ------------------------ | -------------------------- | ------------------------------------------------- |
| `/nikke cookie`          | 查看当前 Cookie 路径与状态 | 优先使用环境变量指定的路径                        |
| `/nikke update_namelist` | 更新角色名映射表           | 默认自动发现最新 JSON，也可在配置中补充手动源地址 |

## 数据与配置

- 插件在数据目录下维护
  `bindings.json`、`cookie.txt`、`latest.json`、`union_raid_members_latest.json`、`union_members_map.json`
  等文件，用于缓存绑定信息与抓取结果。
- 若需自定义成员显示昵称，可编辑 `union_members_map.json` 中的 `member` 字段，保存后再次查询即可生效。
- `names_updater.auto_discover`
  默认开启。开启后，插件会从 Blablalink 首页与当前入口 JS 自动发现名称表逻辑路径，再换算为实际 CDN JSON 地址。
- `names_updater.sources` 现在作为可选的手动补充/兜底来源使用；通常留空即可。

## 常见问题

- **提示 Cookie 不存在**：确认 `cookie.txt` 已写入并与配置一致，或重新设置 `NIKKE_COOKIE_PATH`。
- **战力结果为空**：账号可能未公开或 openid 无效，建议重新确认标识后稍后再试。
- **成员名字显示异常**：在 `union_members_map.json` 内补充 `openid` 与 `member` 对应关系即可矫正。
- **更新名称表失败**：优先保持 `names_updater.auto_discover=true`；若站点结构临时变更，也可手动在
  `names_updater.sources` 中补充一个有效 JSON 地址作为兜底。

欢迎结合 AstrBot 的定时任务或订阅功能，自动推送工会战进度与缺刀提醒。

## 功能验证建议

1. 确保 `cookie.txt` 中的登录态有效，并完成 `/nikke bind <openid>` 绑定。
2. 先执行 `/nikke update_namelist`，确认机器人提示“已更新名称映射”且来源数大于 0。
3. 再执行 `/nikke namecode 5155`（或替换为实际存在的 code），检查机器人是否返回指定角色的摘要。
4. 清理或损坏 `cookie.txt` 后再次执行命令，验证插件能正确提示缺少登录态。
5. 执行 `/nikke info`，确认机器人会直接返回固定文本摘要，不再额外触发 AI 分析。
