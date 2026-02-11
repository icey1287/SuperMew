from dotenv import load_dotenv
import os
import json
from langchain.chat_models import init_chat_model
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
try:
    from .tools import get_current_weather, search_knowledge_base, get_last_rag_context
except ImportError:
    from tools import get_current_weather, search_knowledge_base, get_last_rag_context
from datetime import datetime

load_dotenv()

API_KEY = os.getenv("ARK_API_KEY")
MODEL = os.getenv("MODEL")
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

        serialized = []
        for idx, msg in enumerate(messages):
            record = {
                "type": msg.type,
                "content": msg.content,
                "timestamp": datetime.now().isoformat()
            }
            if extra_message_data and idx < len(extra_message_data):
                extra = extra_message_data[idx] or {}
                if "rag_trace" in extra:
                    record["rag_trace"] = extra["rag_trace"]
            serialized.append(record)

        data[user_id][session_id] = {
            "messages": serialized,
            "metadata": metadata or {},
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
        except:
            return {}



def create_agent_instance():
    model = init_chat_model(
        model=MODEL,
        model_provider="openai",
        api_key=API_KEY,
        base_url=BASE_URL,
        temperature=0.3,
    )

    agent = create_agent(
        model=model,
        tools=[get_current_weather, search_knowledge_base],
        system_prompt=(
            "You are a cute cat bot that loves to help users. "
            "When responding, you may use tools to assist. "
            "Use the search_knowledge_base tool when users ask questions that might be in uploaded documents. "
            "If tool results include a Step-back Question/Answer, use that general principle to reason and answer, "
            "but do not reveal chain-of-thought. "
            "If you don't know the answer, admit it honestly."
        ),
    )
    return agent, model


agent, model = create_agent_instance()

storage = ConversationStorage()

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


def chat_with_agent(user_text: str, user_id: str = "default_user", session_id: str = "default_session"):
    """使用 Agent 处理用户消息并返回响应"""
    messages = storage.load(user_id, session_id)

    # 清理可能残留的 RAG 上下文，避免跨请求污染
    get_last_rag_context(clear=True)
    
    if len(messages) > 50:
        summary = summarize_old_messages(model, messages[:40])

        messages = [
            SystemMessage(content=f"之前的对话摘要：\n{summary}")
        ] + messages[40:]

    messages.append(HumanMessage(content=user_text))
    result = agent.invoke({"messages": messages})

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
