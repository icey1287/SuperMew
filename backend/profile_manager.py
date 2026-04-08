import os
import json
import base64
from pathlib import Path
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

class PatientProfile(BaseModel):
    name: str = Field(default="", description="患者姓名（如病历中包含）")
    age: str = Field(default="", description="患者年龄")
    gender: str = Field(default="", description="患者性别")
    record_date: str = Field(default="", description="病历或检查报告的时间（例如：2023-10-15）")
    diagnosis: str = Field(default="", description="主要诊断（例如：鼻咽癌）")
    stage: str = Field(default="", description="肿瘤分期（如：TNM分期、临床分期）")
    treatment_history: str = Field(default="", description="既往治疗史（如化疗、放疗方案及时间）")
    lab_results: str = Field(
        default="",
        description="对本次报告检验结果的总体文字概述；逐条完整枚举必须以 test_items 为准，此处不得代替省略任何指标",
    )
    test_items: list[DetailedTestResult] = Field(
        default_factory=list,
        description="须覆盖图像中可见的全部检验/化验指标：报告上每一行（或每一条）独立项目对应一条记录，禁止只摘抄“重点项”或抽样",
    )
    current_status: str = Field(default="", description="当前病情或症状描述")
    medical_summary: str = Field(default="", description="基于上述所有信息的连贯、专业病情总结长文，用于作为大模型的长期记忆")
    suggested_questions: list[str] = Field(default_factory=list, description="根据病历信息，推荐患者向AI医生提问的3-5个个性化问题")

class ProfileManager:
    """患者病历档案：用环境变量 MODEL 解析病历。PDF 经渲染为页面图片后以 image_url 送入（兼容仅支持 text/image_url 的网关）。"""

    _MAX_PDF_PAGES = 10

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
        """OpenAI/火山兼容：content 仅支持 text、image_url 等，使用 data URL。"""
        b64 = base64.b64encode(raw).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{b64}"},
        }

    def _attachment_blocks_for_llm(self, file_path: str, filename: str) -> list[dict]:
        """转为网关支持的 image_url 块。PDF 按页渲染为 JPEG（不经 PyPDF 抽文本）。"""
        path = Path(file_path)
        suffix = path.suffix.lower()

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
                n = min(len(doc), self._MAX_PDF_PAGES)
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

    def process_medical_record(self, user_id: str, file_path: str, filename: str, is_update: bool = False) -> dict:
        """将病历以 image_url（PDF 为逐页图）交给 MODEL 解析并保存结构化档案"""
        llm = self._get_llm()

        base_prompt = (
            f"请作为一位专业的肿瘤科医生，分析这份上传的病历资料（文件名：{filename}）。"
            "资料以图像形式提供（PDF 已按页转为图片），请阅读并提取患者的核心医疗信息，"
            "并根据病情给出 3-5 个推荐患者向您提问的个性化问题。\n\n"
            "【化验指标 — 强制完整性】\n"
            "1. `test_items` 必须穷尽本次图像中出现的**全部**检验/化验指标：凡报告表格、列表中单独成行（或成条）的项目，"
            "包括但不限于血常规、C反应蛋白、生化全项、肝肾功能、电解质、血脂、血糖、凝血、心肌酶、"
            "尿/便常规、肿瘤标志物、激素、免疫、血气等，**每一条均须单独输出一条 JSON 对象**，不得合并、不得抽样、不得以“等”“详见报告”代替。\n"
            "2. 禁止因篇幅或“重要性”而删减指标；若不同日期或多张报告，按各自日期分别列入，并正确填写 `record_date`。\n"
            "3. 若某字段在报告上未印出，对应键填 `\"\"`；`result`/`unit`/`reference_range`/`abnormal` 尽量与报告原文一致。\n"
            "4. `lab_results` 仅作概括性说明；**不得以 lab_results 替代 test_items**，遗漏指标视为任务失败。\n\n"
        )

        if is_update:
            existing_profile = self.load_profile(user_id)
            existing_json = json.dumps(existing_profile, ensure_ascii=False)
            base_prompt += (
                f"【患者历史档案】\n{existing_json}\n\n"
                "【更新指令】\n"
                "这是一份新的病历资料。请你提取这份新资料中的信息，并**将它与上面的历史档案进行融合更新**。\n"
                "1. 对于基本信息（姓名、性别等）若新资料未提及，请保留历史数据。\n"
                "2. **对于 `test_items`：必须保留历史档案中的全部记录；对本次新图像中出现的报告，须将其中全部检验项目逐条追加，同样满足「无遗漏、不抽样」；每条写对 `record_date`。**\n"
                "3. **更新长效记忆** `medical_summary`：结合新老病历的数据，总结出病情的发展、治疗的演进和最近的关键变化。\n\n"
            )

        base_prompt += (
            "【重要要求】你必须且只能输出一段合法的 JSON 文本，不要有任何多余的标记（如 ```json）或解释。\n"
            "`test_items` 数组长度必须等于你在图像中识别到的独立检验项目总数（逐项核对，宁多勿漏）。\n"
            "JSON 必须严格包含以下字段：\n"
            "{\n"
            '  "name": "患者姓名（如病历中包含）",\n'
            '  "age": "患者年龄",\n'
            '  "gender": "患者性别",\n'
            '  "record_date": "最新一次病历或检查报告的时间（例如：2023-10-15）",\n'
            '  "diagnosis": "主要诊断（例如：鼻咽癌）",\n'
            '  "stage": "肿瘤分期（如：TNM分期、临床分期）",\n'
            '  "treatment_history": "既往治疗史（如化疗、放疗方案及时间）",\n'
            '  "lab_results": "检验/病理结果的概括性文字（不可用来省略 test_items 中的任何一条）",\n'
            '  "test_items": [{"item_name": "与报告完全一致的项目名", "result": "结果值", "unit": "单位", "reference_range": "参考区间", "abnormal": "偏高/偏低/正常等", "record_date": "该化验单/项目所属日期"}],\n'
            '  "current_status": "当前病情或症状描述",\n'
            '  "medical_summary": "请用一段连贯、专业的文字总结上述所有核心病情（如果是更新操作请包含病情演进过程），这将被大模型永久记忆，要求高度概括、准确",\n'
            '  "suggested_questions": ["问题1", "问题2", "问题3"]\n'
            "}\n"
        )

        try:
            attachment_blocks = self._attachment_blocks_for_llm(file_path, filename)
        except (ValueError, RuntimeError) as e:
            profile_data = PatientProfile().dict()
            profile_data["current_status"] = f"档案解析失败：{str(e)}"
            self.save_profile(user_id, profile_data)
            return profile_data

        content_parts: list[dict] = [{"type": "text", "text": base_prompt}]
        content_parts.extend(attachment_blocks)

        messages = [
            SystemMessage(
                content=(
                    "你是医疗信息提取系统。硬性规则：从用户提供的每一张图像中识别检验类报告时，"
                    "必须将报告中出现的全部检验项目逐条写入 JSON 的 test_items，禁止遗漏、禁止只提取部分指标；"
                    "输出前在内心逐项核对表格行数与 test_items 条数是否一致。"
                )
            ),
            HumanMessage(content=content_parts),
        ]

        try:
            result = llm.invoke(messages)
            content = result.content.strip()

            # 移除可能存在的 markdown 代码块包裹
            if content.startswith("```"):
                content = content.strip("`").replace("json", "", 1).strip()

            parsed_json = json.loads(content)

            # 使用 Pydantic 验证和补全默认值
            profile_data = PatientProfile(**parsed_json).dict()
        except Exception as e:
            print(f"Medical record extraction error: {e}")
            profile_data = PatientProfile().dict()
            profile_data["current_status"] = f"档案解析失败：{str(e)}"

        # Save profile
        self.save_profile(user_id, profile_data)
        return profile_data

    def save_profile(self, user_id: str, profile_data: dict):
        file_path = PROFILE_DIR / f"{user_id}.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(profile_data, f, ensure_ascii=False, indent=2)

    def load_profile(self, user_id: str) -> dict:
        file_path = PROFILE_DIR / f"{user_id}.json"
        if file_path.exists():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}
