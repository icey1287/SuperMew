import asyncio
import json

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage

from backend.chat.request_context import ChatRequestContext
from backend.chat.runtime import create_agent_for_request, fast_model
from backend.chat.storage import ConversationStorage

storage = ConversationStorage()

CONTEXT_WINDOW_MESSAGES = 6


def _build_context_messages(
    messages: list,
    persistent_note: str,
    user_text: str,
) -> list:
    short_term = messages[-CONTEXT_WINDOW_MESSAGES:] if len(messages) > CONTEXT_WINDOW_MESSAGES else messages
    context_messages: list = []
    if persistent_note:
        context_messages.append(
            SystemMessage(
                content=(
                    "【对话持久化笔记（你的工作记忆）】\n"
                    f"{persistent_note}\n"
                    "请参考以上笔记保持对话连贯性，避免重复回答已解决的问题。"
                )
            )
        )
    context_messages.extend(short_term)
    context_messages.append(HumanMessage(content=user_text))
    return context_messages


async def update_persistent_note(
    current_note: str,
    user_text: str,
    ai_response: str,
) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: _update_persistent_note_sync(current_note, user_text, ai_response),
    )


def _generate_session_title_sync(user_text: str) -> str:
    try:
        prompt = (
            "请根据用户的首次提问，生成一个简短的对话标题（控制在 10 个字以内，不要标点）。\n"
            f"用户提问：{user_text}"
        )
        res = fast_model.invoke([HumanMessage(content=prompt)])
        title = (res.content or "").strip().strip('"').strip("。")
        return title or "新会话"
    except Exception as e:
        print(f"Title generation error: {e}")
        return "新会话"


async def generate_session_title(user_text: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _generate_session_title_sync(user_text))


def _update_persistent_note_sync(current_note: str, user_text: str, ai_response: str) -> str:
    try:
        prompt = (
            "你是一个【Context Manager Agent】(上下文管理器)，负责维护多轮对话中的「持久化笔记」。\n"
            "笔记是模型在有限上下文窗口下的长效工作记忆，记录已解决的问题与关键事实。\n\n"
            "更新规则：\n"
            "1. 将新信息与现有笔记智能合并，不要简单拼接。\n"
            "2. 过滤噪音，控制在 500 字以内，用简明条目输出。\n"
            "3. 若信息冲突，保留最可靠或最新版本。\n\n"
            f"▼ 现有笔记：\n{current_note if current_note else '无'}\n\n"
            f"▼ 最新一轮对话：\n用户：{user_text}\nAI：{ai_response}\n\n"
            "请直接输出更新后的笔记（纯文本，不要解释或 Markdown 代码块）："
        )
        res = fast_model.invoke([HumanMessage(content=prompt)])
        return (res.content or "").strip()
    except Exception as e:
        print(f"Context Manager Error: {e}")
        return current_note


def chat_with_agent(
    user_text: str,
    user_id: str = "default_user",
    session_id: str = "default_session",
):
    messages, metadata = storage.load_with_meta(user_id, session_id)
    persistent_note = metadata.get("persistent_note", "")
    is_first_message = len(messages) == 0

    ctx = ChatRequestContext.for_sync(user_id=user_id, session_id=session_id)
    ctx.reset_knowledge_tool_budget()
    request_agent = create_agent_for_request(ctx)

    try:
        context_messages = _build_context_messages(messages, persistent_note, user_text)
        messages.append(HumanMessage(content=user_text))
        storage.save(user_id, session_id, messages)

        result = request_agent.invoke(
            {"messages": context_messages},
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

        stored_trace = ctx.take_rag_trace()
        rag_trace = stored_trace.get("rag_trace") if stored_trace else None

        save_meta = dict(metadata)
        if is_first_message:
            save_meta["title"] = _generate_session_title_sync(user_text)
        save_meta["persistent_note"] = _update_persistent_note_sync(
            persistent_note, user_text, response_content
        )

        extra_message_data = [None] * (len(messages) - 1) + [{"rag_trace": rag_trace}]
        storage.save(
            user_id,
            session_id,
            messages,
            metadata=save_meta,
            extra_message_data=extra_message_data,
        )

        return {
            "response": response_content,
            "rag_trace": rag_trace,
        }
    finally:
        ctx.close()


async def chat_with_agent_stream(
    user_text: str,
    user_id: str = "default_user",
    session_id: str = "default_session",
):
    messages, metadata = storage.load_with_meta(user_id, session_id)
    persistent_note = metadata.get("persistent_note", "")
    is_first_message = len(messages) == 0

    output_queue = asyncio.Queue()
    ctx = ChatRequestContext.for_stream(
        user_id=user_id,
        session_id=session_id,
        output_queue=output_queue,
    )
    ctx.reset_knowledge_tool_budget()
    request_agent = create_agent_for_request(ctx)

    try:
        context_messages = _build_context_messages(messages, persistent_note, user_text)
        messages.append(HumanMessage(content=user_text))
        storage.save(user_id, session_id, messages)

        title_task = None
        if is_first_message:

            def _on_title_done(fut):
                try:
                    title = fut.result()
                    output_queue.put_nowait(
                        {"type": "session_title", "title": title, "session_id": session_id}
                    )
                except Exception as e:
                    print(f"Title task error: {e}")

            title_task = asyncio.create_task(generate_session_title(user_text))
            title_task.add_done_callback(_on_title_done)

        full_response = ""

        async def _agent_worker():
            nonlocal full_response
            try:
                async for msg, _metadata in request_agent.astream(
                    {"messages": context_messages},
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
                await output_queue.put(None)

        agent_task = asyncio.create_task(_agent_worker())

        try:
            while True:
                event = await output_queue.get()
                if event is None:
                    break
                yield f"data: {json.dumps(event)}\n\n"
        except GeneratorExit:
            agent_task.cancel()
            try:
                await agent_task
            except asyncio.CancelledError:
                pass
            raise
        finally:
            if not agent_task.done():
                agent_task.cancel()

        stored_trace = ctx.take_rag_trace()
        rag_trace = stored_trace.get("rag_trace") if stored_trace else None

        if rag_trace:
            yield f"data: {json.dumps({'type': 'trace', 'rag_trace': rag_trace})}\n\n"

        yield "data: [DONE]\n\n"

        save_meta = dict(metadata)
        if is_first_message and title_task is not None:
            try:
                save_meta["title"] = await title_task
            except Exception:
                pass

        try:
            save_meta["persistent_note"] = await update_persistent_note(
                persistent_note, user_text, full_response
            )
        except Exception as e:
            print(f"Update persistent note error: {e}")

        messages.append(AIMessage(content=full_response))
        extra_message_data = [None] * (len(messages) - 1) + [{"rag_trace": rag_trace}]
        storage.save(
            user_id,
            session_id,
            messages,
            metadata=save_meta,
            extra_message_data=extra_message_data,
        )
    finally:
        ctx.close()
