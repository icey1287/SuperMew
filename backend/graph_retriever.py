import os
import requests
import json
import logging

class GraphRetriever:
    """
    图检索器 (Graph Retriever)
    连接 Neo4j，通过用户查询对图谱进行检索。 (使用 HTTP API)
    """

    def __init__(self):
        self.uri = os.getenv("NEO4J_URI", "http://localhost:7474")
        self.user = os.getenv("NEO4J_USER", "neo4j")
        self.password = os.getenv("NEO4J_PASSWORD", "password")

        # 检查连通性
        try:
            resp = requests.get(f"{self.uri}/", auth=(self.user, self.password))
            if resp.status_code in [200, 401]: # 401 just means auth required, neo4j is there
                logging.info("Neo4j HTTP Connected successfully in GraphRetriever.")
            else:
                logging.warning(f"Neo4j connection returned {resp.status_code}")
        except Exception as e:
            logging.warning(f"Neo4j HTTP connection failed: {e}")

    def retrieve(self, query: str, top_k: int = 5) -> list[dict]:
        """
        核心图检索逻辑：
        这里为避免调用额外的 LLM 接口，采用简单高效的关键词匹配查询（模糊匹配）。
        """
        cypher_query = """
        MATCH (n:Entity)-[r]-(m:Entity)
        WHERE $query CONTAINS n.name 
           OR n.name CONTAINS $query
           OR $query CONTAINS m.name
           OR m.name CONTAINS $query
        RETURN n.name AS source, type(r) AS relation, m.name AS target
        LIMIT $limit
        """
        
        payload = {
            "statements": [
                {
                    "statement": cypher_query,
                    "parameters": {"query": query, "limit": top_k}
                }
            ]
        }

        try:
            resp = requests.post(
                f"{self.uri}/db/neo4j/tx/commit",
                auth=(self.user, self.password),
                json=payload,
                headers={"Content-Type": "application/json"}
            )
            resp.raise_for_status()
            data = resp.json()
            
            if "errors" in data and data["errors"]:
                print(f"Neo4j Query Error: {data['errors']}")
                return []
                
            results = data.get("results", [])
            if not results or not results[0]["data"]:
                return []
            
            graph_text_lines = []
            for row_data in results[0]["data"]:
                source, relation, target = row_data["row"]
                graph_text_lines.append(f"{source} -[{relation}]-> {target}")
            
            if not graph_text_lines:
                return []
            
            combined_text = "从知识图谱中检索到的关联知识：\n" + "\n".join(graph_text_lines)
            
            # 返回符合统一结构的上下文
            return [{
                "chunk_id": "graph_context_01",
                "text": combined_text,
                "filename": "Neo4j Knowledge Graph",
                "page_number": "Graph",
                "score": 0.9, 
                "chunk_level": 3,
            }]
                
        except Exception as e:
            print(f"Neo4j Graph retrieve error: {e}")
            return []

    def close(self):
        pass
