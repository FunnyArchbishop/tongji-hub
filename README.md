# 济务台 · TongjiHub

**把散在 QQ 群、Canvas、课堂派、教务、图书馆里的「什么时候要交、什么时候到期」，收进一个本地待办台 + 一份手机能订阅的月历。**

同济专用。只读、只存本机、不登录你的账号去替你交作业。

> 上游是 [ExpertKT/notice-hub](https://github.com/ExpertKT/notice-hub)（群通知 → 待办/日历）。
> 这一版做了两件事：把产品改名成「济务台」，并接上同济自己的一套系统。
> 与同济大学及任何官方系统无关，个人开发，仅供本人学习管理使用。

---

## 它解决什么

在同济读书，一个截止时间可能出现在五个地方：

| 出现在哪 | 具体是什么 |
| --- | --- |
| QQ / 微信群 | 老师或班委发一句「下周三前交」，然后被聊天记录淹掉 |
| Canvas | 作业 `due_at`、公告正文里手写的日期、讨论区的补充说明 |
| 课堂派 | 统一待办里的作业、考试、公告 |
| 教务 1 系统 | 课表、考试安排、成绩 |
| 一网通办 / 图书馆 | 官方日程、借阅到期 |

这五个地方互相不通。济务台把它们统一成同一种「记录」，走同一条流水线：
**抽取 → 认截止时间 → 进待办台 → 导出 ics 给手机日历 → 到期提醒**。

---

## 现在能做什么

### 数据源

| 数据源 | 状态 | 登录方式 | 产出 |
| --- | --- | --- | --- |
| QQ 群（NapCat / OneBot） | ✅ 可用 | 扫码登录 NapCat | 群消息 → 待办 |
| 钉钉群 | ✅ 可用 | 读本机钉钉数据 | 群消息 → 待办 |
| 微信群 | ✅ 可用 | 读本机微信数据库 | 群消息 → 待办 |
| 超星学习通 | ✅ 可用 | 浏览器 Cookie | 通知、未交作业 |
| Canvas | ✅ 可用 | 个人访问令牌 | 作业、公告/讨论里的截止时间 |
| 课堂派 | ✅ 可用 | 账号密码换令牌（30 天） | 作业、考试、公告 |
| 同济教务 1 系统 | ✅ 可用 | 浏览器 `sessionid` | 成绩、课表（→ ics） |
| 同济图书馆 | ✅ 可用 | webpac `jwt` | 借阅到期 |
| 同济校内页面监看 | ✅ 可用 | 统一认证 Cookie | 任意校内页面有更新 → 待办 |
| 一网通办官方日历 | ✅ 可用 | 订阅编号 | 官方 ics → 规范化后并入月历 |

### 同济专有功能

| 功能 | 说明 |
| --- | --- |
| **课表 → ics** | `GET /tongji/timetable.ics`，手机日历直接订阅。带教室、教师、周次展开 |
| **绩点计算器** | `POST /api/tongji/gpa`。本科 5 分制 / 研究生两套官方口径，边界不同不能混算 |
| **一网通办 ics 规范化** | 官方订阅把全天事件写成 `00:00–23:59`，这里转成真正的 `VALUE=DATE` 全天事件 |
| **页面监看** | 教务通知页、学工通知页这类没有接口的页面，填地址 + 关键词，变了自己进待办 |

### 已有能力（继承自上游）

待办台 / 月历 / 手机 App 配对 / 日历订阅 / 推送（QQ、WxPusher、Server酱、PushPlus、Webhook）/ AI 摘要 / 每周复盘 / 悬浮球 / 深浅色主题 + 三套皮肤（纸墨 / Canvas / QQ）。

---

## 快速开始

### 从源码跑

```powershell
git clone https://github.com/FunnyArchbishop/tongji-hub.git
cd tongji-hub
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe main.py doctor   # 检查配置和依赖
.\.venv\Scripts\python.exe main.py run      # 启动服务
```

默认地址 <http://127.0.0.1:8766/>。

### 只想先看界面

```powershell
.\.venv\Scripts\python.exe -c "from pathlib import Path; from noticehub.platform.config import Settings, ensure_dirs; from noticehub.infrastructure.sqlite.store import Store; from noticehub.web.webapp import TaskWebServer; import time; s=Settings.from_env(); ensure_dirs(s); TaskWebServer(s, Store(Path(s.data_dir)/'notice-hub.sqlite3')).start(); print('http://127.0.0.1:8766/'); time.sleep(3600)"
```

---

## 同济集成：凭据怎么拿

⚠️ **`*.tongji.edu.cn` 在校外不可达**，这套功能必须在校园网或学校 VPN 里跑。

凭据全部存在 `data/` 目录下（`*.cookie`），不进 `.env`、不进日志、不上传。

### 教务 1 系统（成绩 + 课表）

1. 浏览器登录 `https://1.tongji.edu.cn`，F12 → Console
2. 执行 `copy(sessionStorage.sessionid)`，再执行 `copy(sessionStorage.uid)`（有的版本在 `localStorage`）
3. 设置 → 同济 → 填「教务会话」「学号」→ 保存

> 会话只活几小时，过期后重新复制一次即可。工具不会替你输密码，也不会破验证码。

### 图书馆（借阅到期）

1. 登录 `https://webpac.tongji.edu.cn/s12/`，F12 → Console
2. `copy(sessionStorage.jwt)`；`jwtHeader` 一般是 `Authorization`
3. 设置 → 同济 → 填「图书馆登录信息」→ 保存

### 校内页面监看

1. 浏览器登录任意同济站点，F12 → Network → 任选一个请求 → 右键 **Copy as cURL**
2. 从里面抠出 `Cookie:` 后面那一整串
3. 设置 → 同济 → 填「统一认证 Cookie」，再在监看列表里加「名称 + 网址 + 关键词」

### 一网通办官方日历

在 `https://all.tongji.edu.cn` 里找到你的日历订阅地址，形如 `https://all.tongji.edu.cn/cal/<编号>`，
把 `<编号>` 填进「日历订阅编号」。

---

## 绩点计算器：两套口径，别混

| 档位 | 本科（5 分制） | 研究生 |
| --- | --- | --- |
| 5 | 优 A：89.5–100（折算 95） | 90–100 |
| 4 | 良 B：79.5–89.4（折算 85） | 80–<90 |
| 3 | 中 C：69.5–79.4（折算 75） | 70–<80 |
| 2 | 及格 D：59.5–69.4（折算 65） | 60–<70 |
| 0 | 不及格 F：0–59.4（**折算 30，不是 0**） | <60 |

`GPA = Σ(学分 × 绩点) / Σ学分`，全部课程纳入。

**关键差别**：89.7 在本科算「优 = 5」，在研究生算「B = 4」。所以计算必须分两条分支。

```powershell
curl.exe -X POST http://127.0.0.1:8766/api/tongji/gpa `
  -H "Content-Type: application/json" `
  -d '{"level":"undergraduate","courses":[{"name":"高数","score":92,"credit":5},{"name":"英语","score":78,"credit":3}]}'
```

---

## 隐私与安全

- **只读**：不提交作业、不签到、不选课、不预约、不缴费、不发邮件。写操作一律不做。
- **不存密码**：课堂派用账号密码换一次令牌，密码用完即弃；其余全部是浏览器里已经存在的会话凭据。
- **只在本机**：待办、月历、凭据都在 `data/` 下，不上传任何服务器。推送通道是你自己配的第三方。
- **不碰他人数据**：只读本人账号，没有「帮别人查」这类功能。
- **频率克制**：默认 30 分钟一轮，串行请求，不做并发。

---

## 目录结构

```
main.py                    入口（doctor / run）
noticehub/
  receivers/               各平台接收端：QQ、微信、钉钉、超星、Canvas、课堂派
    ddl_sync.py            带截止时间的数据源共用底座（状态/基线/改期/线程）
    browser_cookie.py      从浏览器 Cookie 库取登录态（DPAPI + AES-GCM）
  tongji/                  同济校园集成
    session.py             统一认证会话
    jwc.py                 教务 1 系统：成绩、课表、校历、课表→ics
    library.py             图书馆借阅到期
    calendar_feed.py       一网通办官方 ics 规范化
    watches.py             校内页面监看
    gpa.py                 绩点换算（本科/研究生两套口径）
    reader.py              三路轮询器
  ingest/                  消息进库前的抽取与归纳
  delivery/                投递与调度
  web/                     HTTP 接口、页面、设置
  platform/                配置、日志、版本
docs/wiki/                 长期文档（安装、使用、配置、架构、更新日志）
tests/                     单测与浏览器用例
```

---

## 开发与测试

```powershell
$env:TEMP = "$PWD\nh-tmp"; $env:TMP = $env:TEMP
.\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_tongji tests.test_canvas tests.test_ketangpai
```

- 只跑指定模块，不用 `discover`；同一组连跑两次都绿才算过。
- 断言是契约：不许放宽断言或 skip 来让测试变绿。
- 新增数据源请复用 `receivers/ddl_sync.py` 的底座，别各写一套状态机。

---

## 已知边界

| 边界 | 说明 |
| --- | --- |
| 校外不可达 | 同济所有子系统都是内网地址，必须校园网 / VPN |
| 浏览器 Cookie 读不出来 | Chrome/Edge 158 起用 v20 应用级加密，所以 Canvas 走访问令牌、课堂派走账号换令牌 |
| 教务会话短命 | `sessionid` 只有几小时，过期要重新复制 |
| 接口无公开契约 | 同济没有 OpenAPI 文档，接口随时可能变；变了会如实报错，不会假装成功 |
| 开放平台不对个人开放 | `api.tongji.edu.cn` 需要信息办审批，本项目不绕过，只把它当字段字典 |
| 校园网流量 / 食堂人流 | 没有可用的数据源，不做 |

---

## 致谢

接口能对上，是因为这些开源实现把路径验证过一遍：

- [Cinea4678/Tongji-EasyAPI](https://github.com/Cinea4678/Tongji-EasyAPI) —— 教务 SSO 表单流程
- [JERRIYHAN/tongji-online](https://github.com/JERRIYHAN/tongji-online) —— 各站点接口与字段的活文档
- [ShiGuangSchedule/shiguang_warehouse](https://github.com/ShiGuangSchedule/shiguang_warehouse) —— 课表接口示例
- [KingfuChan/Tongji-CourseTable](https://github.com/KingfuChan/Tongji-CourseTable) —— 课表 → ics
- [Linho1219/tj-ics-proxy](https://github.com/Linho1219/tj-ics-proxy) —— 一网通办 ics 的伪全天问题

图标：[Lucide](https://lucide.dev)（ISC）、[Simple Icons](https://simpleicons.org)（CC0）。

---

## 免责声明

本工具是**个人开发的学习辅助工具**，不是同济大学或任何平台的官方产品，未获授权、认可或背书。

- 截止时间与成绩来自自动抓取与正文解析，**可能出错**。任何重要截止时间以官方页面为准。
- 登录凭据以明文存在本机 `data/` 目录，请勿在公共电脑使用，也不要把该目录分享给别人。
- 接口变更、平台封禁、账号受限的风险由使用者自行承担。
- 请勿用它抓取他人数据、批量爬取，或做任何违反校规与平台条款的事。
- 「1 系统不是你的后花园」—— 保持低频，别对学校服务器发过量请求。

许可证沿用上游：[PolyForm Noncommercial](LICENSE)（非商业使用）。
