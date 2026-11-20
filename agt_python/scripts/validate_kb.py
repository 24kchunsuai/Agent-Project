"""知识库数据质量校验工具

校验内容：
1. raw/ 下所有 md 文档的 frontmatter 必填字段与 doc_id 唯一性
2. faq/faq.jsonl 的 JSON 合法性与 from_doc 引用完整性
3. eval/questions.jsonl 的 JSON 合法性、类型分布、expected_doc_ids 引用完整性

用法：python scripts/validate_kb.py
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

KB_ROOT = Path(__file__).resolve().parent.parent / "data" / "kb"
REQUIRED_FIELDS = ["doc_id", "title", "category", "source", "version", "updated", "owner"]
VALID_CATEGORIES = {"manual", "faq", "regulation", "project-doc", "drone-models"}
VALID_SOURCES = {"dji", "caac", "self", "synthetic"}

errors: list[str] = []
warnings: list[str] = []


def parse_frontmatter(text: str) -> dict:
    """解析 markdown 开头的 YAML frontmatter（仅支持 key: value 形式）"""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    fields = {}
    for line in text[3:end].splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


def load_jsonl(path: Path) -> list[dict]:
    records = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            errors.append(f"{path.name}:{lineno} JSON 解析失败 -> {exc}")
    return records


def check_raw_docs() -> set[str]:
    doc_ids: set[str] = set()
    docs = sorted((KB_ROOT / "raw").rglob("*.md"))
    if not docs:
        errors.append("raw/ 目录下没有任何 markdown 文档")
    for doc in docs:
        fm = parse_frontmatter(doc.read_text(encoding="utf-8"))
        rel = doc.relative_to(KB_ROOT)
        if not fm:
            errors.append(f"{rel} 缺少 frontmatter")
            continue
        for field in REQUIRED_FIELDS:
            if not fm.get(field):
                errors.append(f"{rel} frontmatter 缺少字段: {field}")
        doc_id = fm.get("doc_id", "")
        if doc_id in doc_ids:
            errors.append(f"{rel} doc_id 重复: {doc_id}")
        doc_ids.add(doc_id)
        if fm.get("category") and fm["category"] not in VALID_CATEGORIES:
            warnings.append(f"{rel} category 取值非预期: {fm['category']}")
        if fm.get("source") and fm["source"] not in VALID_SOURCES:
            warnings.append(f"{rel} source 取值非预期: {fm['source']}")
        body_len = len(doc.read_text(encoding="utf-8"))
        if body_len < 300:
            warnings.append(f"{rel} 内容偏短（{body_len} 字符），可能被切成过小的 chunk")
        if not re.search(r"^#{1,3} ", doc.read_text(encoding="utf-8"), re.MULTILINE):
            warnings.append(f"{rel} 正文缺少标题层级，不利于结构感知切片")
    return doc_ids


def check_faq(doc_ids: set[str]) -> list[dict]:
    path = KB_ROOT / "faq" / "faq.jsonl"
    if not path.exists():
        errors.append("faq/faq.jsonl 不存在")
        return []
    records = load_jsonl(path)
    seen = set()
    for idx, rec in enumerate(records, 1):
        for field in ["qa_id", "question", "answer", "category", "source", "from_doc", "owner"]:
            if not rec.get(field):
                errors.append(f"faq.jsonl 第 {idx} 条缺少字段: {field}")
        qa_id = rec.get("qa_id", "")
        if qa_id in seen:
            errors.append(f"faq.jsonl qa_id 重复: {qa_id}")
        seen.add(qa_id)
        from_doc = rec.get("from_doc", "")
        if from_doc and from_doc not in doc_ids:
            errors.append(f"faq.jsonl {qa_id} 的 from_doc 不存在于 raw/: {from_doc}")
    return records


def check_eval(doc_ids: set[str]) -> list[dict]:
    path = KB_ROOT / "eval" / "questions.jsonl"
    if not path.exists():
        errors.append("eval/questions.jsonl 不存在")
        return []
    records = load_jsonl(path)
    seen = set()
    for idx, rec in enumerate(records, 1):
        for field in ["qid", "question", "type", "design_branch", "expected_route",
                      "expected_hit_level", "expected_doc_ids", "expected_behavior"]:
            if field not in rec:
                errors.append(f"questions.jsonl 第 {idx} 条缺少字段: {field}")
        qid = rec.get("qid", "")
        if qid in seen:
            errors.append(f"questions.jsonl qid 重复: {qid}")
        seen.add(qid)
        for doc_id in rec.get("expected_doc_ids", []):
            if doc_id not in doc_ids:
                errors.append(f"questions.jsonl {qid} 引用了不存在的 doc_id: {doc_id}")
    return records


def main() -> int:
    print("=" * 64)
    print("知识库数据质量校验")
    print("=" * 64)

    doc_ids = check_raw_docs()
    faqs = check_faq(doc_ids)
    evals = check_eval(doc_ids)

    print(f"\n[文档] raw/ 共 {len(doc_ids)} 篇，doc_id 唯一性通过")
    print(f"[FAQ] 共 {len(faqs)} 条")
    print(f"[评估集] 共 {len(evals)} 条")

    if evals:
        print("\n评估集类型分布：")
        for name, count in Counter(r.get("type") for r in evals).most_common():
            print(f"  {name:<24} {count:>3} 条")
        print("\n评估集路由分布：")
        for name, count in Counter(r.get("expected_route") for r in evals).most_common():
            print(f"  {name:<24} {count:>3} 条")
        print("\n期望命中程度分布：")
        for name, count in Counter(r.get("expected_hit_level") for r in evals).most_common():
            print(f"  {name:<24} {count:>3} 条")

    if faqs:
        print("\nFAQ 分类分布：")
        for name, count in Counter(r.get("category") for r in faqs).most_common():
            print(f"  {name:<24} {count:>3} 条")

    print("\n" + "-" * 64)
    if errors:
        print(f"发现 {len(errors)} 个错误：")
        for item in errors:
            print(f"  [错误] {item}")
    else:
        print("未发现错误")

    if warnings:
        print(f"\n发现 {len(warnings)} 个提示：")
        for item in warnings:
            print(f"  [提示] {item}")

    print("-" * 64)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
