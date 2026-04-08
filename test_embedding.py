from langchain_huggingface import HuggingFaceEmbeddings

embedding = HuggingFaceEmbeddings(
    model_name="BAAI/bge-m3"
)

# 测试
vec = embedding.embed_query("测试医疗RAG")
print("向量维度:", len(vec))  # 输出 1024 就是成功！