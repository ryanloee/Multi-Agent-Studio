import pytest
from app.workflows.plan_parser import parse_plan_to_dag

def test_structured_json_parsing():
    plan = '''Here is my plan:
```json
{
  "tasks": [
    {"id": "explore_1", "type": "explore", "prompt": "分析代码结构", "depends_on": []},
    {"id": "coder_1", "type": "coder", "prompt": "实现功能", "depends_on": ["explore_1"]},
    {"id": "review_1", "type": "review", "prompt": "审查代码", "depends_on": ["coder_1"]}
  ]
}
```
'''
    result = parse_plan_to_dag(plan)
    assert result is not None
    nodes, edges = result
    assert len(nodes) == 3
    assert len(edges) == 2
    assert edges[0]["source"] == "explore_1"
    assert edges[0]["target"] == "coder_1"
    assert edges[1]["source"] == "coder_1"
    assert edges[1]["target"] == "review_1"

def test_parallel_tasks():
    plan = '''```json
{
  "tasks": [
    {"id": "a", "type": "coder", "prompt": "task a", "depends_on": []},
    {"id": "b", "type": "coder", "prompt": "task b", "depends_on": []},
    {"id": "c", "type": "review", "prompt": "review", "depends_on": ["a", "b"]}
  ]
}
```'''
    result = parse_plan_to_dag(plan)
    assert result is not None
    nodes, edges = result
    assert len(nodes) == 3
    assert len(edges) == 2  # a->c, b->c

def test_no_json_returns_none():
    result = parse_plan_to_dag("Just some plain text without JSON")
    assert result is None

def test_invalid_json_returns_none():
    result = parse_plan_to_dag("```json\n{broken\n```")
    assert result is None
