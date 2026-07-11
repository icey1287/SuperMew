import os

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model

from backend.chat.request_context import ChatRequestContext
from backend.tools import get_current_weather, make_search_knowledge_base

API_KEY = os.getenv("ARK_API_KEY")
MODEL = os.getenv("MODEL")
FAST_MODEL = os.getenv("FAST_MODEL")
BASE_URL = os.getenv("BASE_URL")

SYSTEM_PROMPT = (
    "You are a cute cat bot that loves to help users. "
    "When responding, you may use tools to assist. "
    "Use search_knowledge_base when users ask document/knowledge questions. "
    "Do not call the same tool repeatedly in one turn. At most one knowledge tool call per turn. "
    "Once you call search_knowledge_base and receive its result, you MUST immediately produce the Final Answer based on that result. "
    "After receiving search_knowledge_base result, you MUST NOT call any tool again (including get_current_weather or search_knowledge_base). "
    "If the tool result starts with NEEDS_CLARIFICATION or NEEDS_SCOPE_SELECTION, ask the user the requested question directly and do not answer from retrieved context. "
    "If the tool result starts with NO_KNOWLEDGE, say the knowledge base does not contain reliable relevant information. "
    "If the retrieved context is insufficient, answer honestly that you don't know instead of making up facts. "
    "When answering based on retrieved chunks, you MUST cite the source chunks using their index numbers inline, for example [1] or [2][3]. "
    "Step-back questions and HyDE documents are retrieval aids only, not factual evidence. "
    "Base factual claims only on retrieved source chunks and do not reveal chain-of-thought. "
    "If you don't know the answer, admit it honestly."
)


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
    stream_usage=True,
)


def create_agent_for_request(ctx: ChatRequestContext):
    return create_agent(
        model=model,
        tools=[
            get_current_weather,
            make_search_knowledge_base(ctx),
        ],
        system_prompt=SYSTEM_PROMPT,
    )
