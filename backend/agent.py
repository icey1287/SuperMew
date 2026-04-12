from dotenv import load_dotenv
import os
import json
import asyncio
from pydantic import BaseModel, Field
from langchain.chat_models import init_chat_model
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage, AIMessage, AIMessageChunk, SystemMessage
from tools import search_knowledge_base, get_last_rag_context, reset_tool_call_guards, set_rag_step_queue
from profile_manager import ProfileManager, build_folder_medical_summary
from datetime import datetime

load_dotenv()

API_KEY = os.getenv("ARK_API_KEY")
MODEL = os.getenv("MODEL")
FAST_MODEL = os.getenv("FAST_MODEL", MODEL)
BASE_URL = os.getenv("BASE_URL")

class ConversationStorage:
    """对话存储"""

    def __init__(self, storage_file: str = None):
        if storage_file:
            storage_path = os.path.abspath(storage_file)
        else:
            package_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
            data_dir = os.path.join(package_root, "data")
            os.makedirs(data_dir, exist_ok=True)
            storage_path = os.path.join(data_dir, "customer_service_history.json")

        self.storage_file = storage_path

    def save(self, user_id: str, session_id: str, messages: list, metadata: dict = None, extra_message_data: list = None):
        """保存对话"""
        data = self._load()

        if user_id not in data:
            data[user_id] = {}
            
        existing_meta = data[user_id].get(session_id, {}).get("metadata", {})
        merged_meta = {**existing_meta, **(metadata or {})}

        # 同一会话上一次落盘的消息（用于保留每条 AI 回复的 rag_trace）
        old_session_messages = []
        if user_id in data and session_id in data[user_id]:
            old_session_messages = data[user_id][session_id].get("messages") or []

        serialized = []
        for idx, msg in enumerate(messages):
            record = {
                "type": msg.type,
                "content": msg.content,
                "timestamp": datetime.now().isoformat()
            }
            new_rag = None
            if extra_message_data and idx < len(extra_message_data):
                extra = extra_message_data[idx] or {}
                if extra.get("rag_trace") is not None:
                    new_rag = extra["rag_trace"]
            if new_rag is not None:
                record["rag_trace"] = new_rag
            elif idx < len(old_session_messages):
                om = old_session_messages[idx]
                if om.get("type") == msg.type and om.get("content") == msg.content:
                    old_rag = om.get("rag_trace")
                    if old_rag is not None:
                        record["rag_trace"] = old_rag
            serialized.append(record)

        data[user_id][session_id] = {
            "messages": serialized,
            "metadata": merged_meta,
            "updated_at": datetime.now().isoformat()
        }

        with open(self.storage_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load(self, user_id: str, session_id: str) -> list:
        """加载对话"""
        data = self._load()

        if user_id not in data or session_id not in data[user_id]:
            return []

        messages = []
        for msg_data in data[user_id][session_id]["messages"]:
            if msg_data["type"] == "human":
                messages.append(HumanMessage(content=msg_data["content"]))
            elif msg_data["type"] == "ai":
                messages.append(AIMessage(content=msg_data["content"]))
            elif msg_data["type"] == "system":
                messages.append(SystemMessage(content=msg_data["content"]))

        return messages

    def list_sessions(self, user_id: str) -> list:
        """列出用户的所有会话"""
        data = self._load()
        if user_id not in data:
            return []
        return list(data[user_id].keys())

    def delete_session(self, user_id: str, session_id: str) -> bool:
        """删除指定用户的会话，返回是否删除成功"""
        data = self._load()
        if user_id not in data or session_id not in data[user_id]:
            return False

        del data[user_id][session_id]
        if not data[user_id]:
            del data[user_id]

        with open(self.storage_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True

    def _load(self) -> dict:
        """加载数据"""
        if not os.path.exists(self.storage_file):
            return {}
        try:
            with open(self.storage_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}



def create_agent_instance(profile: dict = None):
    model = init_chat_model(
        model=MODEL,
        model_provider="openai",
        api_key=API_KEY,
        base_url=BASE_URL,
        temperature=0.3,
        stream_usage=True,
    )
    
    fast_model = init_chat_model(
        model=FAST_MODEL,
        model_provider="openai",
        api_key=API_KEY,
        base_url=BASE_URL,
        temperature=0.2,
    )

    base_system_prompt = (
        "你是一个专门为鼻咽癌（Nasopharyngeal Carcinoma）患者及家属提供医疗咨询和心理支持的智能问答助手。你的名字叫“喵喵”，语气温暖、专业、充满同理心。\n"
        "请严格遵守以下规则：\n"
        "1. 只要用户询问病情、治疗、医学科普等内容，你必须使用 search_knowledge_base 工具检索专业的医疗知识库。\n"
        "2. 每次对话最多调用一次检索工具，接收到检索结果后，必须立刻基于该结果生成 Final Answer，不可重复调用。\n"
        "3. 如果检索到的知识库内容不足以回答问题，请诚实地说明你不知道，切勿捏造或猜测任何医疗事实。\n"
        "4. 回答结构：在进行详细的医学解读或建议之前，**必须**先在回答的最开头提供一段简明扼要的「深度总结（核心结论）」，让患者或家属能一眼看懂核心要点，然后再分段或分点进行详细的解读。\n"
        "5. 正文引用：当回答依据了检索到的文档块时，**必须**在相应句子末尾使用 [1] 或 [2][3] 等数字编号；编号须与对话界面中**自动展示的「参考文献」面板**中的条目序号一致（该面板由系统根据检索结果单独渲染，含标题、链接等元数据）。\n"
        "6. **禁止重复罗列文献**：**不要**在回答正文之后再输出「参考文献」「参考资料」等标题或小节，也**不要**在文末重复列出文献题录或链接——完整来源信息仅由前端参考文献区展示即可，你只需在正文中保留行内编号 [1][2]。\n"
        "7. 名词解释：当你提到重要的医学术语、治疗方案、药物或解剖学名词时，**必须**使用 HTML 标签包裹它以提供解释，格式为：<span class=\"concept-tooltip\" data-desc=\"简短的术语解释或定义\">医学名词</span>。这样用户可以在前端点击查看解释。\n"
        "8. 如果工具返回了退步问题（Step-back）相关的解答，请结合该普遍原理来回答，但不要在最终回复中暴露思考过程（chain-of-thought）。\n"
        "9. **真实性**：严禁伪造来源或编造检索中不存在的医学事实；无法从检索内容核实的信息不要断言。\n"
        "10. **编号一致性**：正文中出现的 [n] 应对应在本次检索被实际用于论证的要点；不要引用与当前回答无关的编号，以免与界面参考文献序号错位。\n"
        "请始终用中文和用户进行自然、流畅、通俗易懂的交流。\n"
    )

    if profile and isinstance(profile, dict):
        summary_text = ""
        if profile.get("records"):
            summary_text = build_folder_medical_summary(profile)
        if not summary_text:
            summary_text = profile.get("medical_summary") or ""
        if summary_text:
            memory_lines = [
                "\n【当前患者的 Long-Term Memory (长效病历记忆)】",
                f"最近报告日期: {profile.get('record_date', '未知')}",
                f"病情总结（含历次报告摘要）: {summary_text}",
                "请牢记以上患者背景信息，在回答时紧密结合患者的实际病情、分期或用药史，提供高度个性化且具有针对性的建议。如果问题与患者病情无关，可仅做参考。",
            ]
            base_system_prompt += "\n".join(memory_lines)

    agent = create_agent(
        model=model,
        tools=[search_knowledge_base],
        system_prompt=base_system_prompt,
    )
    return agent, model, fast_model


agent, model, fast_model = create_agent_instance()

storage = ConversationStorage()
profile_manager = ProfileManager()

def summarize_old_messages(model, messages: list) -> str:
    """将旧消息总结为摘要"""
    # 提取旧对话
    old_conversation = "\n".join([
        f"{'用户' if msg.type == 'human' else 'AI'}: {msg.content}"
        for msg in messages
    ])

    # 生成摘要
    summary_prompt = f"""请总结以下对话的关键信息：

{old_conversation}
总结（包含用户信息、重要事实、待办事项）："""

    summary = model.invoke(summary_prompt).content
    return summary


class FollowUpQuestions(BaseModel):
    questions: list[str] = Field(description="3个最相关、最可能的追问问题")

async def optimize_user_question(user_text: str, llm_model) -> list[str]:
    """后台异步优化用户提问，使其更专业、更易于大模型理解"""
    if not user_text.strip():
        return []
    try:
        prompt = (
            "你是一个医疗问答提示词优化专家。用户输入了一个初步的鼻咽癌相关提问，请帮用户扩充、优化成 3 个不同侧重、更具体、有助于大模型给出精准医学回答的问题。\n"
            "要求：\n"
            "1. 问题必须非常精炼、直接，不要寒暄。\n"
            "2. 语言要自然、准确，不需要过度追求复杂的循证医学词汇，但要能清晰表达医学意图。\n"
            "3. 只需返回 JSON 数组，严禁其他解释或 markdown 标记。\n"
            "【示例】\n"
            "如果用户原始提问是：鼻咽癌化疗吃不下饭怎么办\n"
            '你应该输出形如：["鼻咽癌化疗期间食欲不振的管理策略", "鼻咽癌化疗相关厌食的循证干预措施", "鼻咽癌化疗患者营养支持的最佳实践"]\n\n'
            f"用户原始提问：{user_text}\n"
        )
        
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, lambda: llm_model.invoke([SystemMessage(content=prompt)]))
        
        content = res.content.strip()
        if content.startswith("```"):
            content = content.strip("`").replace("json", "", 1).strip()
            
        questions = json.loads(content)
        if isinstance(questions, list):
            return questions[:3]
        return []
    except Exception as e:
        print(f"Optimize question error: {e}")
        return []

async def _generate_follow_ups(user_text: str, prev_messages: list, llm_model) -> list[str]:
    """后台异步生成追问问题，不阻塞主流式输出"""
    try:
        history_lines = []
        for msg in prev_messages[-4:]:
            role = "用户" if msg.type == "human" else "AI"
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            history_lines.append(f"{role}: {content}")
        history = "\n".join(history_lines)
        
        prompt = (
            "你是一个鼻咽癌智能问答助手的意图预测模块。请根据对话上下文和用户的最新提问，"
            "预测用户在得到专业回答后，接下来最关心、最可能追问的 3 个简短问题（控制在20字以内）。\n"
            "⚠️重要限制：\n"
            "1. 追问必须是**具体的医学、治疗方案、副作用机制或病理原理**（例如：“早反应A/B型鼻咽癌患者缩短诱导化疗周期具体是缩短到几程？”、“化疗引起的骨髓抑制如何缓解？”）。\n"
            "2. **严禁**生成关于“检查费用”、“医保报销”、“几天出结果”、“在哪家医院挂号”等与具体本地政策和后勤相关的问题。\n"
            "3. 追问必须与用户的提问意图具有高度逻辑延续性。\n\n"
            "你必须且只能返回一个合法的 JSON 字符串数组，不要包含任何 markdown 标记（如 ```json）、反引号或其他解释性文本。\n"
            '格式必须绝对形如：["问题1", "问题2", "问题3"]\n\n'
            f"历史上下文：\n{history}\n\n"
            f"用户最新提问：{user_text}\n"
        )
        
        # 使用线程池调用同步 invoke，防止某些模型不支持 ainvoke 导致静默失败
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, lambda: llm_model.invoke([SystemMessage(content=prompt)]))
        
        content = res.content.strip()
        if content.startswith("```"):
            content = content.strip("`").replace("json", "", 1).strip()
            
        questions = json.loads(content)
        if isinstance(questions, list):
            return questions[:3]
        return []
    except Exception as e:
        print(f"Follow-up generation error: {e}")
        return []


def chat_with_agent(user_text: str, user_id: str = "default_user", session_id: str = "default_session"):
    """使用 Agent 处理用户消息并返回响应"""
    messages = storage.load(user_id, session_id)

    # 清理可能残留的 RAG 上下文，避免跨请求污染
    get_last_rag_context(clear=True)
    reset_tool_call_guards()
    
    # 动态加载针对当前患者记忆的 Agent
    profile = profile_manager.load_profile(user_id)
    dynamic_agent, _, _ = create_agent_instance(profile=profile)

    if len(messages) > 50:
        summary = summarize_old_messages(model, messages[:40])

        messages = [
            SystemMessage(content=f"之前的对话摘要：\n{summary}")
        ] + messages[40:]

    messages.append(HumanMessage(content=user_text))
    storage.save(user_id, session_id, messages)

    result = dynamic_agent.invoke(
        {"messages": messages},
        config={"recursion_limit": 8},
    )

    response_content = ""
    if isinstance(result, dict):
        if "output" in result:
            response_content = result["output"]
        elif "messages" in result and result["messages"]:
            msg = result["messages"][-1]
            response_content = getattr(msg, "content", str(msg))
        else:
            response_content = str(result)
    elif hasattr(result, "content"):
        response_content = result.content
    else:
        response_content = str(result)
    
    messages.append(AIMessage(content=response_content))

    rag_context = get_last_rag_context(clear=True)
    rag_trace = rag_context.get("rag_trace") if rag_context else None

    extra_message_data = [None] * (len(messages) - 1) + [{"rag_trace": rag_trace}]
    storage.save(user_id, session_id, messages, extra_message_data=extra_message_data)

    return {
        "response": response_content,
        "rag_trace": rag_trace,
    }


async def generate_session_title(user_text: str, llm_model) -> str:
    """后台异步生成会话标题"""
    try:
        prompt = f"请根据用户的首次提问，生成一个简短的对话标题（控制在10个字以内，不要带有标点符号）。\n用户提问：{user_text}"
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, lambda: llm_model.invoke([SystemMessage(content=prompt)]))
        title = res.content.strip().strip('"').strip('。')
        return title
    except Exception as e:
        print(f"Title generation error: {e}")
        return "新会话"

from tools import set_rag_config

async def chat_with_agent_stream(user_text: str, user_id: str = "default_user", session_id: str = "default_session", think_mode: str = "normal"):
    """使用 Agent 处理用户消息并流式返回响应。
    
    架构：使用统一输出队列 + 后台任务，确保 RAG 检索步骤在工具执行期间实时推送，
    而非等待工具完成后才显示。
    """
    set_rag_config({"think_mode": think_mode})
    
    messages = storage.load(user_id, session_id)
    is_first_message = len(messages) == 0

    # 清理可能残留的 RAG 上下文
    get_last_rag_context(clear=True)
    reset_tool_call_guards()

    # 统一输出队列：所有事件（content / rag_step）都汇入这里
    output_queue = asyncio.Queue()

    # 动态加载针对当前患者记忆的 Agent
    profile = profile_manager.load_profile(user_id)
    dynamic_agent, _, _ = create_agent_instance(profile=profile)

    class _RagStepProxy:
        """代理对象：将 emit_rag_step 的原始 step dict 包装后放入统一输出队列。"""
        def put_nowait(self, step):
            output_queue.put_nowait({"type": "rag_step", "step": step})

    set_rag_step_queue(_RagStepProxy())

    if len(messages) > 50:
        summary = summarize_old_messages(model, messages[:40])
        messages = [
            SystemMessage(content=f"之前的对话摘要：\n{summary}")
        ] + messages[40:]

    messages.append(HumanMessage(content=user_text))
    
    # 立即存盘一次，保证即便流还没结束，用户如果刷新或者切换会话，至少能加载到自己刚发的消息
    storage.save(user_id, session_id, messages)

    # 启动后台意图识别任务（并发执行，彻底隐藏延迟）
    follow_up_task = asyncio.create_task(_generate_follow_ups(user_text, messages[:-1], model))
    
    # 如果是首条消息，后台异步生成标题
    if is_first_message:
        def _on_title_done(fut):
            try:
                title = fut.result()
                # 放入队列供主循环分发 (因为运行在同一 event loop，put_nowait 安全)
                output_queue.put_nowait({"type": "session_title", "title": title, "session_id": session_id})
            except Exception as e:
                print(f"Title task error: {e}")
                
        title_task = asyncio.create_task(generate_session_title(user_text, fast_model))
        title_task.add_done_callback(_on_title_done)

    full_response = ""

    async def _agent_worker():
        """后台任务：运行 agent 并将内容 chunk 推入输出队列。"""
        nonlocal full_response
        try:
            async for msg, metadata in dynamic_agent.astream(
                {"messages": messages},
                stream_mode="messages",
                config={"recursion_limit": 8},
            ):
                if not isinstance(msg, AIMessageChunk):
                    continue
                if getattr(msg, "tool_call_chunks", None):
                    continue

                content = ""
                if isinstance(msg.content, str):
                    content = msg.content
                elif isinstance(msg.content, list):
                    for block in msg.content:
                        if isinstance(block, str):
                            content += block
                        elif isinstance(block, dict) and block.get("type") == "text":
                            content += block.get("text", "")

                if content:
                    full_response += content
                    await output_queue.put({"type": "content", "content": content})
        except Exception as e:
            await output_queue.put({"type": "error", "content": str(e)})
        finally:
            # 哨兵：通知主循环 agent 已完成
            await output_queue.put(None)

    # 启动后台任务
    agent_task = asyncio.create_task(_agent_worker())

    try:
        # 主循环：持续从统一队列取事件并 yield SSE
        # RAG 步骤在工具执行期间通过 call_soon_threadsafe 实时入队，不需要等 agent 产出 chunk
        while True:
            event = await output_queue.get()
            if event is None:
                break
            yield f"data: {json.dumps(event)}\n\n"
    except GeneratorExit:
        # 客户端断开连接（AbortController）时，FastAPI 会向此生成器抛出 GeneratorExit
        # 我们必须在此处取消后台任务
        agent_task.cancel()
        try:
            await agent_task
        except asyncio.CancelledError:
            pass  # 任务已成功取消
        raise  # 重新抛出 GeneratorExit 以便 FastAPI 正确处理关闭
    finally:
        # 正常结束或异常退出时清理
        set_rag_step_queue(None)
        if not agent_task.done():
             agent_task.cancel()

    # 获取 RAG trace
    rag_context = get_last_rag_context(clear=True)
    rag_trace = rag_context.get("rag_trace") if rag_context else None

    # 发送 trace 信息
    if rag_trace:
        yield f"data: {json.dumps({'type': 'trace', 'rag_trace': rag_trace})}\n\n"

    # 获取并发送追问（等待最多 3 秒，如果不返回则放弃，保证不阻塞）
    try:
        follow_ups = await asyncio.wait_for(follow_up_task, timeout=3.0)
        if follow_ups:
            yield f"data: {json.dumps({'type': 'follow_ups', 'questions': follow_ups}, ensure_ascii=False)}\n\n"
    except Exception as e:
        print(f"Wait for follow_ups timeout/error: {e}")

    # 发送结束信号
    yield "data: [DONE]\n\n"

    # 保存对话并更新标题
    metadata = {}
    if is_first_message:
        try:
            # 确保此时标题任务已完成 (通常早已完成，但保险起见 await 一下以保存元数据)
            title = await title_task
            metadata["title"] = title
        except Exception:
            pass

    messages.append(AIMessage(content=full_response))
    extra_message_data = [None] * (len(messages) - 1) + [{"rag_trace": rag_trace}]
    storage.save(user_id, session_id, messages, metadata=metadata, extra_message_data=extra_message_data)
