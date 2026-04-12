import os
import requests
import json
import time

NEO4J_URI = os.getenv("NEO4J_URI", "http://localhost:7474")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")

GRAPH_KNOWLEDGE = [
    # ---------------- 肿瘤未知原发部位 (CUP) 知识图谱 ----------------
    {"source": "不明原发部位肿瘤 (CUP)", "relation": "HAS_SUBTYPE", "target": "MSI-H/dMMR 不明原发肿瘤"},
    {"source": "不明原发部位肿瘤 (CUP)", "relation": "RECOMMENDED_EVALUATION", "target": "CT/MRI 影像学检查"},
    {"source": "不明原发部位肿瘤 (CUP)", "relation": "RECOMMENDED_EVALUATION", "target": "基因特征谱分析 (Gene signature profiling)"},
    {"source": "基因特征谱分析 (Gene signature profiling)", "relation": "HAS_BENEFIT", "target": "诊断获益"},
    {"source": "基因特征谱分析 (Gene signature profiling)", "relation": "RECOMMENDATION_LEVEL", "target": "3类推荐"},
    {"source": "MSI-H/dMMR 不明原发肿瘤", "relation": "DIAGNOSTIC_METHOD", "target": "IHC (免疫组化)"},
    {"source": "MSI-H/dMMR 不明原发肿瘤", "relation": "DIAGNOSTIC_METHOD", "target": "PCR (聚合酶链式反应)"},

    # ---------------- 系统性轻链淀粉样变性 (AL) 知识图谱 ----------------
    {"source": "系统性轻链淀粉样变性", "relation": "CLINICAL_ASSESSMENT", "target": "ECG (心电图)"},
    {"source": "系统性轻链淀粉样变性", "relation": "ORGAN_INVOLVEMENT_TEST", "target": "NT-proBNP 或 BNP (脑钠肽)"},
    {"source": "系统性轻链淀粉样变性", "relation": "ORGAN_INVOLVEMENT_TEST", "target": "troponin T 或 troponin I (肌钙蛋白)"},
    {"source": "系统性轻链淀粉样变性", "relation": "DIFFERENTIAL_DIAGNOSIS", "target": "MGUS (意义未明的单克隆丙种球蛋白血症)"},
    {"source": "MGUS (意义未明的单克隆丙种球蛋白血症)", "relation": "SYMPTOM", "target": "血清或尿中存在轻链但组织无淀粉样物质"},
]

def build_graph():
    print("正在连接 Neo4j 数据库...")
    
    statements = []
    # 批量构建所有的插入语句
    for triple in GRAPH_KNOWLEDGE:
        source = triple["source"]
        relation = triple["relation"]
        target = triple["target"]
        
        query = f"""
        MERGE (s:Entity {{name: $source}})
        MERGE (t:Entity {{name: $target}})
        MERGE (s)-[:{relation}]->(t)
        """
        statements.append({
            "statement": query,
            "parameters": {"source": source, "target": target}
        })
        
    payload = {"statements": statements}
    
    start_time = time.time()
    
    try:
        resp = requests.post(
            f"{NEO4J_URI}/db/neo4j/tx/commit",
            auth=(NEO4J_USER, NEO4J_PASSWORD),
            json=payload,
            headers={"Content-Type": "application/json"}
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data and data["errors"]:
            print(f"Neo4j HTTP 构建出错: {data['errors']}")
            return
            
        print(f"✅ 图数据库构建完成！成功插入 {len(GRAPH_KNOWLEDGE)} 条知识。耗时: {time.time() - start_time:.2f} 秒")
        
    except Exception as e:
        print(f"执行失败，请确认你的 Neo4j HTTP 服务是否启动！\n错误信息: {e}")

if __name__ == "__main__":
    build_graph()
