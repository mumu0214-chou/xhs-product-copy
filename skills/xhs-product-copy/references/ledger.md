# 共用台账与两技能交接（v2）

两个技能各自带有相同的 `scripts/ledger.py`，只依赖 Python 3.9+ 标准库（macOS/Linux）。单独安装任意一个也能读写。它们默认连接 `~/Documents/Codex/小红书笔记台账`；可通过 `XHS_LEDGER_ROOT` 或每次相同的 `--root` 改位置。不要每个产品另开一份台账。

这是技能执行时主动调用的记录流程，不是后台监听器。每个实际阶段完成就写入，交付前读回核对；调用失败须说明未记成功，不声称已经自动追踪。产品资料存在任务快照，技能里不写死产品。

## 文件及职责

- `ledger.json` 是唯一数据源，记录来源、模板、产品标识、任务、使用、稿件版本及事件。禁止手改、覆盖重建。
- `笔记台账.md` 和 `handoffs/<task_id>.json` 是可重建视图；交接文件包括本任务事实、必写项、已读原文、迁移理由、稳定编号、已写稿及相关反馈。
- `artifacts/<内容哈希>.md` 是不可覆盖的完整稿件，路径相对台账根目录。修改通过新版本提交，不直接编辑旧文件。
- `ledger.json.bak` 为最近一次写入前备份；首次升级另外保存 `ledger.v1.backup.json`。旧稿只有聊天位置的保留原记录，不假造完整稿、精确时间或拆成虚构的版本。
- `风格参照.md`、`风格参照证据.json` 是可选的实际历史参照，脚本不会改它们。随包 `style-baseline.md` 保证没有旧台账也能开始工作。

查找技能负责 start/select、模板复查、选择反馈。写作技能负责 drafts、稿件反馈、完成状态。谁收到明确反馈谁记录，不需要让用户再启动另一技能。两个技能都读取 query：查找排除旧来源，写作读取适用的反馈。只找就记录 0 篇母稿；只写可用 original 登记，不能伪称搜过来源。

## 常用命令

在当前技能目录运行，所有输入路径用绝对路径：

```sh
python3 scripts/ledger.py show
python3 scripts/ledger.py query
python3 scripts/ledger.py record --input /absolute/path/operation.json
python3 scripts/ledger.py handoff --task TASK_ID
python3 scripts/ledger.py render
```

`show` 返回计数与任务进度，`query` 返回已交付／已用来源 ID、模板使用次数、具体反馈与复查情况。默认排除旧原文；用户明确复用时才填 `reuse_reason`，不能为绕过去重随意填写。短链先通过实际页面解析成原笔记链接。不同追踪参数的同一笔记只有一个来源；不同 ID 的相同全文也拒绝重复。近似文案仍需人工判断。

首次写入自动将 v1 升到 v2；也可用 `migrate`。旧的任意 `merge` 接口不再使用，避免列表替换覆盖裂变历史。`record` 一次提交一个操作；它锁住文件、读最新状态再写入，防止两个聊天互相覆盖。

## 记录操作

每次动作准备 JSON，包含 `event_id`、`op` 和下面的字段。ID 用字母数字及 `-_.`，例如任务 `run-日期-简短随机码`、使用 `任务-m01`。同一动作重试用**原 event_id 和原内容**；脚本不重记。改了内容就用新事件，稿件仍沿用原 draft ID 并递增版本。不要每次重试新建任务或 use。

### 1. start：任务事实与计划

```json
{
  "event_id": "run-demo-start", "op": "start", "task_id": "run-demo",
  "product": {"id": "product-demo", "name": "当次产品名称"},
  "brief": {
    "facts": ["当次确认的事实"],
    "mandatory": ["每篇必写项"],
    "optional": ["按主题使用的信息"],
    "unknown": [], "evidence": "当前用户说明的位置",
    "scope": "找三篇、改三篇、首篇裂变十篇"
  },
  "expected": {"selections": 3, "mothers": 3, "variants_by_use": {"run-demo-m01": 10}}
}
```

计划是数量目标，不是已完成数量。只找时 mothers=0、variants_by_use={}；只原创时 selections=0。产品简要登记只存标识和名称，完整资料是本次任务快照，不覆盖其他任务商品事实。用户改变价格／规格时新开任务并引用前一任务，避免老稿被新事实悄悄重写。

### 2. select：一条选中参考和一个使用编号

包含 `task_id`、`use_id`，以及：

```json
{
  "source": {
    "url": "实际打开或可靠历史记录中的原文链接",
    "title": "实际标题", "author": "可见作者",
    "industry": "来源行业", "reading_scope": "full_text",
    "source_text": "实际读过的正文，不补写未取得内容",
    "read_at": "实际阅读日期", "provenance": "当前页面或历史文件位置",
    "query": "检索词", "published_at": null
  },
  "template": {"id": "pattern-id", "name": "模板名称", "structure": "开头→话题→商品转场→收尾"},
  "selection": {
    "source_voice": "merchant",
    "borrowed_excerpt": "值得借的短片段", "excerpt_location": "开头",
    "attraction": "原文为什么吸引人", "product_bridge": "当前事实怎样承接",
    "non_transferable": "不能搬用的原文事实", "pattern": "本篇结构",
    "novelty": "新写法／旧模板的新样本；依据",
    "reused_parts": "借用范围"
  }
}
```

这只是字段片段，实际输入还须 event_id、op=select、task_id、use_id。source ID 由链接提取，不凭空造。阅读范围还可为 caption_only/title_cover_only；此时必须如实限定交付与迁移范围。消费者视角须另填 role_conversion。已交付／已用原文默认拒绝再次入选。新旧模板的判断由 AI 根据结构和历史完成，脚本不会把新来源自动当新模板。

选择完成后，读取 `handoffs/<task_id>.json` 交给写作技能；不得只交标题／链接。用户只找时随后 finish；找并写时直接接续。若写作发现不能迁移，用 withdraw（task_id、use_id、reason）撤回尚未写稿的选择，由查找阶段补选；原记录保留但不计入任务有效选择数。AI 自己的判断不能冒充用户反馈。已有稿件不能撤销掉历史；另开使用记录处理。

### 3. original：无对标的直接写作

字段 `task_id`、`use_id`、`topic`。它建立 source_id/template_id 为 null 的使用记录，供写作，不创建虚构的来源。

### 4. drafts：母稿、裂变与修订

```json
{
  "event_id": "run-demo-write-01", "op": "drafts", "task_id": "run-demo",
  "drafts": [{
    "id": "run-demo-m01--mother", "use_id": "run-demo-m01", "kind": "mother",
    "expected_revision": 0, "title": "完整标题", "body": "完整正文"
  }]
}
```

母稿 ID 固定 `use_id--mother`；裂变用 `use_id--v001` 等，kind=variant，另填 `mother_revision` 表示基于哪个母稿版本。新增 expected_revision=0；修改先读最新版本，用其版本号作为 expected_revision。冲突说明有别处更新，读回再改，不能盲目递增重试。

同批可以有多篇，但只提交实际完整写出的稿。续写从下一个未用编号开始；同一篇修改仍用原 ID。稿件保存、数量更新和事件登记由命令完成。语气／事实／必写项仍由写作技能逐篇检查，脚本只验证结构、引用、版本和完全重复，不能证明文案质量。

修改母稿后不会偷偷重写旧裂变；每篇保留 mother_revision，后续需要同步时明确修订。一份母稿裂变 10 篇，模板仍只算一次使用；修订也不增加独立使用次数。

### 5. feedback：明确且限定对象的用户反馈

字段：`status` 为 approved/rejected/revise，`quote` 为用户原话，`evidence` 为聊天或文件位置，`targets` 为对象列表。

- 稿件：`{"kind":"draft","id":"稿件ID","revision":1}`，必须对应实际版本。
- 参考选择：`{"kind":"selection","id":"use_id"}`。
- 模板或来源：kind 为 template/source，id 为相应 ID。
- 对一组裂变的反馈：枚举实际所指稿件及版本；不能自动扩成母稿或整类模板获认可。

更新后的稿件反馈默认未知，不能继承旧版本的认可。沉默、完成生成、AI 自评通过都不算用户认可。模糊反馈先结合最近交付判断具体对象；仍不清楚就保持未知。

### 6. review-template / publication / finish

- `review-template`：template_id、status（新候选／认可过待复查／本轮适用／降低优先级）、reason、evidence。依据实际检索与用户反馈复查，不以裂变数认定优秀，也不按固定天数判过期。历史留在事件里。
- `publication`：draft_id、revision、evidence，仅记录用户提供或实际可见的已发布证据，命令不会发布。生成不等于发布，认可也不等于成交；效果指标若有写进 evidence，不凭互动推断销量。
- `plan`：task_id、expected（同 start 结构）、reason。用于用户改变数量目标或替换参考后调整裂变目标编号；不得为了假装完成而降低目标。原计划保留在事件中。
- `finish`：task_id；数量达到目标记 complete，否则必须 reason 并记 partial。先记录实际成果，不为凑完成状态虚增数量。读回 show/handoff，核对实际与计划、版本、来源、产品和未知反馈。

## 数据迁移与恢复

两个技能更新到 GitHub，只更新方法、参照和程序；不会上传私人台账、产品快照、原文全文或稿件。跨设备可手动导出：

```sh
python3 scripts/ledger.py export --output /absolute/path/new-export-directory
```

导出包含 ledger、完整新稿、交接文件和已有风格资料。另一设备将此目录放到默认位置或指定同一个 `--root`，再 render 即可；不要覆盖另一份已有台账。旧历史里指向外部文件／聊天的路径不会自动变成可用资料。跨设备**自动同步与两份独立台账合并不在本工具范围内**。

中断后重新用同一事件重试可恢复派生视图；render 可重建视图和新稿文件。备份恢复须先确认保留最新数据，不能静默用备份覆盖后续工作。
