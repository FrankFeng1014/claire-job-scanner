# Claire 求职扫描器

每天自动读取名单里所有雇主的官方招聘系统，找出 Toronto/GTA/Ottawa 新出现的入门岗位，结果存进这个仓库。

## 它做什么

1. 读取 `data/employers.csv`（雇主名单，来自追踪表）。
2. 按每家的招聘系统调用公开接口，取回全部职位：
   - 支持 Greenhouse、Lever、Ashby、SmartRecruiters、Workable、Workday；
   - 没有这些系统、但有官方招聘页的，直接读网页上的职位链接（博物馆、剧团等）。
3. 初筛：只留目标地点 + 入门/相关职能的职位（规则在 `scanner/filters.py`，可以直接改关键词）。
4. 与上次结果比对，只报告**新出现**和**已下架**的职位。
5. 写出：
   - `reports/latest.md` 中文简报（按日期另存一份）；
   - `data/latest.json` 新职位清单（供 Claude 定时任务读取、打分、写入追踪表）；
   - `data/status.csv` 每家雇主的读取结果，方便找出读不到的公司。

**不会抓取 LinkedIn、Indeed 等招聘网站**（它们禁止程序抓取），只读各公司自己的招聘系统。

## 每天自动运行

`.github/workflows/scan.yml` 让 GitHub Actions 每天 UTC 11:17 自动运行一次，也可以在仓库的 **Actions → 每日招聘扫描 → Run workflow** 手动运行。

每周一会先运行一次"招聘系统自动识别"（`scanner/discover.py`）：打开各雇主的官方招聘页，自动找出它用的招聘系统并写回名单，这样能读取的雇主会越来越多。

## 第一次运行要知道的

- 第一次运行时，所有现有职位都会被记为"新职位"，这是正常的；从第二次起才是真正的新增。
- 刚开始只有约 30 家雇主已标注招聘系统，另有约 200 家只有招聘页、约 220 家什么都没有。第一次请手动运行并勾选 **discover**，让程序先自动识别一轮。
- 看 `data/status.csv` 里 `fail` 的行，就知道哪些公司读不到，可以交给 Claude 修。

## 本地运行（可选）

```bash
pip install -r requirements.txt
python -m pytest -q                 # 离线测试
python -m scanner.discover          # 识别招聘系统，结果在 data/discovered.csv
python -m scanner.main scan         # 扫描全部雇主
python -m scanner.main scan --only onex,eq-bank   # 只扫几家
```

## 雇主名单格式（data/employers.csv）

| 列 | 说明 |
|---|---|
| id | 与追踪表 employers 的 doc_id 相同 |
| name / tier / sector | 名称、层级、行业 |
| ats | greenhouse / lever / ashby / smartrecruiters / workable / workday，留空则用 careers_url |
| ats_id | Greenhouse/Lever/Ashby 等填账号名；Workday 填 `租户/wdN/站点`，如 `tmx/wd3/TMX_Careers` |
| careers_url | 官方招聘页 |
