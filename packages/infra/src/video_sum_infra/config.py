from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from video_sum_infra.runtime import (
    default_cache_dir,
    default_data_dir,
    default_database_url,
    default_host,
    default_tasks_dir,
)

PREVIOUS_DEFAULT_SUMMARY_SYSTEM_PROMPT = (
    "你是一名严谨的中文视频摘要助手。"
    "你的唯一任务是基于用户提供的转写和分段信息，生成可直接展示给前端页面的结构化摘要。"
    "不得编造视频中没有出现的信息，不得输出 JSON 以外的任何文字。"
    "You must return valid json only."
)

PREVIOUS_DEFAULT_SUMMARY_USER_PROMPT_TEMPLATE = """请阅读下面的视频资料，并输出一个 JSON 对象。
注意：你必须返回合法的 json 对象，且只返回 json。

目标：生成一个可读性强、信息密度高、适合中文用户阅读的视频摘要。

强约束：
1. 必须输出合法 JSON，对象顶层只允许包含 title、overview、bulletPoints、chapters 四个字段。
2. overview 必须是 2 到 4 句中文，概括视频核心观点、讨论主题和最终结论。
3. bulletPoints 必须是 4 到 6 条中文要点，每条 18 到 60 个字，禁止空字符串，禁止重复改写同一条意思。
4. chapters 必须是 3 到 6 个章节，每个章节必须包含 title、start、summary。
5. chapter.title 要短，像小标题；chapter.summary 要概括该时间段内容，20 到 80 个字。
6. start 必须使用视频里真实出现的时间点，单位为秒，按升序排列。
7. 如果原文信息有限，也必须尽量提炼出非空 bulletPoints 和 chapters，不能返回空数组。
8. 不要写“视频主要讲了”“本视频介绍了”这种空话，直接写内容。
9. 不要引用不存在的数据，不要补充外部背景，不要分析说话者身份之外的隐含动机。

写作要求：
- 保持中文自然、紧凑、具体。
- 优先提炼观点、结论、争议点、使用体验、推荐条件。
- chapters 应体现内容推进，而不是机械平均切分。

输出格式示例：
{{"title":"","overview":"","bulletPoints":["", "", "", ""],"chapters":[{{"title":"","start":0,"summary":""}}]}}

视频标题：{title}

转写节选：
{transcript}

分段数据节选：
{segments_json}"""

DEFAULT_SUMMARY_SYSTEM_PROMPT = (
    "你是一名严谨、克制、信息密度优先的中文视频内容编辑。"
    "你的任务不是泛泛总结，而是基于转写和分段信息，产出可以直接用于“知识卡片”页面的结构化内容。"
    "所有内容都必须忠实原文，不得编造，不得补充外部资料，不得输出 JSON 以外的任何文字。"
    "You must return valid json only."
)

DEFAULT_SUMMARY_USER_PROMPT_TEMPLATE = """请阅读下面的视频资料，并输出一个 JSON 对象。
注意：你必须返回合法的 json 对象，且只返回 json。

目标：
生成一个适合详情页展示的结构化摘要，让用户在不看完整视频的情况下，也能快速理解：
1. 这支视频核心在讲什么；
2. 有哪些关键观点、论据、案例、争议和结论；
3. 内容是如何逐步展开的。

强约束：
1. 必须输出合法 JSON，对象顶层只允许包含 title、overview、bulletPoints、chapters、chapterGroups 五个字段。
2. title 必须是简洁、准确的中文标题，避免口号式空话。
3. overview 必须写成 3 到 5 句中文，整体形成一段完整概述：
   - 第 1 句交代主题或讨论对象；
   - 中间句交代关键论点、论据、背景、冲突或方法；
   - 最后 1 句交代结论、判断、影响或最终落点；
   - 总体要具体、完整，适合单独作为“核心概览”展示。
4. bulletPoints 必须是 5 到 8 条中文要点，每条 28 到 88 个字：
   - 每条都要能单独成为一张知识卡片；
   - 优先提炼事实、观点、因果、对比、条件、风险、建议、争议；
   - 不要把同一件事拆成多条近义重复表达；
   - 不要写“作者认为”“视频提到”这类低信息密度前缀，直接写结论。
5. chapters 必须按内容自然分布生成，每个章节必须包含 title、start、summary：
   - chapter.title 要像小标题，短而具体，能体现这一段的主题推进；
   - chapter.summary 必须比普通概述更详细，写成 2 到 3 个短句或 40 到 120 个字，说明这一段具体讲了什么、举了什么例子、得出了什么判断；
   - chapters 应体现内容推进关系，而不是机械平均切分；
   - 章节数量不要预设固定值，应根据内容转折、主题切换、论证层次和视频时长自适应决定；
   - 短视频可以较少章节，长视频或知识密度高的视频应适当增加章节，必要时可达到 9 到 12 个；
   - 只有在确实进入新主题、新问题、新案例或新结论时才切出新章节，不要为了凑数量硬拆。
6. chapterGroups 用来表示“大章节 / 小章节”层级，按真实结构归纳大章节；每个大章节必须包含 title、start、summary、children。
   - chapterGroups.title 必须是有内容的主题名，禁止使用“大章节1”“第一部分”“Part 1”“Section 1”这类占位标题；
   - 当原文层级明显时，大章节数量也应随内容自适应，不要固定成 2 到 4 组；
   - 如果层级不明显，可以少量归并；如果层级明显，可以返回更多组，但不要机械平均分配。
7. start 必须使用视频里真实出现的时间点，单位为秒，按升序排列。
8. 如果原文信息有限，也必须尽量提炼出非空 bulletPoints 和 chapters，不能返回空数组。
9. 不要写“视频主要讲了”“本视频介绍了”“作者首先”这类模板化空话，直接进入信息本体。
10. 不要引用不存在的数据，不要补充外部背景，不要猜测说话者未明确表达的动机。

写作要求：
- 保持中文自然、清楚、具体，避免官话和营销口吻。
- 优先保留高价值信息：定义、判断、证据、例子、条件、限制、影响、结论。
- 如果视频包含多个层次，overview 负责总览，bulletPoints 负责拆出关键结论，chapters 负责还原内容推进，chapterGroups 负责归纳章节层级。
- 标题必须像真实目录项，而不是编号占位符；宁可少而准，也不要为了数量固定而硬拆。

输出格式示例：
{{"title":"","overview":"","bulletPoints":["", "", "", "", ""],"chapters":[{{"title":"","start":0,"summary":""}}],"chapterGroups":[{{"title":"","start":0,"summary":"","children":[{{"title":"","start":0,"summary":""}}]}}]}}

视频标题：{title}

转写节选：
{transcript}

分段数据节选：
{segments_json}"""

DEFAULT_KNOWLEDGE_NOTE_SYSTEM_PROMPT = (
    "你是一名严谨、擅长整理学习型内容的中文知识编辑。"
    "你的任务是基于转写、分段和现有结构化摘要，单独产出一篇适合阅读的知识笔记。"
    "知识笔记必须比知识卡片更完整，能够承担学习、回顾和查阅任务。"
    "所有内容都必须忠实原文，不得编造，不得补充外部资料，不得输出 JSON 以外的任何文字。"
    "You must return valid json only."
)

DEFAULT_KNOWLEDGE_NOTE_USER_PROMPT_TEMPLATE = """请阅读下面的视频资料，并输出一个 JSON 对象。
注意：你必须返回合法的 json 对象，且只返回 json。

目标：
基于原始转写和结构化摘要，生成一篇适合详情页“知识笔记”阅读视图的 Markdown 笔记。

强约束：
1. 顶层只允许包含 knowledgeNoteMarkdown 一个字段。
2. knowledgeNoteMarkdown 必须是一篇完整 Markdown 笔记，不要输出代码围栏包裹整篇内容。
3. 笔记必须明显区别于知识卡片：
   - 不要只是把 bulletPoints 改写一遍；
   - 要有连续叙述、上下文解释、章节展开和重点串联；
   - 允许引用已有结构化摘要，但必须重新组织为适合阅读的笔记。
4. 遇到知识类内容时，优先组织为：核心结论、关键概念、推理/方法、章节展开、易错点/限制。
5. 遇到教程、评论、新闻等非知识类内容时，退化为通用深度笔记：主题概览、关键信息、内容推进、结论/影响。
6. 只有在原文确实涉及公式、符号、函数、逻辑表达式时才使用 LaTeX：
   - 行内公式使用 `$...$`
   - 独立公式使用 `$$...$$`
   - 不要强行输出数学公式。
7. 不要照抄转写全文，不要把原始 transcript 直接拼进笔记主体。
8. 不要补充外部背景，不要编造例子，不要猜测说话者未表达的动机。

写作要求：
- 标题层级清楚，便于长文阅读。
- 保留定义、条件、因果、例子、结论、限制、争议等高价值信息。
- 如果结构化摘要过于简略，应优先参考转写和分段把笔记写得更完整。

输出格式示例：
{{"knowledgeNoteMarkdown":"# 标题\n\n## 核心结论\n\n..."}}

视频标题：
{title}

已有结构化摘要：
{summary_json}

转写节选：
{transcript_excerpt}

分段数据节选：
{segments_excerpt}"""

DEFAULT_MINDMAP_SYSTEM_PROMPT = (
    "你是一名擅长把学习内容重新组织为知识导图的中文内容编辑。"
    "你的任务是基于已有结构化摘要和知识笔记，输出一个适合思维导图展示、信息密度充足、覆盖完整的 JSON 树。"
    "所有内容都必须忠实原文，不得编造，不得补充外部资料，不得输出 JSON 以外的任何文字。"
    "You must return valid json only."
)

DEFAULT_MINDMAP_USER_PROMPT_TEMPLATE = """请阅读下面的视频资料，并输出一个 JSON 对象。
注意：你必须返回合法的 json 对象，且只返回 json。

目标：
把当前视频内容组织成一棵真正“像思维导图”的知识树。它必须以概念、主题、方法、结论之间的关系为核心，而不是把章节标题换个层级重新排列。最末层节点仍然必须能回到原视频片段。

强约束：
1. 顶层只允许包含 title、root、nodes 三个字段。
2. root 必须是整棵导图的根节点 id。
3. nodes 必须是数组，其中包含唯一的根节点；每个节点必须包含：
   - id
   - label
   - type（只能是 root、theme、topic、leaf 之一）
   - summary
   - children
   - time_anchor（仅 leaf 必填，其余可为空）
   - source_chapter_titles
   - source_chapter_starts
4. 整体结构必须是树，不要输出交叉引用；最大深度为 root -> theme -> topic/leaf -> leaf。
5. 顶层 theme 数量应为 4 到 8 个，每个 theme 下应有 3 到 6 个 topic 或 leaf；除非原内容本身很短，否则不要生成过于稀疏的导图。
6. leaf 节点必须能映射到原章节，并带真实时间点；time_anchor 必须取自 source_chapter_starts 中最早的时间点。
7. source_chapter_titles 和 source_chapter_starts 只保留最相关的 1 到 3 项，且数量一致。
8. label 必须是有内容的主题名，禁止“主题1”“Part 1”“Section 1”等占位标题。
9. summary 要适合学习复盘，直接写信息本体，不要重复整段知识笔记；theme/topic 的 summary 尽量写成 2 到 4 句，leaf 的 summary 至少要交代“结论 / 方法 / 条件 / 例子”中的两项。
10. label 和 summary 内如果出现数学内容，优先使用 KaTeX 兼容的 LaTeX 写法，例如 `$\\frac{1}{n}$`、`$(-1)^n$`、`$\\varepsilon$-$N$`；不要输出无法解析的伪公式。
11. 只允许输出 JSON；但 JSON 字符串内部允许包含少量 Markdown 和 `$...$` / `$$...$$` 数学公式。
12. 不要输出空 children 字段以外的多余字段，不要输出解释说明。
13. 不要把 `chapters` 或 `chapterGroups` 直接一一平移成 theme/topic；必须先做语义归纳，再组织层级。
14. 如果多个章节都在讲同一个概念、同一种方法、同一类例子，应该聚合成一个主题，而不是拆成多个并列节点。
15. 导图的每一层都应体现“父主题如何拆成子主题”，而不是简单的时间顺序。

写作要求：
- 优先按“概念定义 / 推导方法 / 典型例子 / 易错点 / 结论判断 / 应用条件”这类知识结构重组。
- 根节点应该是整支视频真正的学习主题，不要只是视频标题原样重复，除非标题本身已经是明确概念。
- theme 层应该是 4 到 8 个最核心的大主题，彼此之间要有明显区分。
- topic 层应承担细化作用，只有当某个 theme 下确实存在两到三类不同子议题时才保留 topic；否则可直接挂 leaf。
- leaf 节点要具体、短促、可点击后立刻看懂，不要写成长句，也不要只是“第X部分”。
- 允许把多个来源章节压缩成一个更抽象、更像脑图节点的表达。
- 如果原文本身是教程或知识讲解，优先提炼“知识结构”；如果原文是评论或资讯，优先提炼“观点结构”和“因果关系”。
- 若视频包含公式、定义、判别条件、证明步骤、典型例题，不要省略它们；应把它们拆成独立主题或叶子节点，而不是只保留一个笼统标题。
- 不要怕信息多，只要层级清楚即可；优先保证“覆盖完整”和“节点可学”，不要只给每个主题一个空泛标签。
- 对于数学/理工类内容，优先把“定义、命题、判别条件、证明思路、典型例题、易错点”拆成不同节点；不要把整段证明压成一句泛泛描述。
- 如果一个 theme 下只生成了 1 个叶子节点，优先继续细化，除非原文确实只讲了这一点。
- 如果知识笔记已经给出分点、例题或条件，你应该把这些信息展开到对应节点，而不是只复述 theme 名称。
- 最终观感要像学习者自己整理出来的脑图，而不是讲稿目录。

输出格式示例：
{{"title":"","root":"root","nodes":[{{"id":"root","label":"","type":"root","summary":"","children":[{{"id":"theme-1","label":"","type":"theme","summary":"","children":[{{"id":"leaf-1","label":"","type":"leaf","summary":"","children":[],"time_anchor":0,"source_chapter_titles":[""],"source_chapter_starts":[0]}}],"source_chapter_titles":[],"source_chapter_starts":[]}}],"source_chapter_titles":[],"source_chapter_starts":[]}}]}}

视频标题：
{title}

已有结构化摘要：
{summary_json}

知识笔记：
{knowledge_note_markdown}
"""

DEFAULT_VISUAL_NOTE_SYSTEM_PROMPT = (
    "你是一名擅长将视频截图与文字深度整合的中文技术编辑。"
    "你的任务是：基于原始知识笔记和画面客观信息，重新组织一篇图文并茂的笔记。"
    "每张截图必须紧跟在它所属的知识段落后面，绝对禁止把所有图片堆在文章末尾。"
    "调整段落结构、合并重复观点、用更精炼的语言重新表达。"
    "禁止使用「画面呈现」「该画面」「上图」「这张图展示」等流水账句式。"
    "只基于提供的笔记和画面解析来写，不编造外部信息。只输出 Markdown 正文。"
)

DEFAULT_VISUAL_NOTE_USER_PROMPT_TEMPLATE = """请生成一版 VLM 理解型图文笔记。你需要用画面的客观信息来补充和深化原文理解。

核心规则（必须遵守）：
1. 【图片紧跟段落】输出结构必须是：段落1 → 图1 → 段落2 → 图2 → ... 这样的交替模式。每写完一个知识点段落，立即插入匹配该段落的截图，然后再写下一个段落。绝对禁止把所有图片堆在文章末尾。
2. 【精选图片】从 visual_observations_json 中只挑选 3-6 张最重要的图片插入。图片贵精不贵多——选那些真正有信息增量的图（公式推导、图表数据、关键界面），跳过纯文字总结或重复画面。
3. 【图片禁止堆积】如果你发现自己写了两个以上的段落还没有插入一张图，说明你正在堆积文字。正确做法是写完一个完整知识点就配图。

写作要求：
4. 以知识点为叙事主线重新编排段落。
5. 精简原文：合并重复表述，提炼核心要点，不搬运长段落。
6. 禁止使用「画面呈现」「这张图展示」「上图」「如下所示」「该画面」等流水账句式。
7. key_facts 和 semantic_summary 是客观参考，转化为自己的语言，不要复制。
8. 图片插入后用 1-2 句自然过渡。
9. 图片链接原样使用 markdown_image，不修改路径和文件名。

视频标题：
{title}

原始知识笔记：
{knowledge_note_markdown}

视觉解析 JSON：
{visual_observations_json}
"""

DEFAULT_VISUAL_FRAME_PLANNING_PROMPT = """请基于视频标题、结构化摘要、知识笔记和分段信息，规划最值得捕获为图文笔记素材的关键画面。

要求：
1. 只选择对理解内容有帮助的画面，例如公式、图表、流程、代码、实验结果、对比截图、关键演示步骤。
2. 不要为了装饰性画面而捕获；若视频以纯口播为主，可以减少候选数量。
3. 最多选择 {max_frames} 张；不要平均抽帧，只选能解释界面、代码、图表、演示、对比或步骤的画面。
4. 每个候选必须给出 timestamp_seconds、anchor_heading、concept、reason、caption_hint、note_hint、priority。
5. timestamp_seconds 必须来自视频真实时间附近，优先贴近对应章节或分段。
6. 只输出 JSON，不要输出解释说明。

输出格式示例：
{{"keyframes":[{{"timestamp_seconds":0,"anchor_heading":"","concept":"","reason":"","caption_hint":"","note_hint":"","priority":0.8}}]}}

视频标题：
{title}

模式：
{mode}

结构化摘要：
{summary_json}

知识笔记：
{knowledge_note_markdown}

分段数据：
{segments_excerpt}
"""

PREVIOUS_DEFAULT_VISUAL_FRAME_PLANNING_PROMPT = """请基于视频标题、结构化摘要、知识笔记和分段信息，规划最值得捕获为图文笔记素材的关键画面。

要求：
1. 只选择对理解内容有帮助的画面，例如公式、图表、流程、代码、实验结果、对比截图、关键演示步骤。
2. 不要为了装饰性画面而捕获；若视频以纯口播为主，可以减少候选数量。
3. 每个候选必须给出 seconds、reason、expected_visual_type、importance。
4. seconds 必须来自视频真实时间附近，优先贴近对应章节或分段。
5. 只输出 JSON，不要输出解释说明。

输出格式示例：
{{"frames":[{{"seconds":0,"reason":"","expected_visual_type":"slide","importance":3}}]}}

视频标题：
{title}

结构化摘要：
{summary_json}

知识笔记：
{knowledge_note_markdown}

分段数据：
{segments_excerpt}
"""

DEFAULT_VISUAL_VLM_PROMPT = """请提取这张视频关键帧中的客观信息，用于后续图文笔记整合。

要求：
1. 提取画面中可见的事实性内容：OCR 文字、图表数据、公式、代码、界面元素、流程步骤、演示关键状态。
2. key_facts 只列画面中直接可见的客观事实，每条一句话，不要评价、不要推测、禁止使用"该画面/本图/上图/图中可以看到"等句式。
3. semantic_summary 用 2-3 句话客观概括画面的核心知识信息，保持陈述语气，不写"展示了/呈现了"。
4. caption 是画面标题标签，不超过 15 字，不是叙述句。
5. 若画面模糊、重复、信息量低或只是人物/过场，importance 设为 1-3，should_insert 设为 false。
6. 不要编造画面外信息，不要推测看不见的内容。
7. 只输出 JSON，不要输出解释说明。

输出格式：
{{"visual_type":"slide/diagram/code/table/demo/person/other","caption":"简洁标签","ocr_text":"","key_facts":["事实1","事实2"],"semantic_summary":"客观知识概述","suggested_anchor":"应归属的章节标题","importance":5,"should_insert":true,"scene":"场景类型","confidence":0.9}}

视频标题：
{title}

画面时间：
{timestamp}

相关上下文：
{context}
"""

PREVIOUS_DEFAULT_VISUAL_VLM_PROMPT = DEFAULT_VISUAL_VLM_PROMPT

LEGACY_SUMMARY_SYSTEM_PROMPT = (
    "你是一名中文视频总结助手。请基于转写内容输出 JSON，包含 title、overview、bulletPoints、chapters。内容必须忠实原文，不要编造。"
)

LEGACY_SUMMARY_USER_PROMPT_TEMPLATE = (
    "请总结下面的视频转写。\n\n转写全文：\n{transcript}\n\n分段（JSON）：\n{segments_json}\n\n"
    "要求：\n1. bulletPoints 返回数组\n2. chapters 返回数组，每项包含 title、start、summary\n3. 输出必须是 JSON"
)

DEVICE_PREFERENCE_ALIASES = {
    "auto": "auto",
    "automatic": "auto",
    "default": "auto",
    "cpu": "cpu",
    "cuda": "cuda",
    "gpu": "cuda",
}

TRANSCRIPTION_PROVIDER_ALIASES = {
    "local": "local",
    "faster-whisper": "local",
    "faster_whisper": "local",
    "whisper": "local",
    "siliconflow": "siliconflow",
    "silicon-flow": "siliconflow",
    "silicon_flow": "siliconflow",
    "multimodal": "multimodal",
    "openai-multimodal": "multimodal",
    "openai_multimodal": "multimodal",
    "funasr": "funasr",
    "fun-asr": "funasr",
    "qwenasr": "funasr",
    "qwen-asr": "funasr",
    "paraformer": "funasr",
}

KNOWLEDGE_LLM_MODE_ALIASES = {
    "same_as_main": "same_as_main",
    "same-as-main": "same_as_main",
    "same": "same_as_main",
    "custom": "custom",
    "independent": "custom",
}

VISUAL_NOTE_MODE_ALIASES = {
    "text": "text",
    "plain": "text",
    "off": "text",
    "disabled": "text",
    "frame_insert": "frame_insert",
    "frame-insert": "frame_insert",
    "frames": "frame_insert",
    "keyframes": "frame_insert",
    "illustrated": "frame_insert",
    "image_insert": "frame_insert",
    "vlm_integrated": "vlm_integrated",
    "vlm-integrated": "vlm_integrated",
    "vlm": "vlm_integrated",
    "integrated": "vlm_integrated",
}

VISUAL_DOWNLOAD_RESOLUTION_ALIASES = {
    "auto": "auto",
    "best": "auto",
    "source": "auto",
    "360": "360p",
    "360p": "360p",
    "480": "480p",
    "480p": "480p",
    "720": "720p",
    "720p": "720p",
    "1080": "1080p",
    "1080p": "1080p",
}

PROMPT_ROUTER_MODE_ALIASES = {
    "auto": "auto",
    "automatic": "auto",
    "confirm": "confirm",
    "manual": "confirm",
    "ask": "confirm",
}


def normalize_device_preference(value: str | None, default: str = "cpu") -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    return DEVICE_PREFERENCE_ALIASES.get(normalized, default)


def normalize_transcription_provider(value: str | None, default: str = "siliconflow") -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    return TRANSCRIPTION_PROVIDER_ALIASES.get(normalized, default)


def normalize_knowledge_llm_mode(value: str | None, default: str = "same_as_main") -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    return KNOWLEDGE_LLM_MODE_ALIASES.get(normalized, default)


def normalize_visual_note_mode(value: str | None, default: str = "text") -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    return VISUAL_NOTE_MODE_ALIASES.get(normalized, default)


def normalize_visual_download_resolution(value: str | None, default: str = "auto") -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    return VISUAL_DOWNLOAD_RESOLUTION_ALIASES.get(normalized, default)


def normalize_prompt_router_mode(value: str | None, default: str = "confirm") -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    return PROMPT_ROUTER_MODE_ALIASES.get(normalized, default)


def normalize_funasr_hub(value: str | None, default: str = "ms") -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    return "hf" if normalized in ("hf", "huggingface") else "ms"


def normalize_knowledge_embedding_provider(value: str | None, default: str = "local_huggingface") -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    if normalized in ("local_huggingface", "huggingface", "hf"):
        return "local_huggingface"
    if normalized in ("local_modelscope", "modelscope", "ms"):
        return "local_modelscope"
    if normalized in ("siliconflow", "silicon-flow", "silicon_flow"):
        return "siliconflow"
    if normalized in ("online", "api"):
        return "online"
    return default


def recommend_task_concurrency(settings: "ServiceSettings", *, cuda_available: bool | None = None) -> int:
    provider = normalize_transcription_provider(settings.transcription_provider)
    if provider == "local":
        return 1
    if cuda_available is True:
        return 2

    runtime_channel = str(settings.runtime_channel or "").strip().lower()
    if runtime_channel.startswith("gpu-"):
        return 2

    device_preference = normalize_device_preference(settings.device_preference)
    if device_preference == "cuda":
        return 2

    whisper_device = str(settings.whisper_device or "").strip().lower()
    if whisper_device == "cuda":
        return 2

    if provider == "siliconflow":
        return 2
    return 1


def recommend_mindmap_concurrency() -> int:
    return 1


def normalize_knowledge_index_auto_rebuild(value: str | None) -> str:
    normalized = str(value or "disabled").strip().lower()
    if normalized in {"on_task_completed", "task_completed", "completed"}:
        return "on_task_completed"
    return "disabled"


class ServiceSettings(BaseSettings):
    host: str = Field(default_factory=default_host)
    port: int = 3838
    data_dir: Path = Field(default_factory=default_data_dir)
    cache_dir: Path = Field(default_factory=default_cache_dir)
    tasks_dir: Path = Field(default_factory=default_tasks_dir)
    database_url: str = Field(default_factory=default_database_url)
    whisper_model: str = "tiny"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    transcription_provider: str = "siliconflow"
    prefer_bilibili_subtitle: bool = True
    device_preference: str = "cpu"
    compute_type: str = "int8"
    model_mode: str = "fixed"
    fixed_model: str = "tiny"
    siliconflow_asr_base_url: str = "https://api.siliconflow.cn/v1"
    siliconflow_asr_model: str = "TeleAI/TeleSpeechASR"
    siliconflow_asr_api_key: str = ""
    siliconflow_asr_chunk_duration_seconds: int = 1800
    siliconflow_asr_concurrency: int = 2
    multimodal_asr_base_url: str = ""
    multimodal_asr_model: str = "mimo-v2-omni"
    multimodal_asr_api_key: str = ""
    multimodal_asr_chunk_duration_seconds: int = 180
    multimodal_asr_max_retries: int = 5
    funasr_model: str = "paraformer-zh"
    funasr_device: str = "cpu"
    funasr_vad_model: str = "fsmn-vad"
    funasr_punc_model: str = "ct-punc"
    funasr_spk_model: str = ""
    funasr_hub: str = "ms"
    funasr_hotword: str = ""
    funasr_available: bool = False
    cuda_variant: str = "cu128"
    runtime_channel: str = "base"
    output_dir: str = ""
    preserve_temp_audio: bool = False
    enable_cache: bool = True
    language: str = "zh"
    summary_mode: str = "llm"
    prompt_router_mode: str = "confirm"
    prompt_presets_path: str = ""
    llm_enabled: bool = False
    auto_generate_mindmap: bool = False
    visual_note_mode: str = "text"
    visual_evidence_enabled: bool = False
    visual_multimodal_enabled: bool = False
    visual_download_resolution: str = "720p"
    visual_evidence_use_llm: bool = True
    visual_vlm_provider: str = "openai-compatible"
    visual_evidence_base_url: str = ""
    visual_evidence_model: str = ""
    visual_evidence_api_key: str = ""
    visual_evidence_max_frames: int = 12
    visual_evidence_frame_interval_seconds: int = 10
    visual_evidence_frame_width: int = 960
    visual_evidence_image_quality: int = 85
    visual_evidence_timeout_seconds: int = 120
    visual_evidence_retry_count: int = 1
    twelvelabs_summary_enabled: bool = False
    twelvelabs_api_key: str = ""
    twelvelabs_model: str = "pegasus1.5"
    twelvelabs_base_url: str = "https://api.twelvelabs.io/v1.3"
    twelvelabs_prompt: str = ""
    llm_provider: str = "openai-compatible"
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    knowledge_llm_mode: str = "same_as_main"
    knowledge_llm_enabled: bool = False
    knowledge_llm_provider: str = "openai-compatible"
    knowledge_llm_api_key: str = ""
    knowledge_llm_base_url: str = ""
    knowledge_llm_model: str = ""
    knowledge_enabled: bool = False
    knowledge_embedding_provider: str = "local_huggingface"
    knowledge_embedding_model: str = "BAAI/bge-small-zh-v1.5"
    hf_endpoint: str = ""
    siliconflow_embedding_api_key: str = ""
    siliconflow_embedding_base_url: str = "https://api.siliconflow.cn/v1"
    siliconflow_embedding_model: str = "BAAI/bge-large-zh-v1.5"
    knowledge_index_auto_rebuild: str = "disabled"
    summary_system_prompt: str = DEFAULT_SUMMARY_SYSTEM_PROMPT
    summary_user_prompt_template: str = DEFAULT_SUMMARY_USER_PROMPT_TEMPLATE
    knowledge_note_system_prompt: str = DEFAULT_KNOWLEDGE_NOTE_SYSTEM_PROMPT
    knowledge_note_user_prompt_template: str = DEFAULT_KNOWLEDGE_NOTE_USER_PROMPT_TEMPLATE
    visual_note_system_prompt: str = DEFAULT_VISUAL_NOTE_SYSTEM_PROMPT
    visual_note_user_prompt_template: str = DEFAULT_VISUAL_NOTE_USER_PROMPT_TEMPLATE
    visual_frame_planning_prompt: str = DEFAULT_VISUAL_FRAME_PLANNING_PROMPT
    visual_vlm_prompt: str = DEFAULT_VISUAL_VLM_PROMPT
    mindmap_system_prompt: str = DEFAULT_MINDMAP_SYSTEM_PROMPT
    mindmap_user_prompt_template: str = DEFAULT_MINDMAP_USER_PROMPT_TEMPLATE
    summary_chunk_target_chars: int = 2200
    summary_chunk_overlap_segments: int = 2
    task_concurrency: int = 2
    mindmap_concurrency: int = 1
    summary_chunk_concurrency: int = 2
    summary_chunk_retry_count: int = 2
    ytdlp_cookies_file: str = ""
    ytdlp_cookies_browser: str = ""

    model_config = SettingsConfigDict(
        env_prefix="VIDEO_SUM_",
        env_file=".env",
        extra="ignore",
    )

    @field_validator("device_preference", mode="before")
    @classmethod
    def _normalize_device_preference(cls, value: str | None) -> str:
        return normalize_device_preference(value)

    @field_validator("transcription_provider", mode="before")
    @classmethod
    def _normalize_transcription_provider(cls, value: str | None) -> str:
        return normalize_transcription_provider(value)

    @field_validator("knowledge_llm_mode", mode="before")
    @classmethod
    def _normalize_knowledge_llm_mode(cls, value: str | None) -> str:
        return normalize_knowledge_llm_mode(value)

    @field_validator("knowledge_embedding_provider", mode="before")
    @classmethod
    def _normalize_knowledge_embedding_provider(cls, value: str | None) -> str:
        return normalize_knowledge_embedding_provider(value)

    @field_validator("visual_note_mode", mode="before")
    @classmethod
    def _normalize_visual_note_mode(cls, value: str | None) -> str:
        return normalize_visual_note_mode(value)

    @field_validator("visual_download_resolution", mode="before")
    @classmethod
    def _normalize_visual_download_resolution(cls, value: str | None) -> str:
        return normalize_visual_download_resolution(value)

    @field_validator("prompt_router_mode", mode="before")
    @classmethod
    def _normalize_prompt_router_mode(cls, value: str | None) -> str:
        return normalize_prompt_router_mode(value)

    @field_validator("knowledge_index_auto_rebuild", mode="before")
    @classmethod
    def _normalize_knowledge_index_auto_rebuild(cls, value: str | None) -> str:
        return normalize_knowledge_index_auto_rebuild(value)

    @field_validator("summary_chunk_overlap_segments", "task_concurrency", "mindmap_concurrency", "summary_chunk_concurrency", "summary_chunk_retry_count", "visual_evidence_max_frames", "visual_evidence_frame_interval_seconds", "visual_evidence_frame_width", "visual_evidence_timeout_seconds", "visual_evidence_retry_count", mode="before")
    @classmethod
    def _coerce_positive_int(cls, value: int | str | None) -> int:
        parsed = int(value or 1)
        return max(1, parsed)

    @field_validator("visual_evidence_image_quality", mode="before")
    @classmethod
    def _coerce_image_quality(cls, value: int | str | None) -> int:
        parsed = int(value or 85)
        return max(1, min(parsed, 100))

    def resolve_whisper_runtime(
        self,
        cuda_available: bool,
    ) -> tuple[str, str, str]:
        if self.model_mode == "auto":
            model = "large-v3-turbo" if cuda_available else "base"
        else:
            model = self.fixed_model or self.whisper_model or "tiny"

        device_preference = normalize_device_preference(self.device_preference)

        if device_preference == "auto":
            device = "cuda" if cuda_available else "cpu"
        elif device_preference == "cuda" and cuda_available:
            device = "cuda"
        elif device_preference == "cuda" and not cuda_available:
            device = "cpu"
        else:
            device = "cpu"

        if self.compute_type == "auto":
            compute_type = "float16" if device == "cuda" else "int8"
        elif device == "cuda" and self.compute_type == "int8":
            compute_type = "int8_float16"
        else:
            compute_type = self.compute_type or self.whisper_compute_type or "int8"

        return model, device, compute_type

    def with_resolved_runtime(self, cuda_available: bool) -> "ServiceSettings":
        model, device, compute_type = self.resolve_whisper_runtime(cuda_available=cuda_available)
        return self.model_copy(
            update={
                "whisper_model": model,
                "whisper_device": device,
                "whisper_compute_type": compute_type,
            }
        )
