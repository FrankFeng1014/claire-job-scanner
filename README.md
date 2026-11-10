# Claire 求职扫描器

每天两次（早晚各一次）自动读取名单里所有雇主的官方招聘系统，再加上 Job Bank、Work In Culture 等公开招聘渠道，找出 Toronto/GTA/Ottawa 新出现的入门岗位，结果存进这个仓库。名单以外的雇主一旦发布了相关职位，会被自动加入名单。

## 它做什么

1. 读取 `data/employers.csv`（雇主名单，来自追踪表）。
2. 按每家的招聘系统调用公开接口，取回全部职位：
   - 支持 Greenhouse、Lever、Ashby、SmartRecruiters、Workable、Workday、
     SAP SuccessFactors（Bell、Scotiabank、Deloitte、TELUS 等）、Phenom（RBC、BMO、PwC 等）、
     Oracle 招聘云（American Express、Metrolinx 等）、Avature（National Bank 等）、Radancy（BlackRock 等）、
     UKG/UltiPro、JazzHR、BambooHR、Amazon，以及列出全部职位的网站地图（KPMG）；
   - 一家公司有几个招聘板（如社招 + 校招）时，`ats_id` 用分号隔开，全部读取；
   - 没有这些系统、但有官方招聘页的，直接读网页上的职位链接和网页里的标准职位数据（博物馆、剧团等）。
3. **名单以外的渠道**（`scanner/channels.py`）：
   - **Work In Culture**（安省艺术文化行业招聘网站）、**Job Bank**（联邦政府招聘网站，按它的要求每 5 秒请求一次）；
   - 可选 **Adzuna**（招聘聚合网站的官方免费接口，需要在仓库 Secrets 里添加 `ADZUNA_APP_ID`、`ADZUNA_APP_KEY`）；
   - 渠道上的职位如果属于名单里的雇主，记在该雇主名下（和它自己系统里的同名职位去重）；
   - 如果雇主**不在名单里**、职位又与 Claire 相关（分析、财务、审计、研究、画廊/博物馆、艺术行政、项目协调等），
     这家雇主会被**自动加入** `data/employers.csv`（`source` 列写 `auto:渠道名`，`added` 列写日期），以后每次都扫描；
     招聘中介、匿名发布、前台/文员类职位不会触发加入。每次最多新增 30 家。
4. 初筛：把职位分成三类并记录原因（规则在 `scanner/filters.py`，可以直接改关键词）：
   - **可能相关**：进入简报主表；
   - **需要核实**：实习、兼职、合同岗、头衔笼统的顾问岗等，简报单列；
   - **明确不相关**：资深、技术、门店/网点、仅限 Co-op 学生、外省等。不相关的职位也会保存（含原因），
     规则改进后运行 `python -m scanner.main reclassify` 可重新分类，不会被永久丢弃。
5. 与上次结果比对，报告**新出现**、**重新出现**和**已下架**的职位。
   某家雇主（或某个渠道）**第一次**被读到时，它已经挂着的职位单独列为“新接入雇主的现有职位”，
   不算新发布、不发通知（否则一接入就是几百条“新岗位”）。

6. 写出：
   - `reports/latest.md` 中文简报（按日期另存一份）；
   - `data/latest.json` 本次运行的新职位清单；
   - `data/recent.json` **最近 7 天**每次运行发现的新职位、新接入职位和新加入的雇主（每天只看一次的
     Claude 定时任务应该读这个文件，按 `found_at` 取上次之后的内容，就不会漏掉中间几次运行的结果）；
   - `data/status.csv` 每家雇主的读取结果，方便找出读不到的公司。

### 关于“下架”判断（重要）

每个雇主每次读取都会标记读取质量（`data/status.csv` 的 `scan_state` 列）：

| scan_state | 含义 | 会不会判定下架 |
|---|---|---|
| complete | 完整读取了整个招聘板 | 会 |
| partial | 只读到一部分（大型 Workday 只读目标地区、分页中断、超过上限） | 不会 |
| unverified | 普通网页，无法确认是否读全 | 不会 |
| zero_jobs | 返回零个职位（可能是接口临时异常） | 不会 |
| failed / invalid | 读取失败 / 数据结构异常 | 不会 |
| not_configured | 没有招聘入口 | — |

同一天运行多次也能正确判断（每次运行有唯一的 `scan_id`）。
**不会抓取 LinkedIn、Indeed 等招聘网站**（它们禁止程序抓取），只读各公司自己的招聘系统。

## 自动运行

`.github/workflows/scan.yml` 让 GitHub Actions **每天两次**自动运行（多伦多时间约早上 6 点半、晚上 6 点半；GitHub 繁忙时可能推迟），也可以在仓库的 **Actions → 每日招聘扫描 → Run workflow** 手动运行。有新岗位或新加入的雇主时，会在当天的 Issue 里通知（同一天第二次运行追加评论）。

每 7 天会先运行一次"招聘系统自动识别"（`scanner/discover.py`）：
- 打开各雇主的官方招聘页，自动找出它用的招聘系统并写回名单；页面上找不到时，顺着“搜索职位”之类的链接再找一层；
- 没有招聘页的雇主，按公司名找官网（域名和网页标题都要对得上），再找官网上的 Careers 页面；
- 只处理上次没读全的雇主，所以能完整读取的雇主会越来越多。

### 目前读不到的情况

有些网站会拒绝程序访问（iCIMS、Dayforce、部分公司官网），或者是政府网站的特殊系统（Ontario Public Service 的 gojobs、联邦 GC Jobs）。它们会出现在简报的“读取失败”或“零职位”里，需要靠 Claude 定时任务的网页搜索或人工查看。Job Bank 渠道能补上其中一部分。

## 第一次运行要知道的

- 第一次运行时，所有现有职位都会被记为"新职位"，这是正常的；从第二次起才是真正的新增。
- 2026-11 升级后第一次运行会自动做一轮招聘系统识别；新能读到的雇主的现有职位会列在“新接入雇主的现有职位”里。
- 看 `data/status.csv` 里 `fail` 的行，就知道哪些公司读不到，可以交给 Claude 修。

## 本地运行（可选）

```bash
pip install -r requirements.txt
python -m pytest -q                 # 离线测试
python -m scanner.discover          # 识别招聘系统，结果在 data/discovered.csv
python -m scanner.main scan         # 扫描全部雇主
python -m scanner.main scan --only onex,eq-bank   # 只扫几家
python -m scanner.main reclassify   # 改了筛选规则后，重新给已有职位分类
```

## 雇主名单格式（data/employers.csv）

| 列 | 说明 |
|---|---|
| id | 与追踪表 employers 的 doc_id 相同 |
| name / tier / sector | 名称、层级、行业 |
| ats | greenhouse / lever / ashby / smartrecruiters / workable / workday，留空则用 careers_url |
| ats_id | Greenhouse/Lever/Ashby 等填账号名；Workday 填 `租户/wdN/站点`，如 `tmx/wd3/TMX_Careers` |
| careers_url | 官方招聘页 |
| source | `original` = 原名单；`auto:jobbank` / `auto:workinculture` 等 = 从外部渠道自动加入 |
| added | 自动加入的日期 |

`ats` 新增可选值：successfactors / phenom / oracle / avature / radancy / ultipro / jazzhr / bamboohr / amazon / sitemap，`ats_id` 的写法见 `scanner/adapters.py` 里各模块的说明。
