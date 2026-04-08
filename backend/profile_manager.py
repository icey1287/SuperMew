import os
import json
import base64
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

try:
    import fitz  # PyMuPDF：PDF 转页面图（火山等 API 仅支持 image_url，不接受 file/pdf 直传）
    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR.parent / "data"
PROFILE_DIR = DATA_DIR / "profiles"


class DetailedTestResult(BaseModel):
    item_name: str = Field(default="", description="与报告原文一致的检验项目名称，一条对应报告上一行")
    result: str = Field(default="", description="结果值，如'1.2'")
    unit: str = Field(default="", description="单位，如'# mol/L'")
    reference_range: str = Field(default="", description="参考区间，如'0.0~10.0'")
    abnormal: str = Field(default="", description="是否异常（偏高/偏低/正常），如根据参考值判断")
    record_date: str = Field(default="", description="该化验项目的出具日期，如'2023-10-15'")


class MedicalRecordEntry(BaseModel):
    """病历夹中的单条独立报告（一次上传一份）"""

    id: str = Field(default="", description="记录唯一 ID")
    order_category: str = Field(
        default="",
        description="医嘱项目/报告种类，如：血常规、生化全项、凝血、肿瘤标志物、CT、MRI、病理等",
    )
    report_date: str = Field(default="", description="本张报告的开具日期或报告日期")
    source_filename: str = Field(default="", description="上传时的文件名")
    created_at: str = Field(default="", description="入库时间 ISO")
    name: str = Field(default="", description="患者姓名")
    age: str = Field(default="", description="年龄")
    gender: str = Field(default="", description="性别")
    diagnosis: str = Field(default="", description="主要诊断")
    stage: str = Field(default="", description="分期")
    treatment_history: str = Field(default="", description="治疗史（本报告相关或摘录）")
    lab_results: str = Field(
        default="",
        description="本报告检验概况文字；逐条枚举以 test_items 为准",
    )
    test_items: list[DetailedTestResult] = Field(default_factory=list)
    current_status: str = Field(default="", description="当前病情或症状（本报告语境下）")
    medical_summary: str = Field(
        default="",
        description="本条病历独立存储的简要记忆/摘要；夹级 Agent 长记忆由各条 medical_summary 按需拼接，删除本条即不再参与汇总",
    )
    suggested_questions: list[str] = Field(default_factory=list)


class DischargeFollowUpItem(BaseModel):
    """出院医嘱中拆出的单次复诊/处置（须含日期，供日历与提醒）"""

    visit_date: str = Field(
        default="",
        description="就诊/复查/办理日期，YYYY-MM-DD；原文为相对日期时请结合 report_date 推算",
    )
    item_title: str = Field(default="", description="事项名称，如 肿瘤科门诊、抽血、化疗、换药、拆线")
    detail: str = Field(default="", description="具体要求、科室、注意事项")
    raw_excerpt: str = Field(default="", description="医嘱原文摘录（可选）")


class DischargeReportEntry(BaseModel):
    """单次上传的出院报告（独立存储，不并入检验病历 records）"""

    id: str = Field(default="", description="记录 ID")
    report_date: str = Field(default="", description="出院日期或报告日期")
    hospital_name: str = Field(default="", description="医院名称")
    department: str = Field(default="", description="科室")
    source_filename: str = Field(default="", description="上传文件名")
    created_at: str = Field(default="", description="入库时间 ISO")
    name: str = Field(default="", description="患者姓名")
    diagnosis: str = Field(default="", description="出院诊断摘要")
    discharge_summary: str = Field(default="", description="住院经过/出院病情摘要")
    discharge_orders_full_text: str = Field(
        default="",
        description="出院医嘱全文（图像可见范围内尽量完整转写）",
    )
    follow_up_items: list[DischargeFollowUpItem] = Field(
        default_factory=list,
        description="从出院医嘱中提取的每一条含日期的随访/复查/治疗安排",
    )
    medication_notes: str = Field(default="", description="带药及用药说明摘录")
    other_instructions: str = Field(default="", description="其它说明")
    parse_notes: str = Field(default="", description="解析备注")


class UserMedicalFolder(BaseModel):
    """用户病历夹：检验病历 records + 出院报告 discharge_reports"""

    schema_version: int = 2
    name: str = Field(default="", description="患者姓名（可与最近一条报告同步）")
    age: str = Field(default="")
    gender: str = Field(default="")
    record_date: str = Field(default="", description="兼容旧字段：可存最近报告日期")
    medical_summary: str = Field(
        default="",
        description="面向对话 Agent 的汇总长记忆：由病历摘要与出院随访日程拼接，勿手写",
    )
    records: list[MedicalRecordEntry] = Field(default_factory=list)
    discharge_reports: list[DischargeReportEntry] = Field(default_factory=list)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def migrate_raw_profile_to_folder(raw: dict[str, Any]) -> dict[str, Any]:
    """将 v1 单对象档案转为病历夹结构。"""
    if not raw:
        return UserMedicalFolder().model_dump()
    if raw.get("schema_version") == 2 and isinstance(raw.get("records"), list):
        return UserMedicalFolder(**raw).model_dump()

    rid = raw.get("legacy_id") or str(uuid.uuid4())
    entry = MedicalRecordEntry(
        id=rid,
        order_category=raw.get("order_category") or "",
        report_date=raw.get("record_date") or "",
        source_filename=raw.get("source_filename") or "",
        created_at=raw.get("created_at") or "",
        name=raw.get("name") or "",
        age=raw.get("age") or "",
        gender=raw.get("gender") or "",
        diagnosis=raw.get("diagnosis") or "",
        stage=raw.get("stage") or "",
        treatment_history=raw.get("treatment_history") or "",
        lab_results=raw.get("lab_results") or "",
        test_items=raw.get("test_items") or [],
        current_status=raw.get("current_status") or "",
        medical_summary=raw.get("medical_summary") or "",
        suggested_questions=raw.get("suggested_questions") or [],
    )
    folder = UserMedicalFolder(
        name=raw.get("name") or "",
        age=raw.get("age") or "",
        gender=raw.get("gender") or "",
        record_date=raw.get("record_date") or "",
        medical_summary=raw.get("medical_summary") or "",
        records=[entry],
    )
    return folder.model_dump()


def _ensure_record_ids(folder: dict[str, Any]) -> bool:
    """为缺少 id 的病历条补唯一 id（旧数据/手工编辑可能导致 id 为空）。返回是否修改。"""
    changed = False
    for r in folder.get("records") or []:
        if isinstance(r, dict) and not str(r.get("id", "")).strip():
            r["id"] = str(uuid.uuid4())
            changed = True
    return changed


def _ensure_discharge_report_ids(folder: dict[str, Any]) -> bool:
    """为出院报告条补 id。"""
    changed = False
    for r in folder.get("discharge_reports") or []:
        if isinstance(r, dict) and not str(r.get("id", "")).strip():
            r["id"] = str(uuid.uuid4())
            changed = True
    return changed


def sync_folder_demographics_from_last_record(folder: dict[str, Any]) -> None:
    """夹级姓名/年龄等与「当前最后一条」病历对齐；无记录时清空。"""
    recs = folder.get("records") or []
    if not recs:
        folder["name"] = ""
        folder["age"] = ""
        folder["gender"] = ""
        folder["record_date"] = ""
        return
    last = recs[-1]
    if not isinstance(last, dict):
        return
    for key in ("name", "age", "gender"):
        v = last.get(key)
        if v:
            folder[key] = v
    if last.get("report_date"):
        folder["record_date"] = last["report_date"]


def build_folder_medical_summary(folder: dict[str, Any]) -> str:
    """夹级长记忆：检验病历摘要 + 出院随访日程（供 Agent）；无内容时为空。"""
    parts: list[str] = []
    recs = folder.get("records") or []
    for r in recs:
        oc = (r.get("order_category") or "").strip() or "未分类报告"
        rd = (r.get("report_date") or "").strip() or "日期待定"
        ms = (r.get("medical_summary") or "").strip()
        if ms:
            parts.append(f"【{oc} · {rd}】\n{ms}")

    for dr in folder.get("discharge_reports") or []:
        if not isinstance(dr, dict):
            continue
        rd = (dr.get("report_date") or "").strip()
        lines: list[str] = []
        for it in dr.get("follow_up_items") or []:
            if not isinstance(it, dict):
                continue
            vd = (it.get("visit_date") or "").strip()
            if not vd:
                continue
            title = (it.get("item_title") or "复诊").strip()
            det = (it.get("detail") or "").strip()
            line = f"- {vd} {title}"
            if det:
                line += f"：{det}"
            lines.append(line)
        if lines:
            head = f"【出院随访日程 · {rd or '出院报告'}】"
            parts.append(head + "\n" + "\n".join(lines))

    return "\n\n".join(parts) if parts else ""


# API / 旧代码兼容别名
PatientProfile = UserMedicalFolder


class ProfileManager:
    """患者病历夹：每次上传生成独立记录；PDF 按页渲染为 image_url。"""

    _MAX_PDF_PAGES = 10
    _MAX_DISCHARGE_PDF_PAGES = 15

    def __init__(self):
        os.makedirs(PROFILE_DIR, exist_ok=True)
        self.api_key = os.getenv("ARK_API_KEY")
        self.model_name = os.getenv("MODEL")
        self.base_url = os.getenv("BASE_URL")

    def _get_llm(self):
        return init_chat_model(
            model=self.model_name,
            model_provider="openai",
            api_key=self.api_key,
            base_url=self.base_url,
            temperature=0,
        )

    def _image_url_block(self, media_type: str, raw: bytes) -> dict:
        b64 = base64.b64encode(raw).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{b64}"},
        }

    def _attachment_blocks_for_llm(
        self, file_path: str, filename: str, max_pages: int | None = None
    ) -> list[dict]:
        path = Path(file_path)
        suffix = path.suffix.lower()
        page_limit = max_pages if max_pages is not None else self._MAX_PDF_PAGES

        if suffix == ".pdf":
            if not HAS_FITZ:
                raise RuntimeError(
                    "处理 PDF 病历需要 PyMuPDF：请执行 pip install PyMuPDF"
                )
            blocks: list[dict] = []
            try:
                doc = fitz.open(file_path)
            except Exception as e:
                raise RuntimeError(f"无法打开 PDF：{e}") from e
            try:
                n = min(len(doc), page_limit)
                for i in range(n):
                    page = doc[i]
                    pix = page.get_pixmap(dpi=150)
                    img_data = pix.tobytes("jpeg")
                    blocks.append(self._image_url_block("image/jpeg", img_data))
            finally:
                doc.close()
            if not blocks:
                raise RuntimeError("PDF 未得到任何页面图像")
            return blocks

        try:
            raw = path.read_bytes()
        except OSError as e:
            raise RuntimeError(f"无法读取文件: {e}") from e

        if suffix in (".jpg", ".jpeg"):
            mt = "image/jpeg"
        elif suffix == ".png":
            mt = "image/png"
        else:
            raise ValueError(f"不支持的病历文件类型: {suffix}（请使用 PDF 或 JPG/PNG）")

        return [self._image_url_block(mt, raw)]

    def _parse_llm_json_template(self) -> str:
        return (
            "JSON 必须严格包含以下字段（本次仅针对**当前这一份**上传资料，不要合并历史其它报告）：\n"
            "{\n"
            '  "order_category": "医嘱项目/报告种类（必填）：如 血常规、生化全项、凝血四项、肿瘤标志物、胸部CT、病理等，按报告抬头或检查单类型归纳",\n'
            '  "report_date": "本报告的报告日期或开具日期（例如：2023-10-15）",\n'
            '  "name": "患者姓名（如病历中包含）",\n'
            '  "age": "患者年龄",\n'
            '  "gender": "患者性别",\n'
            '  "diagnosis": "主要诊断（例如：鼻咽癌）",\n'
            '  "stage": "肿瘤分期（如：TNM分期、临床分期）",\n'
            '  "treatment_history": "与本报告相关的治疗史摘录",\n'
            '  "lab_results": "本报告检验/检查结果的概括性文字（不可省略 test_items 中的任何一条）",\n'
            '  "test_items": [{"item_name": "与报告完全一致的项目名", "result": "结果值", "unit": "单位", "reference_range": "参考区间", "abnormal": "偏高/偏低/正常等", "record_date": "该化验项目所属日期"}],\n'
            '  "current_status": "当前病情或症状描述",\n'
            '  "medical_summary": "仅针对本份资料的病情/结果要点总结",\n'
            '  "suggested_questions": ["问题1", "问题2", "问题3"]\n'
            "}\n"
        )

    def process_medical_record(
        self,
        user_id: str,
        file_path: str,
        filename: str,
        is_update: bool = False,
    ) -> dict:
        """
        解析单份病历并**追加**为独立记录（is_update 参数已废弃，保留仅为接口兼容）。
        """
        _ = is_update
        llm = self._get_llm()

        base_prompt = (
            f"请作为一位专业的肿瘤科医生，分析这一份上传的病历资料（文件名：{filename}）。\n"
            "资料以图像形式提供（PDF 已按页转为图片）。\n\n"
            "【医嘱项目 order_category — 重点】\n"
            "必须从报告抬头、申请单、检查类型、科室医嘱或报告标题中归纳出**医嘱项目/报告种类**（如：血常规、肝功能、生化、凝血、尿常规、肿瘤标志物、CT、MRI、超声、病理等），"
            "填入 `order_category` 字段；若无法判断，填报告上最接近的分类名称，不可留空（可填「其它检查」并简述）。\n\n"
            "【化验指标 — 强制完整性】\n"
            "1. `test_items` 必须穷尽**本份**图像中的全部检验/化验指标：凡表格中单独成行（或成条）的项目，"
            "每一条均须单独输出一条 JSON 对象，不得合并、不得抽样。\n"
            "2. 若某字段在报告上未印出，对应键填 `\"\"`。\n"
            "3. `lab_results` 仅作概括；**不得以 lab_results 替代 test_items**。\n\n"
        )

        base_prompt += (
            "【重要要求】你必须且只能输出一段合法的 JSON 文本，不要有任何多余的标记（如 ```json）或解释。\n"
            "本次任务**只描述当前这一份资料**，不要引用或合并用户其它历史报告。\n"
            "`test_items` 数组长度必须等于本份图像中识别到的独立检验项目总数。\n"
        )
        base_prompt += self._parse_llm_json_template()

        try:
            attachment_blocks = self._attachment_blocks_for_llm(file_path, filename)
        except (ValueError, RuntimeError) as e:
            folder = migrate_raw_profile_to_folder(self.load_profile(user_id))
            folder["records"].append(
                MedicalRecordEntry(
                    id=str(uuid.uuid4()),
                    source_filename=filename,
                    created_at=_utc_now_iso(),
                    medical_summary="",
                    current_status=f"档案解析失败：{str(e)}",
                ).model_dump()
            )
            self._save_folder(user_id, folder)
            return folder

        content_parts: list[dict] = [{"type": "text", "text": base_prompt}]
        content_parts.extend(attachment_blocks)

        messages = [
            SystemMessage(
                content=(
                    "你是医疗信息提取系统。必须填写 order_category（医嘱项目/报告种类）。"
                    "须将本份报告中出现的全部检验项目逐条写入 test_items，禁止遗漏。"
                )
            ),
            HumanMessage(content=content_parts),
        ]

        try:
            result = llm.invoke(messages)
            content = result.content.strip()
            if content.startswith("```"):
                content = content.strip("`").replace("json", "", 1).strip()
            parsed_json = json.loads(content)
            entry = MedicalRecordEntry.model_validate(
                {
                    "id": str(uuid.uuid4()),
                    "order_category": parsed_json.get("order_category") or "",
                    "report_date": parsed_json.get("report_date")
                    or parsed_json.get("record_date")
                    or "",
                    "source_filename": filename,
                    "created_at": _utc_now_iso(),
                    "name": parsed_json.get("name") or "",
                    "age": parsed_json.get("age") or "",
                    "gender": parsed_json.get("gender") or "",
                    "diagnosis": parsed_json.get("diagnosis") or "",
                    "stage": parsed_json.get("stage") or "",
                    "treatment_history": parsed_json.get("treatment_history") or "",
                    "lab_results": parsed_json.get("lab_results") or "",
                    "test_items": parsed_json.get("test_items") or [],
                    "current_status": parsed_json.get("current_status") or "",
                    "medical_summary": parsed_json.get("medical_summary") or "",
                    "suggested_questions": parsed_json.get("suggested_questions") or [],
                }
            )
        except Exception as e:
            print(f"Medical record extraction error: {e}")
            entry = MedicalRecordEntry(
                id=str(uuid.uuid4()),
                source_filename=filename,
                created_at=_utc_now_iso(),
                order_category="",
                report_date="",
                medical_summary="",
                current_status=f"档案解析失败：{str(e)}",
            )

        folder = migrate_raw_profile_to_folder(self.load_profile(user_id))
        folder["schema_version"] = 2
        folder["records"] = list(folder.get("records") or [])
        folder["records"].append(entry.model_dump())

        # 患者级字段：用最新一条非空覆盖
        if entry.name:
            folder["name"] = entry.name
        if entry.age:
            folder["age"] = entry.age
        if entry.gender:
            folder["gender"] = entry.gender
        if entry.report_date:
            folder["record_date"] = entry.report_date

        folder["medical_summary"] = build_folder_medical_summary(folder)
        self._save_folder(user_id, folder)
        return folder

    def process_discharge_report(self, user_id: str, file_path: str, filename: str) -> dict:
        """解析出院报告 PDF/图，侧重出院医嘱与随访日期，追加到 discharge_reports。"""
        llm = self._get_llm()
        base_prompt = (
            f"你正在处理一份**出院记录/出院小结/出院证明**类文书（文件名：{filename}）。"
            "图像由 PDF 按页转为 JPEG。\n\n"
            "【出院医嘱 — 最高优先级】\n"
            "必须定位「出院医嘱」「出院指导」「随访」「复诊计划」「注意事项」等章节，将其中**每一条**涉及"
            "**具体就诊/复查/治疗日期**的安排，拆解为 `follow_up_items` 中的独立对象。\n"
            "日期一律写为 `visit_date`（YYYY-MM-DD）。若仅写「2周后」「下次化疗前」等，请结合本报告"
            "`report_date`（出院日）推算并写入 `detail` 中说明推算依据。\n"
            "不得遗漏出院医嘱中出现的复查、抽血、换药、拆线、置管、影像、肿瘤科/放疗科门诊等带时间要求的条目。\n"
            "同时将可见范围内的**出院医嘱全文**写入 `discharge_orders_full_text`。\n\n"
            "你必须且只能输出**一段合法 JSON**，不要 markdown 代码块或任何解释文字。\n"
            "JSON 结构：\n"
            "{\n"
            '  "report_date": "出院日期或报告日期 YYYY-MM-DD",\n'
            '  "hospital_name": "医院名称",\n'
            '  "department": "科室",\n'
            '  "name": "患者姓名",\n'
            '  "diagnosis": "出院诊断",\n'
            '  "discharge_summary": "住院经过与出院时病情摘要",\n'
            '  "discharge_orders_full_text": "出院医嘱全文转写",\n'
            '  "follow_up_items": [\n'
            '    {"visit_date": "YYYY-MM-DD", "item_title": "简短事项名", "detail": "说明", "raw_excerpt": "原文节选"}\n'
            "  ],\n"
            '  "medication_notes": "出院带药及用法摘录",\n'
            '  "other_instructions": "其它医嘱说明",\n'
            '  "parse_notes": ""\n'
            "}\n"
        )

        try:
            attachment_blocks = self._attachment_blocks_for_llm(
                file_path, filename, max_pages=self._MAX_DISCHARGE_PDF_PAGES
            )
        except (ValueError, RuntimeError) as e:
            folder = migrate_raw_profile_to_folder(self.load_profile(user_id))
            folder.setdefault("discharge_reports", [])
            folder["discharge_reports"].append(
                DischargeReportEntry(
                    id=str(uuid.uuid4()),
                    source_filename=filename,
                    created_at=_utc_now_iso(),
                    parse_notes=f"文件处理失败：{e}",
                ).model_dump()
            )
            folder["medical_summary"] = build_folder_medical_summary(folder)
            self._save_folder(user_id, folder)
            return folder

        content_parts: list[dict] = [{"type": "text", "text": base_prompt}]
        content_parts.extend(attachment_blocks)
        messages = [
            SystemMessage(
                content=(
                    "你是医疗文书结构化助手。必须完整提取出院医嘱中的日期与随访事项，"
                    "供患者日历提醒；禁止编造报告中不存在的日期。"
                )
            ),
            HumanMessage(content=content_parts),
        ]

        try:
            result = llm.invoke(messages)
            content = result.content.strip()
            if content.startswith("```"):
                content = content.strip("`").replace("json", "", 1).strip()
            pj = json.loads(content)
            entry = DischargeReportEntry.model_validate(
                {
                    "id": str(uuid.uuid4()),
                    "report_date": pj.get("report_date") or "",
                    "hospital_name": pj.get("hospital_name") or "",
                    "department": pj.get("department") or "",
                    "source_filename": filename,
                    "created_at": _utc_now_iso(),
                    "name": pj.get("name") or "",
                    "diagnosis": pj.get("diagnosis") or "",
                    "discharge_summary": pj.get("discharge_summary") or "",
                    "discharge_orders_full_text": pj.get("discharge_orders_full_text") or "",
                    "follow_up_items": pj.get("follow_up_items") or [],
                    "medication_notes": pj.get("medication_notes") or "",
                    "other_instructions": pj.get("other_instructions") or "",
                    "parse_notes": pj.get("parse_notes") or "",
                }
            )
        except Exception as e:
            print(f"Discharge report extraction error: {e}")
            entry = DischargeReportEntry(
                id=str(uuid.uuid4()),
                source_filename=filename,
                created_at=_utc_now_iso(),
                parse_notes=f"档案解析失败：{e}",
            )

        folder = migrate_raw_profile_to_folder(self.load_profile(user_id))
        folder.setdefault("discharge_reports", [])
        folder["discharge_reports"].append(entry.model_dump())
        if entry.name:
            folder["name"] = entry.name
        folder["medical_summary"] = build_folder_medical_summary(folder)
        self._save_folder(user_id, folder)
        return folder

    def delete_discharge_report(self, user_id: str, report_id: str) -> dict[str, Any]:
        rid = (report_id or "").strip()
        if not rid:
            raise ValueError("缺少 report_id")
        folder = self.load_profile(user_id)
        recs = list(folder.get("discharge_reports") or [])
        n0 = len(recs)

        def _same_id(a: Any, b: str) -> bool:
            return str(a or "").strip() == str(b or "").strip()

        recs = [r for r in recs if not _same_id(r.get("id") if isinstance(r, dict) else None, rid)]
        if len(recs) == n0:
            raise ValueError("记录不存在或已删除")
        folder["discharge_reports"] = recs
        folder["schema_version"] = 2
        folder["medical_summary"] = build_folder_medical_summary(folder)
        self._save_folder(user_id, folder)
        return folder

    def _save_folder(self, user_id: str, folder: dict[str, Any]) -> None:
        validated = UserMedicalFolder(**folder).model_dump()
        path = PROFILE_DIR / f"{user_id}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(validated, f, ensure_ascii=False, indent=2)

    def save_profile(self, user_id: str, profile_data: dict[str, Any]) -> None:
        """保存完整病历夹（含 records）。"""
        self._save_folder(user_id, profile_data)

    def delete_record(self, user_id: str, record_id: str) -> dict[str, Any]:
        """删除指定 id 的病历条，并重算夹级 medical_summary、同步患者摘要字段。"""
        rid = (record_id or "").strip()
        if not rid:
            raise ValueError("缺少 record_id")
        folder = self.load_profile(user_id)
        recs = list(folder.get("records") or [])
        n_before = len(recs)

        def _same_id(a: Any, b: str) -> bool:
            return str(a or "").strip() == str(b or "").strip()

        recs = [r for r in recs if not _same_id(r.get("id") if isinstance(r, dict) else None, rid)]
        if len(recs) == n_before:
            raise ValueError("记录不存在或已删除")
        folder["records"] = recs
        folder["schema_version"] = 2
        sync_folder_demographics_from_last_record(folder)
        folder["medical_summary"] = build_folder_medical_summary(folder)
        self._save_folder(user_id, folder)
        return folder

    def load_profile(self, user_id: str) -> dict[str, Any]:
        path = PROFILE_DIR / f"{user_id}.json"
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return {}
        folder = migrate_raw_profile_to_folder(raw)
        changed = _ensure_record_ids(folder) or _ensure_discharge_report_ids(folder)
        if changed:
            self._save_folder(user_id, folder)
        return folder
