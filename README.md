# Zexing Zhang Academic Homepage

这是一个轻量静态个人学术主页。内容主要来自 `data/config.yaml`、`data/papers.bib` 和 `data/preprints.bib`，运行构建脚本后输出到 `output/index.html`。

## 安装依赖

```powershell
py -m pip install -r requirements.txt
```

## 交互式录入

```powershell
py scripts/profile_wizard.py
```

向导启动后顶部显示**状态面板**（姓名、教育/活动条数、论文数 + 缺摘要计数、装饰图数、Codex/Git/Pillow 是否可用、是否有未保存改动）。所有 yaml 改动只动内存，去「构建与发布」菜单或选 `s` 才显示 diff 预览并写盘；改错了选 `q` 直接丢弃。bib/rankings 在自己的子菜单里独立追踪改动，离开子菜单时单独询问保存。

菜单是**分层**的，主菜单只有四类：

```
主菜单
  1  录入与维护
       1  基础信息（姓名 / 链接 / 简介 / 兴趣）
       2  教育经历              [列表精修]
       3  学术活动              [列表精修]
       4  审稿服务
       5  已发表论文 papers.bib  [条目精修 + 联网补摘要]
       6  预印本 preprints.bib   [条目精修 + 联网补摘要]
       7  期刊/会议等级 rankings.yaml
       8  对照编辑中英文        [修正 Codex 翻译]
       0  返回
  2  Codex 智能任务
       1  补全论文摘要
       2  凝练研究方向
       3  翻译中文字段
       4  一键全套（摘要 → 凝练 → 翻译 → 构建）
       0  返回
  3  装饰图库
       1  压缩为 WebP（默认并行 ×8）
       0  返回
  4  构建与发布
       1  重新构建主页
       2  保存改动（不构建）
       3  保存 + 构建（询问开浏览器）
       4  保存 + 构建 + 推送 GitHub        ← 旧数字键 9 直达此项
       0  返回
  s  保存并退出
  q  丢弃修改退出
```

各子菜单中**列表/条目精修**统一支持：编辑某条 / 新增 / 删除 / 上移 / 下移；论文条目额外支持「为某条联网检索摘要」「为所有缺摘要批量检索」。

输入习惯：单行回车保留旧值；多行输入以单独一行 `.` 结束，多行编辑里输入 `!clear` 清空字段；bib 字段输入 `-` 可删除该字段。环境缺 Codex / Git / Pillow 时对应菜单项自动灰显并注明原因。

## 一行命令

只重新构建主页：

```powershell
py build.py
```

完整智能维护：

```powershell
py scripts/profile_wizard.py --codex-auto
```

补全摘要：

```powershell
py scripts/profile_wizard.py --fill-abstracts
```

压缩透明图库（默认并行，可用 `--workers` 指定进程数；0 = 自动取 `min(8, cpu_count)`）：

```powershell
py scripts/profile_wizard.py --compress-gallery
py scripts/profile_wizard.py --compress-gallery --workers 4   # 显式 4 进程
py scripts/profile_wizard.py --compress-gallery --workers 1   # 强制单线程（调试用）
```

只发布当前本地版本到 GitHub：

```powershell
py scripts/profile_wizard.py --publish --commit-message "Update academic homepage"
```

完整智能维护并发布：

```powershell
py scripts/profile_wizard.py --codex-auto --publish --commit-message "Update academic homepage"
```

`--publish` 会执行：保存配置、构建主页、`git add -A`、`git commit -m ...`、`git push`。如果发布前检测到网页仍在使用原始高分辨率图库，脚本会先生成 `gallery_optimized` 并切换网页配置，避免把超大的原图直接发布。加了 `--no-build` 时不会重新运行 `build.py`，首次发布图库时不建议使用。

使用一键推送前，请确认：

- 当前仓库已经配置 `origin` 远端；
- 本机 GitHub 凭据可用；
- 你确实希望把当前工作区改动一起提交。
- 原始高分辨率图库会保留在本地，并由 `.gitignore` 忽略；发布版本使用压缩后的图库。

## 透明装饰素材

把你有权使用和发布的透明 PNG/WebP 放到：

```powershell
output/assets/decorations/gallery/
```

构建时 `build.py` 会扫描图库，网页每次加载会从图库里随机抽取装饰图。如果图库为空，页面不会显示临时占位图。

默认配置：

```yaml
decorations:
  gallery_dir: assets/decorations/gallery
```

如果想使用压缩后的图库，运行：

```powershell
py scripts/profile_wizard.py --compress-gallery
```

脚本会保留原图，输出 WebP 优化版到 `output/assets/decorations/gallery_optimized/`，并默认把网页图库切换到优化目录。压缩使用 `ProcessPoolExecutor`（spawn 模式）并行，默认进程数 = `min(8, cpu_count)`，700+ 张图通常能拿到 4-6 倍加速。

## 论文 / 预印本 / 期刊等级

论文都在 `data/papers.bib` 和 `data/preprints.bib`；venue → tags / color 映射在 `data/rankings.yaml`。三者**都可以在向导里完整维护**（主菜单 → `1` 录入与维护 → `5` / `6` / `7`），不必手编。

**新增论文走引导式流程**，只问真正需要你回答的问题：

1. **标题** — 输入论文标题
2. **venue** — 输缩写（如 `KDD` / `ICASSP`）或全名；若有 Codex 会自动展开成 1-3 个候选全称让你确认
3. **年份** — 4 位数字
4. **你的身份** — 从「第一作者 / 共同一作 / 通讯作者 / 共同通讯 / 合作作者 / 暂不标注」里选
5. **联网检索** — 用标题 + 年份从 Crossref / OpenAlex / Semantic Scholar 抓作者列表 / DOI / 摘要。**作者列表完全靠这一步**：找到就用（你的名字粗体高亮），找不到就留空，**不会强制让你手打**。网页上对应论文的作者位也会跟着不显示，你的「身份」标签仍在。
6. **citekey + 类型自动推断** — 根据作者姓（拿不到就退回到你自己的姓 `info.name.en`）+年份+标题首词生成 citekey（如 `zhang2026ppgpt`）并按 venue 关键词推断 `@article` 还是 `@inproceedings`，让你确认即可

**编辑已有论文**：列表显示 `idx · 年份 · 我的身份 · 摘要 ✔/✘ · venue · 标题`（**不再展示 citekey 和 type 列**），选编号后进入精修子菜单：改标题 / 改 venue（同样支持 Codex 展开，且会建议同步切换 `@article` ↔ `@inproceedings`）/ 改年份 / 改身份 / 联网补摘要 / 高级编辑（全字段，含 author 列表 / DOI / pages 等）/ 改 citekey。

预印本走完全相同的流程。

也可以走一行命令：

```powershell
py scripts/profile_wizard.py --fill-abstracts
```

脚本会优先从 Crossref、OpenAlex、Semantic Scholar 检索摘要；检索不到时会提示你手动输入，不会自动编造。

研究方向可由 Codex CLI 根据简介、关键词、论文题目和已有摘要自动凝练：

```powershell
py scripts/profile_wizard.py --classify-research
```

审稿服务可在交互式向导中选择 `1` → `4` 维护（期刊/会议列表 + 服务角色）。
