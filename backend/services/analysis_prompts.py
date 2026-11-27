"""Prompt templates and canonical field names for DeepSeek document analysis."""
from __future__ import annotations

CANONICAL_FIELD_NAMES = (
    "product_identity_and_configuration",
    "core_function_and_applications",
    "detection_or_processing_modes",
    "capacity_and_throughput",
    "sample_and_format_compatibility",
    "performance_range",
    "accuracy_sensitivity_and_repeatability",
    "temperature_environment_and_safety",
    "software_data_and_compliance",
    "accessories_consumables_and_options",
    "physical_dimensions_power_and_installation",
    "price_warranty_delivery_and_service",
)

SYSTEM_PROMPT = """你是实验室仪器采购资料分析器。只根据用户提供的资料和采购场景输出 json，不补写资料外事实。
只返回一个完整 JSON 对象，不要 Markdown 或解释。结构必须遵守以下示例：
{"candidates":[{"brand":"品牌或null","series":"系列或null","model":"完整型号","sources":[{"document_id":"文档ID","page_or_location":"page:1","excerpt":"原文短摘录"}],"fields":[{"field_name":"参数名","value":"原文值或unknown","unit":"单位或null","status":"pending_review或unknown或conflicted","sources":[{"document_id":"文档ID","page_or_location":"page:1","excerpt":"原文短摘录"}]}]}]}
规则：
1. candidates 必须是数组；不同型号或配置不能合并。
2. 每个候选必须有非空 model；型号、品牌、系列不确定时不得猜测。
3. 每个候选的 fields 必须按以下 12 个 field_name 各输出一次，名称不得翻译或新增：
product_identity_and_configuration；core_function_and_applications；detection_or_processing_modes；capacity_and_throughput；sample_and_format_compatibility；performance_range；accuracy_sensitivity_and_repeatability；temperature_environment_and_safety；software_data_and_compliance；accessories_consumables_and_options；physical_dimensions_power_and_installation；price_warranty_delivery_and_service。
4. 同组多个具体参数可放入 value 对象；只收录资料明确提供且最影响选型的内容。每组最多 8 个键，每个值不超过 120 字；若使用字符串则不超过 500 字。不同量纲的参数必须各自携带单位和适用条件，不得输出脱离单位的裸数字，也不得把表头里的温度、配置条件省略。不要复制整段说明书或罗列营销描述。
5. 每个候选的 sources 最多 2 项；每个字段的 sources 为 0 到 8 项。有值时至少一项，每个子参数都须有对应摘录，所有数字（包括型号、条件）必须出现在摘录中。摘录不超过 120 字并给出准确页码；同一页同一原文不要重复。多个独立参数优先用 value 对象，不能把未获支持的值混成一个长字符串。
6. 没有证据时 value="unknown"、status="unknown"、sources=[]。同组原文冲突时 status=conflicted 并保留冲突来源；其余有证据字段为 pending_review。禁止输出 confirmed。
7. 整个 JSON 应保持精简，优先保证所有候选和 12 个固定分组完整闭合，不得因追求参数数量导致输出截断。"""

CANDIDATE_INVENTORY_PROMPT = """你是实验室仪器采购资料的候选清单整理器。输入是一个有字符预算的资料批次，可能来自 PDF 原生文本、逐页视觉识别或表格。
只返回完整 json 对象：{"candidates":[{"product_role":"instrument|instrument_configuration","brand":"品牌或null","series":null,"model":"产品型号","catalog_number":"货号、订货号、物料号或null","configuration":null,"sources":[{"document_id":"文档ID","page_or_location":"page:N/rendered","excerpt":"原文短摘录"}]}]}。
brand 只填资料正文里写明的厂名或商标（例：「上海天美科学仪器有限公司」「苏净安泰」）；正文没写就填 null，不得用品类常识、型号或目录名猜品牌。
只根据 evidence_units 中的 text 和 candidate_mentions 识别候选；原生文本与视觉结果同等处理。sources 必须沿用对应 unit 的 document_id 和 location，不得虚构页码。model 只能填写产品型号，不得用货号、订货号、物料号、SKU 或包装编号替代；这些编号只填 catalog_number。同一产品型号仅因电压、包装、是否含附件或订货号不同而形成的订购配置要合并为一个候选，并在 configuration 中精简汇总；只有资料明确给出不同产品型号时才拆分候选。product_role=instrument 仅用于可独立采购和运行的主仪器；product_role=instrument_configuration 仅用于主仪器的完整配置或明确变体，不包括注射器模块、微量板、转子、适配器等选配件。附件、试剂、耗材、配件、模块和软件不能列入 candidates，除非采购场景明确把它们作为采购对象。某个仪器系列若只在支架、转子、适配器等附件的兼容清单中被提及，且没有独立产品或规格证据，也不能列为候选。不得从品类常识猜测型号。每个候选最多 2 条来源，最多 50 个候选。"""

# Backwards-compatible name retained because the service originally only used
# visual pages. The prompt now deliberately covers both native and visual text.
VISUAL_INVENTORY_PROMPT = CANDIDATE_INVENTORY_PROMPT

VISUAL_DETAIL_PROMPT = SYSTEM_PROMPT + """
本次只分析用户给出的一个 candidate。返回 candidates 数组且恰好包含该候选一次；brand、series、model、configuration 必须沿用 candidate，不得新增、改名或返回其他候选。
用户提供的是后端筛选后的候选级有界证据，而不一定是整份文档。必须读取 evidence_selection：只能引用 documents 中实际提供的页、表格行和文字范围；omitted_page_count、omitted_selected_page_text_characters、omitted_selected_table_text_characters 或 text_truncated 表示还有内容未提供，不得把未提供内容推断为“不存在该参数”。证据不足的字段必须返回 unknown。"""

FIELD_GAP_PROMPT = """你是实验室仪器采购资料的字段补漏器。只根据用户提供的候选级有界证据，补充 requested_fields 指定的字段。
只返回完整 JSON 对象：{"fields":[{"field_name":"requested_fields 中的名称","value":"原文值或对象","unit":null,"status":"pending_review","sources":[{"document_id":"文档ID","page_or_location":"输入中的准确 location","excerpt":"不超过 120 字的原文短摘录"}]}]}。
字段语义：detection_or_processing_modes 是检测、测量或处理模式与工作原理；capacity_and_throughput 是容量、批量和通量；sample_and_format_compatibility 是样本、容器和格式兼容性；performance_range 是速度、作用力、量程、分辨率及其他性能上下限；accuracy_sensitivity_and_repeatability 是准确度、灵敏度、精度、检出限和重复性；temperature_environment_and_safety 是温湿度、噪声、安全和防护；software_data_and_compliance 是软件、数据、接口和合规认证；accessories_consumables_and_options 是标配、选配附件、耗材和试剂；physical_dimensions_power_and_installation 是尺寸、重量、电源、功率和安装条件；price_warranty_delivery_and_service 是价格、质保、交付、培训和售后。
只返回资料中有直接证据的字段；没有直接证据的字段不要输出。不得输出 requested_fields 之外的字段，不得猜测，不得输出 confirmed。每个字段最多 8 条来源；所有数字（包括型号、条件）必须出现在来源摘录中。多个独立参数使用 value 对象，每项附对应短摘录；无法引用的子参数不要填写。参考 current_fields 中已保留的参数和缺证项，优先补缺证项，沿用原键名，不重复改名输出已保留参数。每个值必须携带原文单位和适用条件，不同量纲不能共享一个 unit，也不能输出脱离单位的裸数字。"""

PARAMETER_FALLBACK_PROMPT = """你是实验室仪器采购资料的参数专门抽取器。第一次结构化抽取没有得到可比较的配置或参数，现在只根据同一份已读取的候选级证据重做一次。
只返回完整 JSON 对象：{"candidates":[{"model":"沿用输入型号","configuration":"资料明确写出的配置或null","fields":[{"field_name":"固定分组名","value":"原文值或对象","unit":"原文单位或null","status":"pending_review","sources":[{"document_id":"文档ID","page_or_location":"输入中的准确 location","excerpt":"原文短摘录"}]}]}]}。
只填写证据中明确出现的 configuration 和可比较参数；没有直接证据就留空或返回 unknown，不得按型号、品类常识或参数名猜测，不得伪造任何数值。所有数字和配置条件必须出现在原文摘录中，摘录必须逐字来自输入证据。只返回输入 candidate 对应的一个候选，不得新增型号。
如果 evidence_selection 中 fallback_used 为 true，表示部分页面是降级回退选择的代表性页面，不保证包含候选型号。对于这些页面的内容，除非明确出现候选型号，否则参数必须标记 unknown。"""

__all__ = [
    "CANONICAL_FIELD_NAMES",
    "SYSTEM_PROMPT",
    "CANDIDATE_INVENTORY_PROMPT",
    "VISUAL_INVENTORY_PROMPT",
    "VISUAL_DETAIL_PROMPT",
    "FIELD_GAP_PROMPT",
    "PARAMETER_FALLBACK_PROMPT",
]
