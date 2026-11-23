# GitHub 资料同步

公开仓库提供工作台源码、依赖、校验脚本和数据规范。平台维护者管理源码写权限。共享资料存入团队指定的私有 GitHub 仓库，成员的资料权限由该仓库独立管理。工作台的运行记录、草稿、附件、密钥和 SQLite 数据库保留在本机。

资料仓库以 `main` 为默认分支。新建资料仓库时，可先用 `README.md` 初始化 `main`。共享记录按官方 L2 编码存于 `data/domains/`，一个文件对应一条信源、文献、企业关系、工序或题目。原件按来源 URL 获取，派生索引存于 `data/_index/`。工作台将已同步的记录投影到本机 SQLite，并保存对应仓库和提交。分别备份本机数据库与私有资料仓库，即可分别保存个人运行历史和团队发布历史。 [元数据目录约定](metadata-records.md)

## 配置资料仓库

启动工作台后打开「资料同步」，填写私有资料仓库的 `owner/repository`，保存并测试连接。成员可使用自己的 GitHub CLI 登录会话。

~~~bash
gh auth login --hostname github.com --git-protocol https --web
~~~

读取需要资料仓库访问权限，发布需要写权限。工作台在 `.local/` 下独立维护资料仓库的 Git 对象。平台的 `git pull` 更新源码，工作台的「资料同步」更新共享资料。留空时，工作台以本地模式运行。

## 本机协作

侧栏「资料同步」右侧的刷新图标将私有资料仓库的 `main` 写入本机共享视图。点击「资料同步」打开发布抽屉，勾选已完成任务，再点击「校验并推送」。研究指挥台上的任务结果也可直接打开抽屉并选中本次任务。同步保留平台工作树、暂存区和运行中的对话。

任务资料先经过本机格式、官方 L2、路径、密钥和文件大小校验。工作台再拉取最新 main，按记录身份去重，为每项任务创建一个快进提交。并发新增时保留先提交的共享记录。如果任务修改了开始时已存在的资料，发布前要填写纠错说明，远端出现同记录的新版本时停止发布。旧任务没有开工时的基线，继续按新增批次去重。抽屉显示最近资料提交，可直接核对改动。

同步队列保存在 SQLite，关闭浏览器后由本机服务继续处理。在「资料同步」可测试仓库连通性及设置自动入库、自动发布和运行产物忽略规则。自动发布适用于在当前资料仓库启用后新建且具备共享产物的任务。全量重建仅替换本机共享视图。维护者可用 `python3 -m industplatform start --maintain` 定期生成共享索引。

本地 Agent 可用同一个同步入口。省略 --run-id 即只拉取，重复给出 --run-id 可发布多项。

~~~bash
python3 -m industplatform sync --repository owner/repository
python3 -m industplatform sync
python3 -m industplatform sync --run-id "$RUN_ID"
python3 -m industplatform sync --run-id "$RUN_ID" --correction-reason '核对新版规范后修正适用范围'
python3 -m industplatform sync --status
~~~

同步读取资料仓库的已提交内容，根据上次成功同步的提交更新新增、修订和废止记录。发生校验错误时，事务保留上次成功的视图。更换资料仓库会清空旧的共享视图，保留本机任务、草稿和附件。旧队列需要重新提交。带有其他仓库基线或来源不明的旧基线的任务，需要在正确的资料仓库同步后重新运行。已分类的 Agent 按 L2 和工程对象查阅共享线索，较早的本机任务按领域名查询。

将本地工作数据放在 `.local/`、`.staging/` 或仓库外。验收数据和截图可分别放在 `test-data/` 与 `screenshots/`。这些目录已列入平台仓库的 `.gitignore`。工作台发布前检查受限路径和常见密钥格式。

## 导入已有本地资料

打开「资料同步 → 批量导入」，选择多个 HTML、PDF、CSV 文件，或选取一个文件夹。单个文件缺少来源链接时，可填写准确的公开 HTTPS 原文。每批最多 50 个文件，单个最多 16 MiB，合计最多 128 MiB，支持最多 5,000 条去重后的链接。HTML 支持页面链接及 `application/json` 内嵌目录的来源字段。PDF 使用 PATH 上的 Poppler `pdftotext` 提取文本，使用 `pdfinfo` 提取内嵌超链接。扫描件需要先做文字识别，只有链接时仍可登记线索。

点击「整理并入库」后，工作台在本机提取信源。Codex 通过所配置的模型逐条归类，模型只接收经过筛查的标题、主题和不含查询参数的公开域名及路径。原件和正文留在本机。整理完毕后按官方 L2 查重并写入本机资料库。研究指挥台的「历史任务」显示进度和入库回执，条目在关联领域的「信源与文献 → 本地导入」中标为待复核。「工艺图谱」按生命周期汇总导入信源，可点击阶段查看对应资料。

需要团队共享时，查看结果并勾选待发布批次，再点击「校验并推送」。本机门禁基于最新 main 去重，按单批次单提交快进推送。共享目录只接收标准 JSON。本地入库和团队发布分别操作。

提交代码时使用 feat/ 或 fix/。维护脚本、仓库配置、文档与忽略规则使用 chore/。开发依赖和验收命令如下。

~~~bash
python3 -m pip install -r requirements-dev.txt
python3 scripts/check_repo_hygiene.py
python3 scripts/validate_data_schemas.py
python3 -m ruff check --select E9,F63,F7,F82 industplatform scripts tests
python3 -m mypy --follow-imports=silent --check-untyped-defs scripts/repo_support.py industplatform/shared_metadata.py industplatform/taxonomy.py
python3 -m pytest -q
~~~

## 完成一个采集任务

在工作台运行所选阶段。阶段 1 保存官方 L2、工程对象标识和适用生命周期，导出按任务的分类写入。工作台只导出本次任务的共享元数据，包括出处和可公开的工程条款。可用下面的命令在 .staging 预览导出内容。

~~~bash
RUN_ID='替换为已完成的运行 ID'
python3 scripts/export_completed_task.py --run-id "$RUN_ID"
git status --untracked-files=all --short
python3 scripts/check_repo_hygiene.py
python3 -m industplatform sync --run-id "$RUN_ID"
~~~

没有保存过分类的旧任务，可在导出时一次补齐这三项。以下以电子元件、PCB 与电子组装为例。检查实际工序后选择对应的工程对象和生命周期。

~~~bash
python3 scripts/export_completed_task.py --run-id "$RUN_ID" \
  --l2-code D01.2 --object-cluster pcb-assembly --lifecycle-stages S5
~~~

官方 L2 码表使用《工业工程数字工作覆盖图谱》公开展示的编号，覆盖 D01–D11 共 58 个 L2，含 D11.1 跨行业工程支撑。生命周期独立枚举为 S0–S8。L3/L4 工程对象和场景可由 Agent 按实际范围细化。Agent 先核对同一 L2 的既有对象和覆盖提示，再复用同义标识或登记新标识。维护者在平台源码仓库更新码表和提示文件。 [L2 码表](../schemas/taxonomy_l2_registry.json) · [生命周期](../schemas/lifecycle_stages.json) · [L3/L4 提示](../schemas/taxonomy_l3_l4_hints.json)

一个任务的记录共用 `task_id`。新资料从 `active v1` 开始。`canonical_id` 取身份字段的 SHA-256 前 24 位。信源、文献按 L2、类型和规范化 URL 去重。企业关系、工序和题目在 L2、类型、实体键之外使用 `object_cluster` 区分。

## 纠错、撤回和归档

发布纠错批次时填写纠错说明。修订递增 `version`，保留 `correction_reason`。废止标记为 `deprecated`，存在替代记录时填写 `superseded_by`。遇到远端同一条记录的新版本，重新核对原文、条款与适用范围后再发布。

要撤回一次采集，从「资料同步」最近提交中找到该任务的提交 SHA，再用下列命令提交撤回。新增记录会被废止，旧记录的修改会恢复为新版本。如果后续任务已经更新了同一条记录，回滚会给出冲突条目。

~~~bash
TASK_SHA='替换为待撤回的任务提交'
python3 -m industplatform sync --rollback "$TASK_SHA" --correction-reason '该批次使用了错误的产品范围'
~~~

归档时，在独立的资料仓库检出中调用平台仓库的脚本，可从已提交的领域生成 `.staging/archives/` 下的 JSONL 或 `.jsonl.zst`。压缩模式需要系统 `zstd`。

~~~bash
python3 /path/to/industry-platform-workbench/scripts/archive_domain_rollup.py D01.2 --ref main --format zst
~~~
